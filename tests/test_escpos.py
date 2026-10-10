"""Unit tests for app/escpos.py: PBM parsing, raster banding, job framing.

Everything runs against in-memory bytes - no device, no ghostscript.
"""

import io

import pytest

import escpos
from escpos import (EscposSettings, RASTER_START, read_pbm_header,
                    read_pbm_page, rasterize_rows, render_all, job_setup,
                    job_finish, skiplines)


def pbm_page(width, height, fill=b"\xf0"):
    """Minimal single-line PBM page of the given geometry."""
    row_bytes = (width + 7) // 8
    return b"P4\n%d %d\n" % (width, height) + fill * row_bytes * height


# ---------------------------------------------------------------- PBM header

class TestReadPbmHeader:
    def test_basic(self):
        assert read_pbm_header(io.BytesIO(b"P4\n384 480\n")) == (384, 480)

    def test_width_height_on_one_line(self):
        assert read_pbm_header(io.BytesIO(b"P4\n7 3")) == (7, 3)

    def test_split_across_lines(self):
        assert read_pbm_header(io.BytesIO(b"P4\n384\n480\n")) == (384, 480)

    def test_comment_lines_before_dimensions(self):
        data = b"P4\n# created by gs pbmraw\n# second comment\n384 480\n"
        assert read_pbm_header(io.BytesIO(data)) == (384, 480)

    def test_inline_comment_after_height(self):
        data = b"P4\n384 480 # trailing comment\n"
        assert read_pbm_header(io.BytesIO(data)) == (384, 480)

    def test_bad_signature(self):
        with pytest.raises(ValueError, match="unsupported PBM signature"):
            read_pbm_header(io.BytesIO(b"P5\n1 1\n"))

    def test_truncation_immediately_after_magic(self):
        with pytest.raises(ValueError, match="truncated PBM header"):
            read_pbm_header(io.BytesIO(b"P4"))

    def test_truncation_before_dimensions(self):
        with pytest.raises(ValueError, match="truncated PBM header"):
            read_pbm_header(io.BytesIO(b"P4\n384\n"))

    def test_truncation_inside_comment(self):
        with pytest.raises(ValueError, match="truncated PBM header"):
            read_pbm_header(io.BytesIO(b"P4\n# nowhere\n"))

    def test_extra_token_after_height_rejected(self):
        with pytest.raises(ValueError, match="must end the header line"):
            read_pbm_header(io.BytesIO(b"P4\n384 480 99\n"))


# ------------------------------------------------------------------ PBM page

class TestReadPbmPage:
    def test_round_trip(self):
        page = pbm_page(384, 3)
        width, height, rows = read_pbm_page(io.BytesIO(page))
        assert (width, height) == (384, 3)
        assert len(rows) == 3 and all(len(r) == 48 for r in rows)

    def test_width_padded_to_byte_boundary(self):
        # width 9 px pads to 2 storage bytes, pad bits zero
        width, height, rows = read_pbm_page(
            io.BytesIO(b"P4\n9 1\n\xa0\x00"))
        assert (width, height) == (9, 1)
        assert rows == [b"\xa0\x00"]

    def test_truncated_pixel_data(self):
        page = pbm_page(384, 4)[:-10]
        with pytest.raises(ValueError, match="truncated PBM pixel data"):
            read_pbm_page(io.BytesIO(page))

    def test_pixel_data_starts_right_after_header_line(self):
        # The header line's newline is the data separator: parsing must
        # leave the stream positioned on the first pixel row, none skipped.
        data = b"P4\n8 2\n" + b"\x80\x40"
        width, height, rows = read_pbm_page(io.BytesIO(data))
        assert rows == [b"\x80", b"\x40"]


# ------------------------------------------------------------ raster banding

