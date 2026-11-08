"""Audio extraction and chunk planning for cloud transcription.

Cloud services cap upload size, so audio is a small mono mp3 (about 480 KB per
minute) and long recordings are cut into equal-duration chunks.
"""
from __future__ import annotations

import math
from pathlib import Path

from messages import UserFacingError
from runtime import run_text
from transcript import Word

MAX_UPLOAD_BYTES = 24_000_000


def extract_audio_cmd(video: Path, out: Path) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-vn", "-acodec", "libmp3lame",
        "-ar", "16000", "-ac", "1", "-b:a", "64k", str(out),
    ]


def _run_ffmpeg(cmd: list[str], timeout: float) -> None:
    result = run_text(cmd, timeout=timeout)
    if result.returncode != 0:
        raise UserFacingError("unknown", detail="ffmpeg failed: " + result.stderr.strip()[:200])


def extract_audio(video: Path, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(extract_audio_cmd(video, out), timeout=3600)
    return out


def probe_duration(path: Path) -> float:
    result = run_text(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        timeout=30,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise UserFacingError("unknown", detail="could not read media duration") from None


def plan_chunks(total_bytes: int, duration_s: float, max_bytes: int) -> list[tuple[float, float]]:
    """Equal-duration chunks; the last one ends exactly at duration_s."""
    n = max(1, math.ceil(total_bytes / max_bytes))
    step = duration_s / n
    chunks = []
    for i in range(n):
        start = round(i * step, 6)
        end = duration_s if i == n - 1 else round((i + 1) * step, 6)
        chunks.append((start, end))
    return chunks


def split_audio(audio: Path, chunks: list[tuple[float, float]], out_dir: Path) -> list[Path]:
    """Cut constant-bitrate mp3 into chunk files (stream copy is safe for CBR mp3)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, (start, end) in enumerate(chunks):
        part = out_dir / f"chunk_{i:03d}.mp3"
        _run_ffmpeg(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{start:.3f}", "-i", str(audio), "-t", f"{end - start:.3f}",
             "-c", "copy", str(part)],
            timeout=600,
        )
        parts.append(part)
    return parts


def shift_words(words: list[Word], offset: float) -> list[Word]:
    return [Word(w.text, round(w.start + offset, 6), round(w.end + offset, 6)) for w in words]
