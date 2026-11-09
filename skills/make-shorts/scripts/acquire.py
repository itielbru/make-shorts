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
# 1080p, H.264 preferred. The vertical crop keeps only a 9:16 strip of the wide picture: from 720p it
# is stretched 2.7x and fast motion smears (seen in a real clip at ~1 Mbps AV1); from 1080p it is 1.8x.
# H.264 decodes far faster than AV1 on a weak CPU, which also speeds up the render.
_FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba/bv*[height<=1080]+ba/b[height<=1080]/bv+ba/b"
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
    bit_rate: int = 0     # video bits per second, 0 when unknown
    fps: float = 25.0


# Seen on a real run: a 720p source at ~1 Mbps cropped to vertical smeared on every fast movement,
# and a face that barely moved looked frozen while the sound went on (the encoder hardly updates it).
MIN_HEIGHT = 1080
# Per pixel per frame. YouTube's own 1080p of a studio talk show is ~0.02 (1.1 Mbps) and, cropped to
# vertical, looked sharp: a calm picture needs few bits. Only far below that is it really starved.
MIN_BITS_PER_PIXEL = 0.012


def quality_warning(info: MediaInfo) -> str | None:
    """A plain-Hebrew warning when the source is too weak for a vertical crop, else None."""
    if not info.height or not info.width:
        return None
    if info.height < MIN_HEIGHT:
        stretch = 1920 / info.height
        return (f"הסרטון ברזולוציה {info.height}p. בחיתוך אנכי התמונה תוגדל פי {stretch:.1f}, "
                "ותנועות מהירות ייראו מרוחות. אם יש גרסה ב-1080p או יותר, עדיף לעבוד ממנה.")
    if info.bit_rate:
        bpp = info.bit_rate / (info.width * info.height * (info.fps or 25.0))
        if bpp < MIN_BITS_PER_PIXEL:
            return (f"הסרטון דחוס מאוד ({info.bit_rate / 1e6:.1f}Mbps). פנים שכמעט לא זזות עלולות להיראות "
                    "קפואות ותנועות מהירות מרוחות. אם יש גרסה באיכות גבוהה יותר, עדיף לעבוד ממנה.")
    return None


def _rate(stream: dict) -> float:
    num, _, den = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "").partition("/")
    try:
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 25.0
    return value if value > 0 else 25.0


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
    bit_rate = 0
    for candidate in (videos[0].get("bit_rate"), data.get("format", {}).get("bit_rate")):
        try:
            bit_rate = int(candidate)
            break
        except (TypeError, ValueError):
            continue
    return MediaInfo(
        bit_rate=bit_rate,
        fps=_rate(videos[0]),
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
