"""Subprocess and stdio helpers. No shell, UTF-8 everywhere."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from messages import UserFacingError


@dataclass
class CompletedText:
    stdout: str
    stderr: str
    returncode: int


def _program_name(cmd: list[str]) -> str:
    # Only the program name ever reaches a message: arguments may hold secrets.
    return Path(str(cmd[0])).name if cmd else ""


def run_text(cmd: list[str], *, timeout: float | None, cwd: Path | None = None) -> CompletedText:
    """Run a command without a shell and return decoded UTF-8 text."""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise UserFacingError("timed_out", program=_program_name(cmd)) from None
    except OSError:
        raise UserFacingError("cannot_run", program=_program_name(cmd)) from None
    return CompletedText(proc.stdout or "", proc.stderr or "", proc.returncode)


def configure_stdio() -> None:
    """Make stdout and stderr UTF-8 so Hebrew never crashes a print."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
