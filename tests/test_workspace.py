import re

import workspace

INVALID = re.compile(r'[<>:"/\\|?*]')


def test_final_filename_keeps_hebrew_and_strips_windows_invalid_chars():
    name = workspace.final_filename('מה זה חרדה? "הסבר": קצר', 1)
    assert not INVALID.search(name)
    assert "חרדה" in name and "הסבר" in name
    assert name.endswith(".mp4")
    assert name.startswith("01")


def test_sanitized_hebrew_title_can_be_written_to_disk(hebrew_tmp):
    name = workspace.final_filename('מה זה חרדה? "הסבר": קצר', 2)
    path = hebrew_tmp / name
    path.write_bytes(b"x")
    assert path.exists()


def test_sanitize_component_avoids_reserved_names_and_trailing_dots():
    assert workspace.sanitize_component("CON") != "CON"
    assert workspace.sanitize_component("nul") != "nul"
    assert workspace.sanitize_component("שלום. ") == "שלום"
    assert workspace.sanitize_component('???') == "clip"
    assert workspace.sanitize_component("") == "clip"


def test_final_filename_length_capped_at_100():
    name = workspace.final_filename("א" * 300, 3)
    assert len(name) <= 100
    assert name.endswith(".mp4")


def test_safe_slug_is_ascii_only():
    assert workspace.safe_slug("מה זה חרדה?") == "clip"
    assert workspace.safe_slug("My Great Video!!") == "My-Great-Video"
    assert workspace.safe_slug("My Great Video!!").isascii()


def test_workspace_name_is_ascii_even_when_parent_is_hebrew(hebrew_tmp):
    ws = workspace.Workspace.create(hebrew_tmp)
    assert ws.path.parent == hebrew_tmp
    assert ws.path.name.isascii()
    assert ws.path.name.startswith("ms-")
    assert ws.path.is_dir()


def test_default_parent_is_system_temp():
    import tempfile
    from pathlib import Path

    ws = workspace.Workspace.create()
    try:
        assert ws.path.parent == Path(tempfile.gettempdir())
    finally:
        ws.cleanup()


def test_cleanup_removes_directory(hebrew_tmp):
    ws = workspace.Workspace.create(hebrew_tmp)
    (ws.path / "file.txt").write_text("x", encoding="utf-8")
    ws.cleanup()
    assert not ws.path.exists()
