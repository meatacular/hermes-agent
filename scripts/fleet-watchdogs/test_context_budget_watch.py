"""context-budget-watch: pure-logic and synthetic-fleet tests.

Every positive is paired with the control that proves it can fail — the point of this
watchdog is that the fleet had a budget nobody measured, and a watchdog whose first run
is green is a watchdog nobody has tested.
"""
import importlib.util
import json
import pathlib
import time

import pytest

_spec = importlib.util.spec_from_file_location(
    "cbw", pathlib.Path(__file__).with_name("context-budget-watch.py"))
cbw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cbw)

DAY = 86400


# ----------------------------------------------------------------- growth_per_day

def test_growth_rate_matches_the_real_root_series():
    """The measured root chain: 8,975 chars on 09-01 -> 30,597 on 09-12 is ~1,966/day."""
    t0 = 1_788_000_000
    rate = cbw.growth_per_day([(t0, 8975), (t0 + 11 * DAY, 30597)])
    assert rate == pytest.approx(1965.6, abs=1.0)


def test_growth_rate_is_none_without_two_points():
    assert cbw.growth_per_day([]) is None
    assert cbw.growth_per_day([(1_788_000_000, 8975)]) is None


def test_growth_rate_is_none_when_every_point_shares_a_timestamp():
    t = 1_788_000_000
    assert cbw.growth_per_day([(t, 100), (t, 900)]) is None


def test_growth_rate_uses_the_trailing_window_not_the_whole_history():
    """A flat ancient history must not dilute a steep recent trend."""
    now = int(time.time())
    old = [(now - 90 * DAY, 1000), (now - 80 * DAY, 1010)]
    recent = [(now - 4 * DAY, 1010), (now, 9010)]
    assert cbw.growth_per_day(old + recent) == pytest.approx(2000.0, abs=1.0)


# ----------------------------------------------------------------- days_to_breach

def test_days_to_breach_arithmetic():
    assert cbw.days_to_breach(30597, 40000, 1966) == pytest.approx(4.78, abs=0.05)
    assert cbw.days_to_breach(30597, 60000, 1966) == pytest.approx(14.97, abs=0.05)


def test_days_to_breach_none_when_not_growing_or_no_cap():
    assert cbw.days_to_breach(30597, 40000, 0) is None
    assert cbw.days_to_breach(30597, 40000, -50) is None
    assert cbw.days_to_breach(30597, None, 1966) is None


def test_days_to_breach_floors_at_zero_when_already_past():
    assert cbw.days_to_breach(45000, 40000, 1000) == 0.0


# ----------------------------------------------------------------------- evaluate

def _green_worker():
    return {"soul": 2500, "skills_index": 2000, "memory": 1000, "user": 900,
            "ctx_cap": 40000, "growth_per_day": 0.0, "days_to_breach": None,
            "soul_truncated": False}


def test_control_a_worker_inside_budget_reports_nothing():
    """The control. If this ever fails, no other assertion here means anything."""
    assert cbw.evaluate("bob", _green_worker()) == []


def test_a_single_slot_over_budget_is_caught():
    m = _green_worker() | {"skills_index": 5264}
    findings = cbw.evaluate("rodge", m)
    assert any("skills_index 5264 > 3000" in f for f in findings)


def test_total_is_caught_even_when_every_slot_passes():
    """The trap this watchdog exists for: four green slots, one bloated prompt."""
    m = {"soul": 2900, "skills_index": 2900, "memory": 2100, "user": 1300,
         "ctx_cap": 40000, "growth_per_day": 0.0, "days_to_breach": None,
         "soul_truncated": False}
    findings = cbw.evaluate("bob", m)
    assert not any(f.startswith(("soul ", "skills_index ", "memory ", "user ")) for f in findings)
    assert any("TOTAL owned 9200 > 8000" in f for f in findings)


def test_root_gets_the_larger_budget():
    """Same measurements: fine for root, over budget for a worker."""
    m = {"soul": 11000, "skills_index": 2000, "memory": 1000, "user": 900,
         "ctx_cap": 60000, "growth_per_day": 0.0, "days_to_breach": None,
         "soul_truncated": False}
    assert sum(m[s] for s in cbw.SLOTS) <= cbw.BUDGETS["root"]["total"]
    assert cbw.evaluate("root", m) == []
    assert cbw.evaluate("bob", m) != []


def test_roots_total_budget_still_binds():
    """Root is not exempt — every slot inside its cap, the total is not."""
    m = {"soul": 11900, "skills_index": 5900, "memory": 2100, "user": 1300,
         "ctx_cap": 60000, "growth_per_day": 0.0, "days_to_breach": None,
         "soul_truncated": False}
    findings = cbw.evaluate("root", m)
    assert not any(f.startswith(("soul ", "skills_index ", "memory ", "user ")) for f in findings)
    assert any("TOTAL owned 21200 > 15000" in f for f in findings)


