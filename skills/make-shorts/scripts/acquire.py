"""Get a video to work on: a local file (used in place) or a downloaded link.

Only video is supported. An audio-only file is refused with a plain message.
yt-dlp is never updated here without the user's consent; we only explain.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from messages import UserFacingError
from runtime import run_text
from workspace import Workspace

YTDLP_TIMEOUT = 1800
FFPROBE_TIMEOUT = 30
_FORMAT = "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
_JS_RUNTIME_RE = re.compile(r"javascript runtime|js runtime|\bejs\b|challenge solver", re.IGNORECASE)
_403_RE = re.compile(r"\b403\b|forbidden", re.IGNORECASE)


@dataclass
class MediaInfo:
    path: Path
    title: str
    duration: float
    width: int
    height: int
    has_video: bool
    has_audio: bool


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    result = run_text(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=FFPROBE_TIMEOUT,
    )
    if result.returncode != 0:
        raise UserFacingError("unreadable_media")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise UserFacingError("unreadable_media") from None
    streams = data.get("streams") or []
    if not streams:
        raise UserFacingError("unreadable_media")
    videos = [
        s for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not videos:
        raise UserFacingError("audio_only_unsupported")
    durations = []
    for candidate in [data.get("format", {}).get("duration")] + [s.get("duration") for s in streams]:
        try:
            durations.append(float(candidate))
        except (TypeError, ValueError):
            continue
    tags = data.get("format", {}).get("tags") or {}
    title = str(tags.get("title") or path.stem)
    return MediaInfo(
        path=path,
        title=title,
        duration=max(durations) if durations else 0.0,
        width=int(videos[0].get("width") or 0),
        height=int(videos[0].get("height") or 0),
        has_video=True,
        has_audio=has_audio,
    )


def ytdlp_args(url: str, out_dir: Path) -> list[str]:
    template = str(out_dir).replace("%", "%%") + os.sep + "video.%(ext)s"
    return [
        "yt-dlp", "--no-playlist", "-N", "8", "-f", _FORMAT,
        "--merge-output-format", "mp4", "--no-write-subs", "--no-write-auto-subs",
        "--write-info-json", "-o", template, "--", url,
    ]


def diagnose_download_error(stderr: str) -> UserFacingError:
    if _JS_RUNTIME_RE.search(stderr):
        return UserFacingError("ytdlp_js_runtime")
    if _403_RE.search(stderr):
        return UserFacingError("ytdlp_403")
    last_line = (stderr.strip().splitlines() or ["unknown"])[-1][:200]
    return UserFacingError("download_failed", reason=last_line)


def _is_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _download(url: str, ws: Workspace) -> MediaInfo:
    out_dir = ws.path / "download"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = run_text(ytdlp_args(url, out_dir), timeout=YTDLP_TIMEOUT)
    if result.returncode != 0:
        raise diagnose_download_error(result.stderr)
    media = out_dir / "video.mp4"
    if not media.exists():
        candidates = [
            p for p in out_dir.glob("video.*")
            if p.suffix.lower() not in {".json", ".part", ".ytdl", ".vtt"} and p.stat().st_size > 0
        ]
        if not candidates:
            raise UserFacingError("download_failed", reason="לא נוצר קובץ וידאו")
        media = candidates[0]
    info = probe(media)
    info_json = out_dir / "video.info.json"
    if info_json.exists():
        try:
            title = json.loads(info_json.read_text(encoding="utf-8")).get("title")
            if title:
                info.title = str(title)
        except (OSError, ValueError):
            pass
    return info


def fetch(source: str, ws: Workspace) -> MediaInfo:
    if _is_url(source):
        return _download(source, ws)
    path = Path(source).expanduser()
    if not path.is_file():
        raise UserFacingError("file_not_found", path=str(path))
    return probe(path)
