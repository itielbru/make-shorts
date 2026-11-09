"""Quality checks after rendering: frames to look at, plus automatic checks.

The visual judgement is Claude's: the report lists frame files and says every one
must be viewed before any claim about captions or framing. These helpers only
produce the frames, a contact sheet, and the checks a machine can do reliably.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from audio import probe_duration
from captions import Block
from messages import UserFacingError
from runtime import run_text
from selection import Clip
from transcript import Transcript

TILE_W = 270
SHEET_COLS = 4
SHEET_MAX_TILES = 8


@dataclass
class Frame:
    path: Path
    t: float
    reason: str


@dataclass
class Finding:
    kind: str
    message_he: str
    t: float | None = None


def caption_cue_times(blocks: list[Block]) -> list[float]:
    """The middle of every caption block: the moment its text is on screen."""
    return [round((b.start + b.end) / 2, 6) for b in blocks]


def extract_frames(video: Path, times: list[float], out_dir: Path, reason: str = "כתובית") -> list[Frame]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = max(0.0, probe_duration(video) - 0.1)
    frames: list[Frame] = []
    for i, t in enumerate(times):
        t = min(t, limit)
        out = out_dir / f"frame_{i:02d}.png"
        result = run_text(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y", "-ss", f"{t:.3f}", "-copyts",
             "-i", str(video), "-frames:v", "1", "-vf", "showinfo", str(out)],
            timeout=120,
        )
        if result.returncode != 0 or not out.exists():
            raise UserFacingError("unknown", detail="חילוץ פריים נכשל: " + result.stderr.strip()[-200:])
        match = re.search(r"pts_time:\s*([0-9.]+)", result.stderr)
        frames.append(Frame(out, float(match.group(1)) if match else t, reason))
    return frames


def sheet_selection(frames: list[Frame]) -> list[Frame]:
    """At most 8 frames, evenly spread and always including the first and last."""
    n = len(frames)
    if n <= SHEET_MAX_TILES:
        return list(frames)
    indices = sorted({round(i * (n - 1) / (SHEET_MAX_TILES - 1)) for i in range(SHEET_MAX_TILES)})
    return [frames[i] for i in indices]


def contact_sheet(frames: list[Frame], out: Path) -> Path:
    chosen = sheet_selection(frames)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / "sheet_tmp"
    tmp.mkdir(exist_ok=True)
    try:
        for i, frame in enumerate(chosen):
            shutil.copy(frame.path, tmp / f"f{i:02d}.png")
        rows = max(2, -(-len(chosen) // SHEET_COLS))
        result = run_text(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1", "-i", "f%02d.png",
             "-vf", f"scale={TILE_W}:-2,tile={SHEET_COLS}x{rows}:padding=4:margin=4:color=black",
             "-frames:v", "1", str(out.resolve())],
            timeout=120, cwd=tmp,
        )
        if result.returncode != 0 or not out.exists():
            raise UserFacingError("unknown", detail="יצירת הגיליון המקדים נכשלה: " + result.stderr.strip()[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _raw16(path: Path) -> bytes | None:
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf", "scale=16:16,format=rgb24",
         "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 and len(proc.stdout) == 16 * 16 * 3 else None


def frame_delta(a: Path, b: Path) -> float:
    """Mean absolute difference (0-255) between 16x16 RGB thumbnails."""
    ra, rb = _raw16(a), _raw16(b)
    if ra is None or rb is None:
        return 255.0  # unreadable: treat as different so nothing is dropped
    return sum(abs(x - y) for x, y in zip(ra, rb)) / len(ra)


def dedupe_frames(frames: list[Frame], threshold: float = 2.0) -> list[Frame]:
    kept: list[Frame] = []
    for frame in frames:
        if not kept or frame_delta(kept[-1].path, frame.path) > threshold:
            kept.append(frame)
    return kept


def _even_sample(frames: list[Frame], limit: int) -> list[Frame]:
    if len(frames) <= limit:
        return frames
    if limit == 1:
        return frames[:1]
    indices = sorted({round(i * (len(frames) - 1) / (limit - 1)) for i in range(limit)})
    return [frames[i] for i in indices]


def preview_frames(
    video: Path, out_dir: Path, *, scene_threshold: float = 0.20, dedup_threshold: float = 2.0,
    max_frames: int = 50, width: int = 512,
) -> list[Frame]:
    """Frames at scene changes (uniform sampling when there are few), deduplicated and capped."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video)

    def grab(select: str, prefix: str, reason: str) -> list[Frame]:
        result = run_text(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y", "-i", str(video),
             "-vf", f"{select},scale={width}:-2,showinfo", "-fps_mode", "vfr",
             str(out_dir / f"{prefix}_%04d.jpg")],
            timeout=1800,
        )
        times = [float(m) for m in re.findall(r"pts_time:\s*([0-9.]+)", result.stderr)]
        files = sorted(out_dir.glob(f"{prefix}_*.jpg"))
        return [Frame(f, t, reason) for f, t in zip(files, times)]

    frames = grab(f"select='eq(n\\,0)+gt(scene\\,{scene_threshold})'", "scene", "שינוי סצנה")
    if len(frames) < 8:
        for f in frames:
            f.path.unlink(missing_ok=True)
        rate = max(8, min(max_frames, int(duration // 5) or 8)) / max(duration, 1.0)
        frames = grab(f"fps={rate:.6f}", "uniform", "דגימה אחידה")
    frames = _even_sample(dedupe_frames(frames, dedup_threshold), max_frames)
    keep = {f.path for f in frames}
    for leftover in out_dir.glob("*.jpg"):
        if leftover not in keep:
            leftover.unlink(missing_ok=True)
    return frames


def check_boundaries(clip: Clip, t: Transcript) -> list[Finding]:
    findings = []
    for w in t.words:
        if w.start + 0.02 < clip.start < w.end - 0.02:
            findings.append(Finding("mid_word_start", "החיתוך בתחילת הקליפ נופל באמצע מילה.", clip.start))
        if w.start + 0.02 < clip.end < w.end - 0.02:
            findings.append(Finding("mid_word_end", "החיתוך בסוף הקליפ נופל באמצע מילה.", clip.end))
    return findings


def _has_audio(video: Path) -> bool:
    result = run_text(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video)],
        timeout=30,
    )
    return "audio" in result.stdout


def auto_checks(video: Path, expected_duration: float, clip: Clip, t: Transcript) -> list[Finding]:
    findings: list[Finding] = []
    actual = probe_duration(video)
    if abs(actual - expected_duration) > 0.3:
        findings.append(Finding(
            "duration_mismatch",
            f"אורך הקליפ ({actual:.1f} שניות) שונה מהמתוכנן ({expected_duration:.1f}).",
        ))

    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(video),
           "-vf", "blackdetect=d=0.5:pix_th=0.10"]
    audio = _has_audio(video)
    if audio:
        cmd += ["-af", "silencedetect=n=-45dB:d=0.5,volumedetect"]
    cmd += ["-f", "null", "-"]
    log = run_text(cmd, timeout=1800).stderr

    for start in re.findall(r"black_start:\s*([0-9.]+)", log):
        findings.append(Finding("black_frames", f"יש מסך שחור בקליפ סביב שנייה {float(start):.0f}. כדאי לבדוק אם זה בכוונה.", float(start)))

    if audio:
        starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[0-9.]+)", log)]
        ends = [float(x) for x in re.findall(r"silence_end:\s*(-?[0-9.]+)", log)]
        if starts and starts[0] <= 0.1:
            findings.append(Finding("silent_start", "הקליפ מתחיל בשקט. כדאי לקצר את ההתחלה או לבחור התחלה אחרת.", 0.0))
        if starts:
            last_end = ends[-1] if len(ends) == len(starts) else actual
            if last_end >= actual - 0.1:
                findings.append(Finding("silent_end", "הקליפ מסתיים בשקט. כדאי לקצר את הסוף.", starts[-1]))
        mean = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", log)
        peak = re.search(r"max_volume:\s*(-?[0-9.]+) dB", log)
        if mean and float(mean.group(1)) < -35:
            findings.append(Finding("too_quiet", "הקול בקליפ שקט מאוד. כדאי לבדוק את ההקלטה."))
        if peak and float(peak.group(1)) >= -0.1:
            findings.append(Finding("clipping", "הקול בקליפ חזק מדי ועלול להישמע מעוות."))

    findings.extend(check_boundaries(clip, t))
    return findings


