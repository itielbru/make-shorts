"""Environment preflight, hardware detection and transcription-engine advice.

Nothing here installs anything. It reports what is missing and gives the
commands a person can run, so the skill can explain and ask before installing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable

import config
from messages import UserFacingError
from runtime import CompletedText, run_text

_DISCRETE_GPU_RE = re.compile(
    r"nvidia|geforce|quadro|\brtx\b|\bgtx\b|radeon\s+(rx|pro|vii)|\barc\s*a\d", re.IGNORECASE
)

_POWERSHELL_HARDWARE = (
    "$p = Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores; "
    "$g = @(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM); "
    "$r = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory; "
    "[pscustomobject]@{cpu=$p.Name;cores=$p.NumberOfCores;gpus=$g;ram=$r} "
    "| ConvertTo-Json -Compress -Depth 3"
)


@dataclass
class EnvStatus:
    ffmpeg: bool
    ffprobe: bool
    ytdlp: bool
    deno: bool
    libass: bool
    missing: list[str] = field(default_factory=list)


@dataclass
class Hardware:
    cpu_name: str
    cores: int
    ram_gb: float
    has_discrete_gpu: bool


@dataclass
class EngineAdvice:
    engine: str
    reason_he: str
    warn_slow_local: bool


def _has_ass_filter(filters_output: str) -> bool:
    for line in filters_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "ass":
            return True
    return False


def check_environment(
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., CompletedText] = run_text,
) -> EnvStatus:
    ffmpeg = which("ffmpeg") is not None
    ffprobe = which("ffprobe") is not None
    ytdlp = which("yt-dlp") is not None
    deno = which("deno") is not None
    libass = False
    if ffmpeg:
        try:
            libass = _has_ass_filter(run(["ffmpeg", "-hide_banner", "-filters"], timeout=30).stdout)
        except UserFacingError:
            libass = False
    missing = []
    if not ffmpeg:
        missing.append("ffmpeg")
    if not ffprobe:
        missing.append("ffprobe")
    if ffmpeg and not libass:
        missing.append("libass")
    if not ytdlp:
        missing.append("yt-dlp")
    if not deno:
        missing.append("deno")
    return EnvStatus(ffmpeg, ffprobe, ytdlp, deno, libass, missing)


def is_discrete_gpu(name: str) -> bool:
    return bool(_DISCRETE_GPU_RE.search(name or ""))


def detect_hardware(
    run: Callable[..., CompletedText] = run_text, platform: str = sys.platform
) -> Hardware:
    if platform.startswith("win"):
        try:
            out = run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_HARDWARE],
                timeout=60,
            ).stdout
            data = json.loads(out)
            gpus = data.get("gpus") or []
            if isinstance(gpus, dict):
                gpus = [gpus]
            return Hardware(
                cpu_name=str(data.get("cpu") or "unknown"),
                cores=int(data.get("cores") or os.cpu_count() or 1),
                ram_gb=float(data.get("ram") or 0) / 1024**3,
                has_discrete_gpu=any(is_discrete_gpu(g.get("Name", "")) for g in gpus),
            )
        except (UserFacingError, ValueError, TypeError, AttributeError):
            pass
    ram_gb = 0.0
    try:
        ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass
    return Hardware("unknown", os.cpu_count() or 1, ram_gb, shutil.which("nvidia-smi") is not None)


def recommend_engine(hw: Hardware, cloud_allowed: bool) -> EngineAdvice:
    if hw.has_discrete_gpu:
        return EngineAdvice(
            "ivrit_local",
            "יש במחשב שלך כרטיס מסך נפרד, ולכן תמלול מקומי מהיר, פרטי וחינמי, והוא הכי מותאם לעברית.",
            False,
        )
    if cloud_allowed:
        return EngineAdvice(
            "groq",
            "במחשב שלך אין כרטיס מסך נפרד, ולכן תמלול מקומי יהיה איטי. "
            "Groq מהיר וחינמי (עם מגבלות), אבל האודיו נשלח אליהם, ולכן נדרשת הסכמה שלך.",
            False,
        )
    return EngineAdvice(
        "ivrit_local",
        "בחרת לא לשלוח אודיו החוצה, ולכן התמלול ירוץ על המחשב. "
        "בלי כרטיס מסך נפרד זה יכול לקחת הרבה זמן להקלטה ארוכה.",
        True,
    )


def install_hints_windows(status: EnvStatus) -> list[str]:
    hints: list[str] = []
    if any(item in status.missing for item in ("ffmpeg", "ffprobe", "libass")):
        hints.append("winget install --id Gyan.FFmpeg --exact")
    if "yt-dlp" in status.missing:
        hints.append("winget install --id yt-dlp.yt-dlp --exact")
    if "deno" in status.missing:
        hints.append("winget install --id DenoLand.Deno --exact")
    return hints


def is_first_run() -> bool:
    return config.get_setting("SETUP_COMPLETE") != "true"


def status_json(env: EnvStatus, hw: Hardware, advice: EngineAdvice, *, first_run: bool) -> dict:
    return {
        "first_run": first_run,
        "can_proceed": env.ffmpeg and env.ffprobe and env.libass,
        "url_input_ready": env.ytdlp and env.deno,
        "missing": list(env.missing),
        "hardware": asdict(hw),
        "advice": asdict(advice),
        "install_hints": install_hints_windows(env) if sys.platform.startswith("win") else [],
    }
