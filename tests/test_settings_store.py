"""Tests for settings_store: precedence, validation, atomic saves."""

import json

import pytest

import escpos
import settings_store


@pytest.fixture(autouse=True)
def _restore_paper_profile():
    """validate() may import escpos; keep the default profile afterwards."""
    before = escpos.PAPER_WIDTH_MM
    yield
    escpos.apply_paper_width(before)


class TestDefaults:
    def test_nothing_set_uses_defaults(self, tmp_path):
        values = settings_store.effective(env={}, path=tmp_path / "s.json")
        assert values["PAPER_WIDTH_MM"] == "48"
        assert values["MQTT_BROKER"] == "localhost"
        assert values["MQTT_TOPIC"] == "printer/commands"
        assert values["MQTT_AVAILABILITY_TOPIC"] == "printer/availability"
        assert values["PRINTER_DEVICE"] == "/dev/usb/lp1"

    def test_environment_wins_over_saved_value(self, tmp_path):
        path = tmp_path / "s.json"
        settings_store.save({"MQTT_BROKER": "panel-host",
                             "MQTT_TOPIC": "panel/commands"}, path)
        values = settings_store.effective(env={"MQTT_BROKER": "env-host"},
                                          path=path)
        assert values["MQTT_BROKER"] == "env-host"
        assert values["MQTT_TOPIC"] == "panel/commands"
        assert settings_store.locked(env={"MQTT_BROKER": "env-host"})[
            "MQTT_BROKER"] is True
        assert settings_store.locked(env={})["MQTT_BROKER"] is False

    def test_blank_environment_value_is_not_an_override(self, tmp_path):
        path = tmp_path / "s.json"
        settings_store.save({"MQTT_BROKER": "panel-host"}, path)
        values = settings_store.effective(env={"MQTT_BROKER": "   "}, path=path)
        assert values["MQTT_BROKER"] == "panel-host"
        assert settings_store.locked(env={"MQTT_BROKER": "   "})[
            "MQTT_BROKER"] is False

    def test_paper_device_env_name_also_overrides(self, tmp_path):
        values = settings_store.effective(
            env={"PAPER_DEVICE": "/dev/usb/lp9"}, path=tmp_path / "s.json")
        assert values["PRINTER_DEVICE"] == "/dev/usb/lp9"
        assert settings_store.locked(
            env={"PAPER_DEVICE": "/dev/usb/lp9"})["PRINTER_DEVICE"] is True


class TestSaveAndLoad:
    def test_roundtrip_is_private(self, tmp_path):
        path = tmp_path / "s.json"
        settings_store.save({"PAPER_WIDTH_MM": "58",
                             "MQTT_TOPIC": "kitchen/commands"}, path)
        assert settings_store.effective(env={}, path=path)["PAPER_WIDTH_MM"] == "58"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert json.loads(path.read_text())["MQTT_TOPIC"] == "kitchen/commands"

    def test_unknown_keys_are_dropped(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"MQTT_TOPIC": "a/b", "NOT_A_SETTING": 1}))
        assert settings_store.stored(path) == {"MQTT_TOPIC": "a/b"}

    def test_broken_file_reads_as_empty(self, tmp_path, capsys):
        path = tmp_path / "s.json"
        path.write_text("{not json")
        assert settings_store.stored(path) == {}
        assert "unreadable settings file" in capsys.readouterr().out

    def test_fingerprint_changes_after_save(self, tmp_path):
        path = tmp_path / "s.json"
        assert settings_store.fingerprint(path) is None
        settings_store.save({"MQTT_TOPIC": "a/b"}, path)
        assert settings_store.fingerprint(path) is not None
        before = settings_store.fingerprint(path)
        settings_store.save({"MQTT_TOPIC": "a/c"}, path)
        assert settings_store.fingerprint(path) != before


class TestUpdate:
    def test_saves_submitted_values(self, tmp_path):
        path = tmp_path / "s.json"
        result = settings_store.update({"PAPER_WIDTH_MM": "58"}, env={}, path=path)
        assert result["saved"] is True and result["errors"] == []
        assert settings_store.stored(path)["PAPER_WIDTH_MM"] == "58"

    def test_environment_locked_key_is_ignored(self, tmp_path):
        path = tmp_path / "s.json"
        result = settings_store.update(
            {"MQTT_BROKER": "panel-host", "PAPER_WIDTH_MM": "80"},
            env={"MQTT_BROKER": "env-host"}, path=path)
        assert result["saved"] is True
        assert result["ignored_locked"] == ["MQTT_BROKER"]
        assert settings_store.stored(path) == {"PAPER_WIDTH_MM": "80"}

    @pytest.mark.parametrize("values,message", [
        ({"PAPER_WIDTH_MM": "57"}, "PAPER_WIDTH_MM must be 48, 58 or 80"),
        ({"PRINTER_DEVICE": "usb/lp0"}, "PRINTER_DEVICE must be an absolute path"),
        ({"PRINTER_DEVICE": "/"}, "PRINTER_DEVICE must be an absolute path"),
        ({"MQTT_BROKER": "  "}, "MQTT_BROKER must be a hostname or address"),
        ({"MQTT_TOPIC": "printer/#"}, "MQTT_TOPIC must be a plain MQTT topic"),
        ({"MQTT_PAPER_TOPIC": "+/paper"}, "plain MQTT topic"),
        ({"MQTT_LAST_ERROR_TOPIC": "$SYS/x"}, "plain MQTT topic"),
        ({"MQTT_USERNAME": "bad\nuser"}, "control characters"),
    ])
    def test_invalid_values_are_refused(self, tmp_path, values, message):
        path = tmp_path / "s.json"
        result = settings_store.update(values, env={}, path=path)
        assert result["saved"] is False
        assert any(message in error for error in result["errors"])
        assert not path.exists()