def _mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def qa_report_markdown(frames: list[Frame], sheet: Path | None, findings: list[Finding]) -> str:
    lines = ["# בדיקת איכות", ""]
    if sheet is not None:
        lines += [f"גיליון מקדים: `{sheet}`", ""]
    lines += ["## פריימים לצפייה"]
    lines += [f"- `{f.path}` (t={_mmss(f.t)}, {f.reason})" for f in frames]
    lines += ["", "## ממצאים אוטומטיים"]
    lines += [f"- {f.message_he}" for f in findings] if findings else ["לא נמצאו בעיות אוטומטיות."]
    lines += [
        "",
        "> צפה בכל פריים ברשימה לפני שתקבע שהכתוביות והמסגור תקינים. "
        "אל תסיק על מה שנראה בווידאו רק מהתמלול.",
    ]
    return "\n".join(lines) + "\n"


MIN_SHOT_S = 1.0


AV_GAP_S = 0.15        # a frame or two of padding is normal; the L-cut bug gave half a second
# Mean grey difference of a frame the render repeated: identical, so ~0. A still shot in a ~1 Mbps
# source was measured at 0.02-0.05 per frame and must not count, or the check blames the render.
DUPLICATE_DIFF = 0.005


def duplicate_runs(diffs: list[float], fps: float, min_run: int = 2) -> list[tuple[float, int]]:
    """(time, count) of runs of repeated frames. diffs[k] compares frame k+1 with frame k.
    The source never repeats a frame exactly, so a run means the render held a picture."""
    runs: list[tuple[float, int]] = []
    k = 0
    while k < len(diffs):
        if diffs[k] < DUPLICATE_DIFF:
            j = k
            while j < len(diffs) and diffs[j] < DUPLICATE_DIFF:
                j += 1
            if j - k >= min_run:
                runs.append((round((k + 1) / fps, 3), j - k))
            k = j
        else:
            k += 1
    return runs