def test_projected_breach_is_reported_before_it_happens():
    m = _green_worker() | {"days_to_breach": 4.8, "growth_per_day": 1966, "ctx_cap": 40000}
    assert any("in 4.8 days" in f for f in cbw.evaluate("bob", m))


def test_a_distant_projected_breach_is_not_reported():
    m = _green_worker() | {"days_to_breach": 400.0, "growth_per_day": 3}
    assert cbw.evaluate("bob", m) == []


def test_live_truncation_is_reported_separately_from_the_projection():
    m = _green_worker() | {"soul_truncated": True, "soul": 41000, "ctx_cap": 40000}
    assert any("SOUL IS TRUNCATED NOW" in f for f in cbw.evaluate("bob", m))


# ------------------------------------------------------------------------- render

def test_render_is_empty_when_every_profile_is_green():
    results = {"bob": (_green_worker(), []), "karl": (_green_worker(), [])}
    assert cbw.render(results) == ""


def test_render_names_the_breaching_profile_and_lists_every_profile():
    results = {"bob": (_green_worker(), []),
               "rodge": (_green_worker() | {"skills_index": 5264},
                         cbw.evaluate("rodge", _green_worker() | {"skills_index": 5264}))}
    out = cbw.render(results)
    assert "1 of 2 profiles over budget" in out
    assert "rodge:" in out
    assert "bob" in out                      # context: green rows still shown
    assert ".hermes.md" in out               # the destinations footer


# ----------------------------------------------------- measure / main on a fake fleet

def _fleet(tmp_path, soul_chars, *, cap=40000):
    home = tmp_path / ".hermes"
    (home / "profiles" / "bob" / "memories").mkdir(parents=True)
    (home / "SOUL.md").write_text("r" * 1000)
    (home / "profiles" / "bob" / "SOUL.md").write_text("x" * soul_chars)
    (home / "profiles" / "bob" / "memories" / "USER.md").write_text("u" * 200)
    return home


def test_measure_reads_the_slots_off_disk(tmp_path):
    home = _fleet(tmp_path, 2500)
    m = cbw.measure(home / "profiles" / "bob", "bob")
    assert m["soul"] == 2500
    assert m["user"] == 200
    assert m["memory"] == 0            # absent file is zero, not a crash


def test_measure_survives_a_missing_profile_dir(tmp_path):
    m = cbw.measure(tmp_path / "nope", "ghost")
    assert m["soul"] == 0 and m["skills_index"] == 0


def test_growth_is_computed_from_the_bak_chain(tmp_path):
    home = _fleet(tmp_path, 9000)
    soul = home / "profiles" / "bob" / "SOUL.md"
    old = soul.with_name("SOUL.md.bak-old")
    old.write_text("x" * 1000)
    now = time.time()
    import os
    os.utime(old, (now - 4 * DAY, now - 4 * DAY))
    os.utime(soul, (now, now))
    m = cbw.measure(soul.parent, "bob")
    assert m["growth_per_day"] == pytest.approx(2000.0, abs=5.0)


def test_main_is_silent_on_a_green_fleet_and_loud_on_a_fat_one(tmp_path, capsys, monkeypatch):
    home = _fleet(tmp_path, 2500)
    state = tmp_path / "state.json"
    monkeypatch.setattr(cbw, "STATE", state)
    assert cbw.main.__call__ is not None
    import sys
    monkeypatch.setattr(sys, "argv", ["x", "--home", str(home), "--no-state"])
    cbw.main()
    assert capsys.readouterr().out.strip() == ""          # CONTROL: green is silent

    (home / "profiles" / "bob" / "SOUL.md").write_text("x" * 9000)
    cbw.main()
    out = capsys.readouterr().out
    assert "over budget" in out and "bob" in out


def test_an_unchanged_breach_is_not_reported_again(tmp_path, capsys, monkeypatch):
    home = _fleet(tmp_path, 9000)
    state = tmp_path / "state.json"
    monkeypatch.setattr(cbw, "STATE", state)
    import sys
    monkeypatch.setattr(sys, "argv", ["x", "--home", str(home)])
    cbw.main()
    assert "over budget" in capsys.readouterr().out       # first time: reported
    cbw.main()
    assert capsys.readouterr().out.strip() == ""          # second time: deduped
    (home / "profiles" / "bob" / "SOUL.md").write_text("x" * 30000)
    cbw.main()
    assert "over budget" in capsys.readouterr().out       # changed: reported again
    assert json.loads(state.read_text())["fingerprint"]
