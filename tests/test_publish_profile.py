"""The publish tool: the thing an operator is holding when economics change."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_stub = types.ModuleType("prisma")
_stub.Json = lambda x: x
_stub.Prisma = object
sys.modules.setdefault("prisma", _stub)

_spec = importlib.util.spec_from_file_location(
    "publish_profile", Path(__file__).resolve().parent.parent
    / "scripts" / "publish_profile.py")
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


# ---- durations ---------------------------------------------------------------------

def test_days_and_hours_convert_to_blocks():
    assert pp.parse_duration("7d") == 7 * pp.BLOCKS_PER_DAY
    assert pp.parse_duration("12h") == 3600
    assert pp.parse_duration("1.5d") == int(1.5 * pp.BLOCKS_PER_DAY)


def test_a_raw_block_count_still_works():
    assert pp.parse_duration("300") == 300


def test_nonsense_is_refused_rather_than_guessed():
    import argparse
    for bad in ("soon", "7 weeks", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            pp.parse_duration(bad)


def test_a_day_is_the_expected_number_of_blocks():
    assert pp.BLOCKS_PER_DAY == 7200


# ---- the diff ----------------------------------------------------------------------

LIVE = {"settlement": {"C": 26697.0, "live": False},
        "emission": {"gain": 100.0, "midpoint": 0.59},
        "rations": {"dispatch": False}, "oracle": {"live": False}, "controller": {}}


def test_every_changed_value_is_listed():
    new = {**LIVE, "settlement": {"C": 30000.0, "live": True},
           "emission": {"gain": 15.0, "midpoint": 0.59}}
    lines = "\n".join(pp.describe_change(new, LIVE))
    assert "settlement.C" in lines and "+12.4%" in lines
    assert "emission.gain" in lines
    assert "settlement.live" in lines
    assert "midpoint" not in lines          # unchanged values stay quiet


def test_a_publish_that_changes_nothing_says_so():
    assert "no economic value differs" in " ".join(pp.describe_change(LIVE, LIVE))


def test_the_first_publish_is_labelled():
    assert "first profile" in " ".join(pp.describe_change(LIVE, None))


def test_percentages_are_only_shown_where_they_mean_something():
    new = {**LIVE, "oracle": {"live": True}}
    line = [l for l in pp.describe_change(new, LIVE) if "oracle.live" in l][0]
    assert "%" not in line          # a boolean has no percentage change


# ---- the switches ------------------------------------------------------------------

def test_the_pay_affecting_switches_are_the_ones_called_out():
    assert set(pp.SWITCHES) == {
        ("settlement", "live"), ("settlement", "floor_gating"),
        ("rations", "dispatch"), ("oracle", "live")}


# ---- the capacity notice -----------------------------------------------------------

def test_the_capacity_notice_is_a_week():
    assert pp.CAPACITY_NOTICE_DAYS == 7


def test_the_notice_is_not_enforced():
    """Rollback is itself a publish, so a hard minimum would trap a bad value."""
    source = (Path(__file__).resolve().parent.parent
              / "scripts" / "publish_profile.py").read_text()
    assert "warnings.append" in source
    # the only refusals are an invalid body or an unreadable chain head
    refusals = [l for l in source.splitlines() if "REFUSED" in l]
    assert len(refusals) == 2
    assert any("chain head" in l for l in refusals)