def render_findings(video_s: float, audio_s: float | None, duplicate_runs: list[tuple[float, int]]) -> list[str]:
    """Warnings about faults the render itself can cause, unlike the source's own look:
    picture and sound of different length (they drift apart), and frames held in place."""
    out: list[str] = []
    if audio_s is not None and abs(video_s - audio_s) > AV_GAP_S:
        out.append(f"התמונה ({video_s:.1f} שניות) והקול ({audio_s:.1f} שניות) לא באותו אורך, אז הם לא מסונכרנים. זו תקלת רינדור.")
    for t, count in duplicate_runs:
        # a still title card or a variable-frame-rate source repeats frames too; only the
        # comparison with the source tells which
        out.append(f"התמונה נעצרת ב-{t:.1f} למשך {count} פריימים זהים, והקול ממשיך. "
                   f"בדוק עם diagnose אם זה כך גם במקור; אם לא, זו תקלת רינדור.")
    return out


def sound_timestamp_findings(pts: list[float], frame_s: float) -> list[str]:
    """Sound packets must follow each other evenly (one AAC frame apart). Decoding to samples hides
    bad timestamps, but players follow them: seen in real clips, 2.4 s of sound stamped into 0.6 s
    and then a gap made the picture stall and drift from the sound."""
    if len(pts) < 2:
        return ["לא הצלחתי לקרוא את חותמות הזמן של הקול, אז הבדיקה לא רצה. אל תניח שהקליף תקין."]
    bad = [a for a, b in zip(pts, pts[1:-1]) if abs((b - a) - frame_s) > 0.25 * frame_s]
    if not bad:
        return []
    return [f"חותמות הזמן של הקול לא אחידות ({len(bad)} מקומות, הראשון ב-{bad[0]:.1f}), "
            "ולכן נגנים עלולים לעצור את התמונה או לאבד סנכרון. זו תקלת רינדור."]


def picture_timestamp_findings(pts: list[float], frame_s: float) -> list[str]:
    """Picture frames must follow each other one frame apart (pts sorted into display order).
    A hole or a pile-up in them makes a player hold the picture or skip."""
    if len(pts) < 2:
        return ["לא הצלחתי לקרוא את חותמות הזמן של התמונה, אז הבדיקה לא רצה. אל תניח שהקליף תקין."]
    ordered = sorted(pts)
    bad = [a for a, b in zip(ordered, ordered[1:]) if abs((b - a) - frame_s) > 0.25 * frame_s]
    if not bad:
        return []
    return [f"חותמות הזמן של התמונה לא אחידות ({len(bad)} מקומות, הראשון ב-{bad[0]:.1f}), "
            "ולכן נגנים עלולים לעצור את התמונה או לדלג. זו תקלת רינדור."]


AV_START_GAP_S = 0.1  # AAC adds ~21 ms of priming before 0; more than this is a real offset


def start_findings(video_start: float, audio_start: float | None) -> list[str]:
    if audio_start is None or abs(video_start - audio_start) <= AV_START_GAP_S:
        return []
    return [f"התמונה והקול מתחילים בזמנים שונים ({video_start:.2f} מול {audio_start:.2f}), "
            "ולכן הקליף יתחיל לא מסונכרן. זו תקלת רינדור."]


JOIN_CUT_TOLERANCE_S = 0.15


def motion_findings(
    cuts: list[float],
    joins: list[tuple[float, bool]],
    duration: float,
    crop_steps: list[tuple[float, float]],
    min_shot: float = MIN_SHOT_S,
) -> list[str]:
    """Plain-Hebrew warnings about what makes a short look jumpy, from the finished clip:
    shots too short to register (a flash), joins where the same person jumps in place,
    and a vertical crop stuck at the edge of the wide picture (the speaker may be cut off).
    Times are seconds on the clip's own timeline."""
    out: list[str] = []
    points = [0.0] + sorted(cuts) + [duration]
    for a, b in zip(points, points[1:]):
        if b - a < min_shot:
            out.append(f"צילום קצר של {b - a:.1f} שניות ב-{a:.1f}, נראה כמו הבזק.")
    for t, is_jump in joins:
        # the join time is counted from segment lengths and can sit a frame or two off the frame
        # where the picture really changes; a camera cut that close means the picture did change
        if is_jump and not any(abs(c - t) <= JOIN_CUT_TOLERANCE_S for c in cuts):
            out.append(f"קפיצה בחיבור ב-{t:.1f}: אותו אדם משני הצדדים, בלי זום שמסתיר את זה.")
    # crop_steps are accepted but not judged: a crop at the edge of the wide picture still keeps the
    # face inside the frame (crop_fraction clamps around it). Tried on a real run, the warning fired
    # in nearly every clip and pointed at nothing visible, so it was dropped.
    return out