class TestTopicPrefix:
    def test_default_prefix_builds_every_topic(self, tmp_path):
        values = settings_store.effective(env={}, path=tmp_path / "s.json")
        assert values["MQTT_TOPIC_PREFIX"] == "printer"
        assert values["MQTT_TOPIC"] == "printer/commands"
        assert values["MQTT_AVAILABILITY_TOPIC"] == "printer/availability"
        assert values["MQTT_PAPER_TOPIC"] == "printer/paper"
        assert values["MQTT_PAPER_LOW_TOPIC"] == "printer/paper_low"
        assert values["MQTT_LAST_ERROR_TOPIC"] == "printer/last_error"
        assert values["MQTT_DROPPED_TOTAL_TOPIC"] == "printer/dropped_total"
        assert values["MQTT_FAILED_TOTAL_TOPIC"] == "printer/failed_total"

    def test_saved_prefix_moves_every_topic(self, tmp_path):
        path = tmp_path / "s.json"
        result = settings_store.update(
            {"MQTT_TOPIC_PREFIX": "receiptprinter2/"}, env={}, path=path)
        assert result["saved"] is True
        values = settings_store.effective(env={}, path=path)
        assert values["MQTT_TOPIC_PREFIX"] == "receiptprinter2"
        assert values["MQTT_TOPIC"] == "receiptprinter2/commands"
        assert values["MQTT_FAILED_TOTAL_TOPIC"] == "receiptprinter2/failed_total"
        assert settings_store.stored(path) == {
            "MQTT_TOPIC_PREFIX": "receiptprinter2"}

    def test_per_topic_environment_still_wins(self, tmp_path):
        path = tmp_path / "s.json"
        settings_store.save({"MQTT_TOPIC_PREFIX": "printer_two"}, path)
        values = settings_store.effective(
            env={"MQTT_PAPER_TOPIC": "custom/paper"}, path=path)
        assert values["MQTT_PAPER_TOPIC"] == "custom/paper"
        assert values["MQTT_TOPIC"] == "printer_two/commands"

    def test_old_per_topic_values_survive_until_a_prefix_is_saved(self, tmp_path):
        path = tmp_path / "s.json"
        settings_store.save({"MQTT_TOPIC": "legacy/commands"}, path)
        values = settings_store.effective(env={}, path=path)
        assert values["MQTT_TOPIC"] == "legacy/commands"
        assert values["MQTT_PAPER_TOPIC"] == "printer/paper"
        settings_store.update({"MQTT_TOPIC_PREFIX": "newprinter"},
                              env={}, path=path)
        values = settings_store.effective(env={}, path=path)
        assert values["MQTT_TOPIC"] == "newprinter/commands"
        assert "MQTT_TOPIC" not in settings_store.stored(path)

    @pytest.mark.parametrize("value,message", [
        ("", "plain topic prefix"),
        ("/leading", "plain topic prefix"),
        ("$sys", "plain topic prefix"),
        ("bad prefix", "plain topic prefix"),
        ("a/#", "plain topic prefix"),
    ])
    def test_invalid_prefix_is_refused(self, tmp_path, value, message):
        result = settings_store.update(
            {"MQTT_TOPIC_PREFIX": value}, env={}, path=tmp_path / "s.json")
        assert result["saved"] is False
        assert any(message in error for error in result["errors"])


class TestExtraFeed:
    def test_default_is_zero_and_saved_value_applies(self, tmp_path):
        path = tmp_path / "s.json"
        assert settings_store.effective(env={}, path=path)["EXTRA_FEED_MM"] == "0"
        assert settings_store.update({"EXTRA_FEED_MM": "12"},
                                     env={}, path=path)["saved"] is True
        assert settings_store.effective(env={}, path=path)["EXTRA_FEED_MM"] == "12"

    @pytest.mark.parametrize("value", ["-1", "251", "abc", "1.5"])
    def test_must_be_whole_mm_in_range(self, tmp_path, value):
        result = settings_store.update(
            {"EXTRA_FEED_MM": value}, env={}, path=tmp_path / "s.json")
        assert result["saved"] is False
        assert any("EXTRA_FEED_MM" in error for error in result["errors"])
