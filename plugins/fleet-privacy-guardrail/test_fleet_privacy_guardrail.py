"""fleet-privacy-guardrail: the text renders, and it fails closed on a partial rule."""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "fpg", pathlib.Path(__file__).with_name("__init__.py"))
fpg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fpg)

CLAUSES = ("No human other than Richie Culph", "Google Drive exception",
           "never seek out, surface, or mention", "error: 500. notify account admin immediately")


def test_the_shipped_text_carries_all_four_clauses():
    t = fpg.guardrail_text()
    assert t is not None
    for c in CLAUSES:
        assert c in t, c


def test_it_fits_the_hosts_section_budget():
    assert len(fpg.guardrail_text()) < fpg.MAX_CHARS <= 4000


def test_render_returns_the_text():
    assert fpg.render({}) == fpg.guardrail_text()


def test_CONTROL_a_truncated_file_renders_NOTHING_not_half_a_rule(tmp_path, monkeypatch):
    partial = tmp_path / "GUARDRAIL.md"
    partial.write_text("## Human Privacy Guardrail\n\n1. Only clause one survives.\n")
    monkeypatch.setattr(fpg, "_TEXT_FILE", partial)
    assert fpg.guardrail_text() is None
    assert fpg.render({}) == ""


def test_CONTROL_a_missing_file_renders_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(fpg, "_TEXT_FILE", tmp_path / "gone.md")
    assert fpg.guardrail_text() is None
    assert fpg.render({}) == ""


def test_render_never_raises(monkeypatch):
    monkeypatch.setattr(fpg, "guardrail_text", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert fpg.render({}) == ""


def test_registers_the_section():
    seen = {}

    class Ctx:
        def register_system_prompt_section(self, sid, content, **kw):
            seen.update(id=sid, content=content, **kw)

    fpg.register(Ctx())
    assert seen["id"] == fpg.SECTION_ID
    assert callable(seen["content"])
    assert seen["position"] == "after_memory"


LIVE = pathlib.Path.home() / ".hermes"


@pytest.mark.skipif(not (LIVE / "SOUL.md").exists(), reason="live fleet not present")
def test_the_text_matches_what_the_souls_carry_today():
    """Byte-identical to the block being removed — this is a relocation, not a rewrite."""
    import re
    src = (LIVE / "SOUL.md").read_text(encoding="utf-8")
    m = re.search(r"(?ms)^## Human Privacy Guardrail.*?(?=^## |^---\s*$|\Z)", src)
    if not m:
        pytest.skip("root SOUL no longer carries the block (already migrated)")
    assert m.group(0).strip() == fpg.guardrail_text().strip()
