import subprocess
import sys

import pytest

import runtime
from messages import UserFacingError


def test_run_text_missing_executable():
    with pytest.raises(UserFacingError) as exc:
        runtime.run_text(["definitely-not-a-real-binary-xyz"], timeout=5)
    assert exc.value.key == "cannot_run"
    assert exc.value.exit_code == 2


def test_run_text_timeout():
    with pytest.raises(UserFacingError) as exc:
        runtime.run_text([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert exc.value.key == "timed_out"


def test_run_text_never_uses_shell(monkeypatch):
    seen = {}
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    result = runtime.run_text([sys.executable, "-c", "print('שלום')"], timeout=10)
    assert isinstance(seen["cmd"], list)
    assert not seen["kwargs"].get("shell")
    assert result.stdout.strip() == "שלום"
    assert result.returncode == 0


def test_secret_never_in_error_message():
    secret = "SECRET-TOKEN-123"
    with pytest.raises(UserFacingError) as exc:
        runtime.run_text(["definitely-not-a-real-binary-xyz", f"--key={secret}"], timeout=5)
    assert secret not in exc.value.message_he
    assert secret not in str(exc.value)