class TestRasterizeRows:
    def test_single_band_structure(self):
        rows = [b"\xff" * 48] * 24
        out = rasterize_rows(rows, EscposSettings())
        # one band: GS v 0 + xDim/YDim(=24) + band + ESC J 0 flush...
        starts = [i for i in range(len(out)) if out[i:i + 4] == RASTER_START]
        assert len(starts) == 1
        header = out[starts[0]:starts[0] + 8]
        assert header == escpos.raster_header(48, 24)
        assert out[starts[0] + 8:starts[0] + 8 + 48 * 24] == b"\xff" * 48 * 24
        # page-end feed (FEED_DIST=2) and cut per PPD defaults
        assert out.endswith(skiplines(0x18) * 2 + b"\x1bi")

    def test_band_heights_split_over_max(self):
        rows = [b"\xff" * 48] * 50  # 24 + 24 + 2
        out = rasterize_rows(rows, EscposSettings())
        starts = [i for i in range(len(out)) if out[i:i + 4] == RASTER_START]
        assert len(starts) == 3
        for start, band_lines in zip(starts, (24, 24, 2)):
            header = out[start:start + 8]
            assert header == escpos.raster_header(48, band_lines)
            band = out[start + 8:start + 8 + 48 * band_lines]
            assert band == b"\xff" * (48 * band_lines)

    def test_width_clamped_to_48_bytes(self):
        rows = [b"\xff" * 60] * 1
        out = rasterize_rows(rows, EscposSettings())
        start = out.index(RASTER_START)
        assert out[start:start + 8] == escpos.raster_header(48, 1)
        # exactly 48 bytes of each row reach the band; the rest is dropped
        assert out == (escpos.raster_header(48, 1)
                       + b"\xff" * 48          # clamped band
                       + skiplines(0)          # flush ESC J 0
                       + skiplines(0x18) * 2   # FEED_DIST page end feed
                       + b"\x1bi")             # cut at end of page

    def test_blank_rows_skipped_when_blank_space_set(self):
        rows = [b"\x00" * 48] * 48
        out = rasterize_rows(rows, EscposSettings(blank_space=1))
        # nothing printed at all; only the page feed and the cut remain,
        # and no blank-band skip feeds are emitted for the dropped page
        assert out == skiplines(0x18) * 2 + b"\x1bi"
        assert out.endswith(skiplines(0x18) * 2 + b"\x1bi")

    def test_blank_rows_fed_when_blank_space_zero(self):
        rows = [b"\x00" * 48] * 48  # two blank 24-line bands
        out = rasterize_rows(rows, EscposSettings(blank_space=0, cutting=0))
        assert RASTER_START not in out
        # blank band skip feeds are emitted after all, then the page feed
        assert skiplines(escpos.SKIPPED_BLANK_LINES) * 2 in out
        assert out.startswith(skiplines(escpos.SKIPPED_BLANK_LINES))

    def test_blank_band_before_content_leads_to_one_skip_feed(self):
        rows = [b"\x00" * 48] * 24 + [b"\xff" * 48] * 24
        out = rasterize_rows(rows, EscposSettings())
        # the blank first band collapses into one ESC J 24 before the band
        assert out == (skiplines(escpos.SKIPPED_BLANK_LINES)
                       + escpos.raster_header(48, 24)
                       + b"\xff" * 48 * 24
                       + skiplines(0)
                       + skiplines(0x18) * 2
                       + b"\x1bi")

    def test_cutting_disabled_drops_cut(self):
        out = rasterize_rows([b"\xff" * 48], EscposSettings(cutting=0))
        assert not out.endswith(b"\x1bi")
        assert out.endswith(skiplines(0x18) * 2)

    def test_feed_dist_zero(self):
        out = rasterize_rows([b"\xff" * 48], EscposSettings(feed_dist=0,
                                                           cutting=0))
        assert out == escpos.raster_header(48, 1) + b"\xff" * 48 + skiplines(0)


# ------------------------------------------------------------- job framing

class TestJobSetupFinish:
    def test_setup_default_is_reset_only(self):
        assert job_setup(EscposSettings()) == b"\x1b@"

    def test_setup_drawer1_pulses_first(self):
        out = job_setup(EscposSettings(cash_drawer1=1))
        assert out == escpos.CASH_DRAWER[0] + b"\x1b@"

    def test_setup_both_drawers_in_order(self):
        out = job_setup(EscposSettings(cash_drawer1=1, cash_drawer2=1))
        assert out == escpos.CASH_DRAWER[0] + escpos.CASH_DRAWER[1] + b"\x1b@"

    def test_finish_default_is_reset_only(self):
        assert job_finish(EscposSettings()) == b"\x1b@"

    def test_finish_cut_when_cutting_set_to_2(self):
        assert job_finish(EscposSettings(cutting=2)).startswith(b"\x1bi")

    def test_finish_drawer_pulse_when_value_2(self):
        out = job_finish(EscposSettings(cutting=2, cash_drawer2=2))
        assert out == b"\x1bi" + escpos.CASH_DRAWER[1] + b"\x1b@"


# --------------------------------------------------------------- render_all

class TestRenderAll:
    def test_multipage_concatenation(self):
        # gs pbmraw emits pages back to back with no separator bytes; the
        # parser consumes exactly one page per call until EOF.
        blob = pbm_page(384, 2) + pbm_page(384, 3)
        out, pages = render_all(blob, EscposSettings())
        assert pages == 2
        assert out.count(RASTER_START) == 2
        assert out.startswith(b"\x1b@")      # job setup
        assert out.endswith(b"\x1b@")        # job finish (cutting != 2)

    def test_trailing_whitespace_after_last_page_is_tolerated(self):
        blob = pbm_page(8, 1) + b"\n\n"
        out, pages = render_all(blob, EscposSettings())
        assert pages == 1
        assert out.count(RASTER_START) == 1

    def test_empty_input_raises(self):
        with pytest.raises(ValueError):
            render_all(b"", EscposSettings())

    def test_garbage_only_raises(self):
        with pytest.raises(ValueError):
            render_all(b"not a pbm", EscposSettings())

    def test_settings_apply_once_per_job(self):
        blob = pbm_page(8, 1) + pbm_page(8, 1)
        out, pages = render_all(blob, EscposSettings(cash_drawer1=1))
        setup = escpos.CASH_DRAWER[0] + b"\x1b@"
        assert out.count(setup) == 1          # setup, not per page
        assert pages == 2