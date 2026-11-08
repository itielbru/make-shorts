"""Temporary working directory with safe names, and Windows-safe output file names.

Working directories get ASCII names (ms-<hex>) so ffmpeg filter paths never meet
odd characters. Output names may be Hebrew but are stripped of anything Windows
refuses in a file name.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
MAX_FILENAME = 100


def sanitize_component(text: str) -> str:
    cleaned = _INVALID.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ").strip()
    if not cleaned:
        return "clip"
    if cleaned.upper().split(".")[0] in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def safe_slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-") or "clip"


def final_filename(title: str, index: int, ext: str = ".mp4") -> str:
    prefix = f"{index:02d} - "
    budget = MAX_FILENAME - len(prefix) - len(ext)
    component = sanitize_component(title)[:budget].rstrip(". ").strip() or "clip"
    return f"{prefix}{component}{ext}"


@dataclass
class Workspace:
    path: Path

    @classmethod
    def create(cls, parent: Path | None = None) -> "Workspace":
        base = Path(parent) if parent is not None else Path(tempfile.gettempdir())
        path = base / f"ms-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True)
        return cls(path)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
