"""Audio extraction and chunk planning for cloud transcription.

Cloud services cap upload size, so audio is a small mono mp3 (about 480 KB per
minute) and long recordings are cut into equal-duration chunks.
"""
from __future__ import annotations

import math
import re
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


def detect_silences(path: Path, noise_db: int = -35, min_s: float = 0.3) -> list[tuple[float, float]]:
    """Silent stretches as (start, end) seconds; a silence that runs to the end ends at the file's end."""
    result = run_text(
        ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path),
         "-af", f"silencedetect=n={noise_db}dB:d={min_s}", "-f", "null", "-"],
        timeout=1800,
    )
    starts = [max(0.0, float(x)) for x in re.findall(r"silence_start:\s*(-?[0-9.]+)", result.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(-?[0-9.]+)", result.stderr)]
    if len(ends) < len(starts):
        ends.append(probe_duration(path))
    return [(round(s, 3), round(e, 3)) for s, e in zip(starts, ends)]


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


def quietest_in(energy: list[float], start: float, step: float = 0.01) -> float:
    """Time of the quietest moment in an energy profile (one value per `step` seconds from `start`),
    smoothed over three values so a single silent sample does not win over a real pause."""
    if not energy:
        return start
    if len(energy) < 3:
        return start + step * min(range(len(energy)), key=lambda i: energy[i])
    smooth = [(energy[i - 1] + energy[i] + energy[i + 1]) / 3 for i in range(1, len(energy) - 1)]
    best = min(range(len(smooth)), key=lambda i: smooth[i]) + 1
    return start + best * step


def waveform_quietest(src: Path, step: float = 0.01):
    """A function (lo, hi) -> time of the quietest moment of the audio between lo and hi seconds.
    Reads only that short window from the source, as 8 kHz mono samples.
    None when numpy is missing: the caller then cuts by word timing and pauses alone."""
    import subprocess

    try:
        import numpy as np
    except ImportError:
        return None

    rate = 8000
    hop = int(rate * step)

    def quietest(lo: float, hi: float) -> float:
        if hi - lo < 3 * step:
            return (lo + hi) / 2
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}",
             "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
            capture_output=True, timeout=60,
        )
        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
        if proc.returncode != 0 or len(samples) < 3 * hop:
            return (lo + hi) / 2
        frames = samples[: len(samples) // hop * hop].reshape(-1, hop)
        energy = [float(v) for v in np.sqrt(np.mean(frames ** 2, axis=1))]
        return quietest_in(energy, lo + step / 2, step)

    return quietest
