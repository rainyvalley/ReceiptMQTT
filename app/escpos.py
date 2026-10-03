"""ESC/POS emission for ZJ-58/ZJ-80-class thermal receipt printers.

Python port of the rastertozj CUPS filter logic (klirichek/zj-58, BSD-2-Clause),
so the deprecated PPD-based CUPS filter chain can be replaced with a raw
queue: PDF -> Ghostscript pbmraw (1 bpp packed, black=1) -> this module ->
lp on a raw CUPS queue.

The PPD options the C filter read (CashDrawer1Setting, CashDrawer2Setting,
BlankSpace, FeedDist, Cutting) are carried over as environment variables with
the same default values the stock zj58 PPD marked as default.
"""

import io
import os
import sys

ESC = 0x1B
GS = 0x1D

INIT = bytes((ESC, 0x40))
CUT = bytes((ESC, 0x69))
RASTER_START = bytes((GS, 0x76, 0x30, 0x00))
CASH_DRAWER = (bytes((ESC, 0x70, 0x00, 0x40, 0x50)),
               bytes((ESC, 0x70, 0x01, 0x40, 0x50)))

SKIPPED_BLANK_LINES = 24  # rastertozj.c skiplines(24) for blank bands
ENDPAGE_FEED = 0x18       # rastertozj.c skiplines(0x18) perFeedDist unit
MAX_BAND_LINES = 24       # ESC/POS raster band height from rastertozj.c
MAX_WIDTH_BYTES = 48      # rastertozj.c caps width at 0x180 px = 48 bytes


class EscposSettings:
    """Job options, mirroring rastertozj.c struct settings_ and PPD defaults.

    PPD defaults: FeedDist=2feed9mm -> 2; BlankSpace=1NoPrint;
    Cutting=1CutAtTheEndOfPage; cash drawers disabled (0).
    """

    def __init__(self, cash_drawer1=0, cash_drawer2=0, blank_space=1,
                 feed_dist=2, cutting=1):
        self.cash_drawer1 = cash_drawer1
        self.cash_drawer2 = cash_drawer2
        self.blank_space = blank_space
        self.feed_dist = feed_dist
        self.cutting = cutting

    @classmethod
    def from_env(cls, env=None):
        env = env if env is not None else os.environ

        def int_env(name, default):
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        return cls(int_env("CASH_DRAWER1", 0), int_env("CASH_DRAWER2", 0),
                   int_env("BLANK_SPACE", 1), int_env("FEED_DIST", 2),
                   int_env("CUTTING", 1))


def raster_header(width_bytes, height_lines):
    """GS v 0 0 followed by little-endian xDim (bytes) and yDim (lines)."""
    return (RASTER_START
            + bytes((width_bytes & 0xFF, (width_bytes >> 8) & 0xFF,
                     height_lines & 0xFF, (height_lines >> 8) & 0xFF)))


def skiplines(size):
    """ESC J n: flush the print buffer, then feed n pixel lines."""
    return bytes((ESC, 0x4A, size & 0xFF))


def read_pbm_header(stream):
    """Read the P4 magic, then the whitespace-separated width/height line.

    Handles comment lines (starting with #) per the PBM spec.
    """
    signature = stream.read(2)
    if signature != b"P4":
        raise ValueError(f"unsupported PBM signature: {signature!r}")
    fields = []
    while len(fields) < 2:
        line = stream.readline()
        if not line:
            raise ValueError("truncated PBM header")
        for raw_token in line.split(b"#")[0].split():
            token = raw_token.split(b"#")[0]
            if token:
                fields.append(int(token))
        if len(fields) >= 2:
            # width and height may share one line; the height token ends the
            # header and the very next byte starts pixel data.
            remainder = line.split(b"#")[0].split(maxsplit=2)
            if len(remainder) == 3:
                extra = remainder[2]
                if extra.strip():
                    raise ValueError("PBM height tokens must end the header line")
            break
    try:
        width, height = fields[0], fields[1]
    except IndexError:
        raise ValueError("incomplete PBM header") from None
    return width, height


def read_pbm_page(stream):
    """Parse one P4 PBM page; returns (width, height, packed rows or None).

    Rows are padded to byte boundaries exactly as ESC/POS raster rows of the
    same width are, so banding can consume them without repacking.
    """
    width, height = read_pbm_header(stream)
    row_bytes = (width + 7) // 8
    data = stream.read(row_bytes * height)
    if len(data) < row_bytes * height:
        raise ValueError("truncated PBM pixel data")
    rows = [data[i * row_bytes:(i + 1) * row_bytes] for i in range(height)]
    return width, height, rows


