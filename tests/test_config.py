import os

import pytest

import config
from messages import UserFacingError


def _env_file(config_home):
    return config_home / ".config" / "make-shorts" / ".env"


def test_env_roundtrip_preserves_comments_and_unrelated_lines(config_home):
    config.write_settings({"WATCH_ENGINE": "groq"})
    path = _env_file(config_home)
    path.write_text("# my note\nUNRELATED=keep me\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    config.write_settings({"SETUP_COMPLETE": "true"})

    text = path.read_text(encoding="utf-8")
    assert "# my note" in text
    assert "UNRELATED=keep me" in text
    assert config.get_setting("WATCH_ENGINE") == "groq"
    assert config.get_setting("SETUP_COMPLETE") == "true"


def test_precedence_env_over_file_over_default(config_home, monkeypatch):
    assert config.get_setting("ENGINE_X", "dflt") == "dflt"
    config.write_settings({"ENGINE_X": "from-file"})
    assert config.get_setting("ENGINE_X", "dflt") == "from-file"
    monkeypatch.setenv("ENGINE_X", "from-env")
    assert config.get_setting("ENGINE_X", "dflt") == "from-env"


def test_write_is_atomic_and_0600_on_posix(config_home):
    config.write_settings({"A": "1"})
    config.write_settings({"B": "2"})
    path = _env_file(config_home)
    leftovers = [p.name for p in path.parent.iterdir() if p.name != ".env"]
    assert leftovers == []
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_rejects_key_with_invalid_name(config_home):
    with pytest.raises(UserFacingError) as exc:
        config.write_settings({"1bad key": "x"})
    assert exc.value.key == "bad_setting_name"
    assert exc.value.exit_code == 2


def test_reads_utf16_bom_file(config_home):
    path = _env_file(config_home)
    path.parent.mkdir(parents=True)
    path.write_bytes("FOO=bar\r\n".encode("utf-16"))
    assert config.get_setting("FOO") == "bar"


def test_load_secret_reads_environment_first(config_home, monkeypatch):
    config.write_settings({"GROQ_API_KEY": "file-key"})
    assert config.load_secret("GROQ_API_KEY") == "file-key"
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    assert config.load_secret("GROQ_API_KEY") == "env-key"
