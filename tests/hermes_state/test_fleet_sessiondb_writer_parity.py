"""FLEET: SessionDB overrides the usage mixin's writers — their signatures must not drift.

2026-09-16 catch-up: upstream added a ``source`` kwarg to ``update_token_counts`` and every
caller of ``queue_token_counts`` started passing it. The fleet's ``SessionDB`` override did
not take it, the merge auto-resolved with no conflict, and the background writer swallowed
the TypeError as a warning — every per-call usage delta was dropped, which blinds the $1
card cap. These tests fail on that tree and pass on the fixed one.
"""
import inspect
import sqlite3

import hermes_state
from hermes_state import SessionDB
from hermes_state_usage import SessionUsageMixin


def _params(fn):
    return {p for p in inspect.signature(fn).parameters if p != "self"}


def test_sessiondb_writer_overrides_accept_every_mixin_kwarg():
    drift = {}
    for name, member in vars(SessionUsageMixin).items():
        if not callable(member) or name.startswith("__"):
            continue
        override = vars(SessionDB).get(name)
        if override is None or not callable(override):
            continue
        missing = _params(member) - _params(override)
        if missing:
            drift[name] = sorted(missing)
    assert not drift, f"SessionDB overrides lack mixin kwargs (queued writes will be dropped): {drift}"


def test_queued_delta_with_source_is_recorded(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", "cli", model="m1")
        db.queue_token_counts("s1", source="cli", input_tokens=100, output_tokens=5, model="m1",
                              billing_provider="openrouter", api_call_count=1, estimated_cost_usd=0.01)
        assert db.flush_token_counts(10)
    finally:
        db.close()
    c = sqlite3.connect(tmp_path / "state.db")
    try:
        assert c.execute("select input_tokens from sessions where id='s1'").fetchone()[0] == 100
        assert c.execute("select coalesce(sum(input_tokens),0) from session_model_usage").fetchone()[0] == 100
    finally:
        c.close()
