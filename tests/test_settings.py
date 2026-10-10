"""Unit tests for EscposSettings.from_env with an injected environment dict."""

import os

from escpos import EscposSettings

PPD_DEFAULTS = dict(cash_drawer1=0, cash_drawer2=0, blank_space=1,
                    feed_dist=2, cutting=1)


class TestFromEnv:
    def test_empty_env_yields_ppd_defaults(self):
        s = EscposSettings.from_env({})
        assert vars(s) == PPD_DEFAULTS

    def test_all_values_set(self):
        s = EscposSettings.from_env({
            "CASH_DRAWER1": "1", "CASH_DRAWER2": "2", "BLANK_SPACE": "0",
            "FEED_DIST": "5", "CUTTING": "0"})
        assert vars(s) == dict(cash_drawer1=1, cash_drawer2=2, blank_space=0,
                               feed_dist=5, cutting=0)

    def test_bad_int_falls_back_to_default(self):
        s = EscposSettings.from_env({
            "CASH_DRAWER1": "yes", "BLANK_SPACE": "", "FEED_DIST": "3x",
            "CUTTING": "2x"})
        # everything unparsable (or empty) keeps the PPD default
        assert vars(s) == PPD_DEFAULTS

    def test_partial_env_keeps_other_defaults(self):
        s = EscposSettings.from_env({"FEED_DIST": "0"})
        assert s.feed_dist == 0
        assert s.cutting == PPD_DEFAULTS["cutting"]

    def test_negative_values_accepted_as_ints(self):
        s = EscposSettings.from_env({"FEED_DIST": "-1"})
        assert s.feed_dist == -1

    def test_without_dict_uses_os_environ(self, monkeypatch):
        monkeypatch.setattr(os, "environ", {"FEED_DIST": "7"})
        assert EscposSettings.from_env().feed_dist == 7

    def test_none_argument_also_uses_os_environ(self, monkeypatch):
        monkeypatch.setattr(os, "environ", {"CUTTING": "2"})
        s = EscposSettings.from_env(None)
        assert s.cutting == 2