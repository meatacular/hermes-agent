"""FLEET (catch-up 2026-10-31): managed-worker usage must carry the fleet's aggregator attribution.

Upstream moved kanban workers into the profile owner (agent/managed_worker); their usage now reaches
state.db through ``worker.persist`` -> ``hermes_state_runtime._worker_usage`` ->
``_update_token_counts_in_transaction`` / ``_record_model_usage``, never through the fleet's
``SessionDB.update_token_counts``. These tests feed that path the payload production sends: the
upstream fields plus every key of ``agent.transports.chat_completions.usage_attribution``."""
import pytest
from hermes_state import SessionDB
import hermes_state_runtime as rt


class _Usage:
    def __init__(self):
        self.model_extra = {"native_tokens_prompt": 120, "native_tokens_cached": 80,
                            "cache_discount": -0.0004, "cost": 0.0123}


class _Resp:
    provider = "DeepInfra"
    usage = _Usage()
    model_extra = {}


def _scope(db):
    db.create_session('s', source='cli')
    epoch = rt.begin_runtime_epoch(db, instance_id='owner')
    scope = dict(epoch=epoch, execution_id='w', session_id='s', generation=0)
    rt.register_worker_execution(db, **scope, kind='compute', adoption_secret='private')
    return scope


def _attribution():
    from agent.transports.chat_completions import usage_attribution
    return usage_attribution(_Resp())


def test_main_usage_with_fleet_attribution_is_accepted_and_recorded(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        scope = _scope(db)
        attr = _attribution()
        assert set(attr) == {"provider_name", "native_tokens_prompt", "native_tokens_cached", "cache_discount", "total_cost"}
        payload = {'input_tokens': 13, 'output_tokens': 7, 'api_call_count': 1, 'model': 'm',
                   'billing_provider': 'openrouter', 'estimated_cost_usd': 0.25, **attr}
        rt.mutate_worker_execution(db, **scope, sequence=1, operation='usage.main', payload=payload)
        assert db.get_session('s')['input_tokens'] == 13
        with db._read_ctx() as conn:
            row = dict(conn.execute('SELECT * FROM session_model_usage').fetchone())
        assert row['provider_name'] == attr['provider_name'] == 'DeepInfra'
        for k in ("native_tokens_prompt", "native_tokens_cached", "cache_discount", "total_cost"):
            if k in row:
                assert row[k] == pytest.approx(attr[k]), k
    finally:
        db.close()


def test_auxiliary_usage_with_fleet_attribution_is_accepted(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        scope = _scope(db)
        payload = {'task': 'compression', 'model': 'aux', 'input_tokens': 3, **_attribution()}
        rt.mutate_worker_execution(db, **scope, sequence=1, operation='usage.auxiliary', payload=payload)
        with db._read_ctx() as conn:
            r = conn.execute("SELECT input_tokens, provider_name FROM session_model_usage WHERE task='compression'").fetchone()
        assert tuple(r) == (3, 'DeepInfra')
    finally:
        db.close()


def test_fleet_extras_are_still_validated(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        scope = _scope(db)
        with pytest.raises(rt.RuntimeStoreError, match='invalid_params'):
            rt.mutate_worker_execution(db, **scope, sequence=1, operation='usage.main',
                                       payload={'input_tokens': 1, 'native_tokens_prompt': -5})
        with pytest.raises(rt.RuntimeStoreError, match='invalid_params'):
            rt.mutate_worker_execution(db, **scope, sequence=1, operation='usage.main',
                                       payload={'input_tokens': 1, 'cache_discount': 'x'})
    finally:
        db.close()
