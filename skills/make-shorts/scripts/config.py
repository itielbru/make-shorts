"""Settings and secrets in a dotenv-style file under the user's home.

Location is the same on every OS: ~/.config/make-shorts/.env. Precedence is
environment variable, then file, then default. Writes are atomic and keep every
unrelated line and comment. Secrets are never printed or put on command lines.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from messages import UserFacingError

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


def _config_dir() -> Path:
    # Resolved on every call so tests (and users) can redirect the home dir.
    return Path.home() / ".config" / "make-shorts"


def config_dir() -> Path:
    return _config_dir()


CONFIG_DIR = _config_dir()


def _config_file() -> Path:
    return _config_dir() / ".env"


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _read_text() -> str:
    path = _config_file()
    if not path.exists():
        return ""
    return _decode(path.read_bytes())


def _parse_value(raw: str, line_no: int) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end == -1:
            raise UserFacingError("config_error", line=line_no)
        return raw[1:end]
    cut = raw.find(" #")
    return (raw[:cut] if cut != -1 else raw).strip()


def _parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        name = name.strip()
        if not _NAME_RE.match(name):
            continue
        values[name] = _parse_value(raw, line_no)  # last assignment wins
    return values


def get_setting(name: str, default: str | None = None) -> str | None:
    """Environment variable, then config file, then default."""
    env = os.environ.get(name)
    if env:
        return env
    file_value = _parse(_read_text()).get(name)
    if file_value:
        return file_value
    return default


def load_secret(name: str) -> str | None:
    return get_setting(name)


def _format_value(name: str, value: str) -> str:
    if "\n" in value or "\r" in value or ('"' in value and "'" in value):
        raise UserFacingError("bad_setting_value", name=name)
    if any(c in value for c in " #\"'") or value != value.strip():
        quote = "'" if '"' in value else '"'
        return f"{quote}{value}{quote}"
    return value


def write_settings(updates: dict[str, str]) -> None:
    """Update settings atomically, preserving unrelated lines and comments."""
    for name in updates:
        if not _NAME_RE.match(name):
            raise UserFacingError("bad_setting_name", name=name)
    rendered = {name: _format_value(name, value) for name, value in updates.items()}

    lines = _read_text().splitlines()
    pending = dict(rendered)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.partition("=")[0].strip()
        if name in pending:
            lines[i] = f"{name}={pending.pop(name)}"
    lines.extend(f"{name}={value}" for name, value in pending.items())

    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def has_setting(name: str) -> bool:
    """Whether a setting has a non-empty value. Never returns or prints the value itself."""
    return bool(get_setting(name))


def prepare_key_file(name: str) -> Path:
    """Make sure the settings file has an empty `NAME=` line for the user to fill in. Keeps every other line."""
    if f"{name}=" not in "".join(line.replace(" ", "") for line in _read_text().splitlines()):
        write_settings({name: ""})
    return _config_file()
