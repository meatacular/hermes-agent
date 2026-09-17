import base64, importlib.util, struct, zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location("aeg_under_test", HERE / "__init__.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


def _png(w, h):
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IEND", b"")


def _call(mod, data, name="cap.png", tool="kanban_attach"):
    return mod.on_pre_tool_call(tool_name=tool, args={"task_id": "t_x", "filename": name, "content_base64": base64.b64encode(data).decode()})


def test_zero_by_zero_png_is_refused():
    out = _call(_load(), _png(0, 0)); assert out and out["action"] == "block" and "0x0" in out["message"]


def test_empty_payload_is_left_to_the_kernel():
    # base64 of b"" is "", which the kernel's own _require_text rejects before any hook matters
    assert _call(_load(), b"") is None


def test_real_png_is_allowed():
    assert _call(_load(), _png(390, 844)) is None


def test_non_image_is_allowed():
    assert _call(_load(), b"just a log file\n", name="run.log") is None


def test_other_tools_untouched():
    assert _call(_load(), _png(0, 0), tool="kanban_comment") is None


def test_gif_and_jpeg_headers():
    mod = _load()
    assert mod.image_dimensions(b"GIF89a" + struct.pack("<HH", 0, 5)) == (0, 5)
    jpeg = b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 0, 100) + b"\x03" + b"\x00" * 9
    assert mod.image_dimensions(jpeg) == (100, 0)


def test_negative_control_neutered_parser_allows_stub():
    mod = _load(); mod.image_dimensions = lambda d: None
    assert _call(mod, _png(0, 0)) is None