def rasterize_rows(rows, settings):
    """Band a raster page into ESC/POS, ported from rastertozj.c main loop.

    Fully blank bands are skipped (ESC J 24 per band; printed out only when
    blank_space == 0); printed bands flush with ESC J 0. Page-End feeds
    feed_dist x ESC J 0x18, then per-page cutting when cutting == 1.
    """
    out = bytearray()
    total = len(rows)
    row_bytes = len(rows[0]) if rows else 0
    width_bytes = min(row_bytes, MAX_WIDTH_BYTES)

    y = 0
    zeroy = 0
    while y < total:
        rest = total - y
        if rest > MAX_BAND_LINES:
            rest = MAX_BAND_LINES
        y += rest

        band = bytearray(width_bytes * rest)
        for j in range(rest):
            band[j * width_bytes:(j + 1) * width_bytes] = rows[y - rest + j][:width_bytes]

        if not any(band):
            zeroy += 1
            continue

        for _ in range(zeroy):
            out += skiplines(SKIPPED_BLANK_LINES)
        zeroy = 0

        out += raster_header(width_bytes, rest)
        out += band
        out += skiplines(0)

    if not settings.blank_space:
        for _ in range(zeroy):
            out += skiplines(SKIPPED_BLANK_LINES)

    for _ in range(settings.feed_dist):
        out += skiplines(ENDPAGE_FEED)
    if settings.cutting == 1:
        out += bytes((ESC, 0x69))
    return bytes(out)


def job_setup(settings):
    """ESC/POS preamble: cash drawers before print, then ESC @ reset."""
    out = bytearray()
    if settings.cash_drawer1 == 1:
        out += CASH_DRAWER[0]
    if settings.cash_drawer2 == 1:
        out += CASH_DRAWER[1]
    out += bytes((ESC, 0x40))
    return bytes(out)


def job_finish(settings):
    """ESC/POS trailer: job-end cut, drawers after print, ESC @ reset."""
    out = bytearray()
    if settings.cutting == 2:
        out += bytes((ESC, 0x69))
    if settings.cash_drawer1 == 2:
        out += CASH_DRAWER[0]
    if settings.cash_drawer2 == 2:
        out += CASH_DRAWER[1]
    out += bytes((ESC, 0x40))
    return bytes(out)


def render_all(input_bytes, settings):
    """Complete ESC/POS job for one or more concatenated PBM pages."""
    stream = io.BytesIO(input_bytes)
    out = bytearray()
    out += job_setup(settings)
    pages = 0
    # read_pbm_page consumes exactly one page per call until EOF; a trailing
    # parse failure after at least one page is trailing whitespace, not fatal.
    while True:
        try:
            _, _, rows = read_pbm_page(stream)
        except ValueError as exc:
            if pages > 0 and "unsupported PBM signature" in str(exc):
                break
            raise
        pages += 1
        out += rasterize_rows(rows, settings)
    out += job_finish(settings)
    return bytes(out), pages


def selftest_pbm(width=384, height=480):
    """Deterministic border + diagonal pattern for regression testing."""
    row_bytes = (width + 7) // 8
    rows = bytearray()
    for y in range(height):
        row = bytearray(row_bytes)
        if y in (0, height - 1):
            for i in range(row_bytes):
                row[i] = 0xFF
        else:
            row[0] |= 0x80
            row[row_bytes - 1] |= 0x01
            col = (y * (width - 1)) // height
            row[col // 8] |= 0x80 >> (col % 8)
        rows += row
    return b"P4\n%d %d\n" % (width, height) + bytes(rows)


def main():
    """CLI: read PBM from stdin or a file argument, write ESC/POS to stdout.

    --selftest emits a known pattern instead of reading input.
    """
    if "--selftest" in sys.argv:
        input_bytes = selftest_pbm()
        settings = EscposSettings()
    else:
        if len(sys.argv) > 1:
            with open(sys.argv[1], "rb") as fh:
                input_bytes = fh.read()
        else:
            input_bytes = sys.stdin.buffer.read()
        settings = EscposSettings.from_env()

    out, _pages = render_all(input_bytes, settings)
    sys.stdout.buffer.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())