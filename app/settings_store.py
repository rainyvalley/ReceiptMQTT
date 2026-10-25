"""Panel-editable settings, with the container environment taking precedence.

The web panel stores what it changes in one JSON file (`/app/data/settings.json`
by default). A variable that is set and non-empty in the environment is an
override: the panel shows that field read-only and never writes it. Docker
Compose and `.env` therefore keep working exactly as before, while a plain
`docker run` deployment can be configured from the panel.

Values are always strings and are validated before they are written. The file
holds the MQTT password, so it is written 0600 inside a 0700 directory.
"""

import json
import os
import tempfile

SETTINGS_FILE = os.getenv("SETTINGS_FILE") or "/app/data/settings.json"

KEYS = ("PAPER_WIDTH_MM", "MQTT_BROKER", "MQTT_USERNAME", "MQTT_PASSWORD",
        "MQTT_TOPIC", "MQTT_AVAILABILITY_TOPIC", "MQTT_PAPER_TOPIC",
        "MQTT_PAPER_LOW_TOPIC", "MQTT_LAST_ERROR_TOPIC",
        "MQTT_DROPPED_TOTAL_TOPIC", "MQTT_FAILED_TOTAL_TOPIC",
        "PRINTER_DEVICE")

TOPIC_KEYS = ("MQTT_TOPIC", "MQTT_AVAILABILITY_TOPIC", "MQTT_PAPER_TOPIC",
              "MQTT_PAPER_LOW_TOPIC", "MQTT_LAST_ERROR_TOPIC",
              "MQTT_DROPPED_TOTAL_TOPIC", "MQTT_FAILED_TOTAL_TOPIC")

# Used when neither the environment nor the panel provides a value. The panel
# starts from these, so its form shows what the app would do anyway.
DEFAULTS = {
    "PAPER_WIDTH_MM": "48",
    "MQTT_BROKER": "localhost",
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
    "MQTT_TOPIC": "printer/commands",
    "MQTT_AVAILABILITY_TOPIC": "printer/availability",
    "MQTT_PAPER_TOPIC": "printer/paper",
    "MQTT_PAPER_LOW_TOPIC": "printer/paper_low",
    "MQTT_LAST_ERROR_TOPIC": "printer/last_error",
    "MQTT_DROPPED_TOTAL_TOPIC": "printer/dropped_total",
    "MQTT_FAILED_TOTAL_TOPIC": "printer/failed_total",
    "PRINTER_DEVICE": "/dev/usb/lp1",
}

# PRINTER_DEVICE has a second, older environment name; treat both as overrides.
ENV_NAMES = {"PRINTER_DEVICE": ("PRINTER_DEVICE", "PAPER_DEVICE")}

MAX_LENGTHS = {
    "MQTT_BROKER": 253,
    "MQTT_USERNAME": 256,
    "MQTT_PASSWORD": 1024,
    "PRINTER_DEVICE": 4096,
    "MQTT_TOPIC": 65535,
    "MQTT_AVAILABILITY_TOPIC": 65535,
    "MQTT_PAPER_TOPIC": 65535,
    "MQTT_PAPER_LOW_TOPIC": 65535,
    "MQTT_LAST_ERROR_TOPIC": 65535,
    "MQTT_DROPPED_TOTAL_TOPIC": 65535,
    "MQTT_FAILED_TOTAL_TOPIC": 65535,
}


def env_value(key, env=None):
    """The environment override for one key, or None when it is not set."""
    env = os.environ if env is None else env
    for name in ENV_NAMES.get(key, (key,)):
        raw = env.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return None


def locked(env=None):
    """Which keys the environment controls and the panel must not change."""
    return {key: env_value(key, env) is not None for key in KEYS}


def stored(path=None):
    """What the panel has saved. Missing or broken files read as empty."""
    path = path or SETTINGS_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"Ignoring unreadable settings file {path}: {exc}")
        return {}
    if not isinstance(data, dict):
        print(f"Ignoring settings file {path}: not a JSON object")
        return {}
    return {key: str(value) for key, value in data.items()
            if key in KEYS and value is not None}


def effective(env=None, path=None):
    """Final values: environment override, else panel value, else default."""
    saved = stored(path)
    return {key: (env_value(key, env)
                  if env_value(key, env) is not None
                  else saved.get(key, DEFAULTS[key]))
            for key in KEYS}


def validate(values):
    """(clean values, errors) for the keys that were submitted."""
    clean, errors = {}, []
    for key, raw in values.items():
        if key not in KEYS:
            continue
        value = "" if raw is None else str(raw).strip()
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            errors.append(f"{key} contains control characters")
            continue
        if key == "PAPER_WIDTH_MM":
            import escpos
            allowed = {str(mm) for mm in escpos.PAPER_PROFILES}
            if value not in allowed:
                errors.append("PAPER_WIDTH_MM must be 48, 58 or 80")
                continue
        elif key == "PRINTER_DEVICE":
            if not value.startswith("/") or value == "/":
                errors.append("PRINTER_DEVICE must be an absolute path")
                continue
        elif key == "MQTT_BROKER":
            if not value or any(ch.isspace() for ch in value):
                errors.append("MQTT_BROKER must be a hostname or address")
                continue
        elif key in TOPIC_KEYS:
            # These are publish topics, so wildcards are not valid, and a
            # leading $ would aim at the broker's own topics.
            if (not value or value.startswith("$")
                    or any(ch in "#+" for ch in value)
                    or any(ch.isspace() for ch in value)):
                errors.append(f"{key} must be a plain MQTT topic")
                continue
        limit = MAX_LENGTHS.get(key)
        if limit is not None and len(value) > limit:
            errors.append(f"{key} is longer than {limit} characters")
            continue
        clean[key] = value
    return clean, errors


def save(values, path=None):
    """Write the settings atomically, readable only by the app user."""
    path = path or SETTINGS_FILE
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(values, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(values, env=None, path=None):
    """Validate and store panel changes; environment-locked keys are ignored."""
    path = path or SETTINGS_FILE
    locks = locked(env)
    wanted = {key: value for key, value in values.items() if key in KEYS}
    ignored = sorted(key for key in wanted if locks[key])
    clean, errors = validate({k: v for k, v in wanted.items() if not locks[k]})
    if errors:
        return {"saved": False, "errors": errors, "ignored_locked": ignored}
    merged = stored(path)
    merged.update(clean)
    save(merged, path)
    return {"saved": True, "errors": [], "ignored_locked": ignored}


def fingerprint(path=None):
    """Identity of the settings file, for change detection by the handler."""
    path = path or SETTINGS_FILE
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
