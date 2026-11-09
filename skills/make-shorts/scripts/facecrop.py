"""Face-aware vertical crop: follow whoever is on screen and speaking in a wide, multi-camera video.

For each rendered segment the video is sampled at 10 frames per second, camera cuts are found by
comparing small thumbnails, faces are found with OpenCV's YuNet model, and each shot gets one crop
position. When a shot shows several people the one whose mouth region moves most is chosen.
OpenCV and numpy are optional: without them the caller falls back to a fixed crop.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from captions import SKILL_ROOT
from runtime import run_text

MODEL_PATH = SKILL_ROOT / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 10
FRAME_WIDTH = 480
CUT_THRESHOLD = 18.0       # mean absolute thumbnail difference (0-255) that marks a camera cut
MIN_SHOT_S = 0.4
OUT_W, OUT_H = 1080, 1920


@dataclass
class FaceCluster:
    cx: float        # horizontal centre of the face, 0..1 of the frame width
    width: float     # face width as a fraction of the frame width
    frames: int      # in how many sampled frames this face was seen
    motion: float    # summed mouth-region motion over those frames


def available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return MODEL_PATH.exists()


def crop_fraction(face_cx: float, aspect: float) -> float:
    """Where (0 = far left, 1 = far right) the vertical window must sit to centre a face at `face_cx`."""
    scaled_w = OUT_H * aspect
    x = face_cx * scaled_w - OUT_W / 2
    return round(min(1.0, max(0.0, x / (scaled_w - OUT_W))), 3)


def choose_subject(clusters: list[FaceCluster], total_frames: int) -> FaceCluster | None:
    """The face to follow: seen often enough, then the one with the most mouth motion, else the biggest."""
    seen = [c for c in clusters if c.frames >= max(1, 0.3 * total_frames)]
    if not seen:
        return None
    if len(seen) == 1:
        return seen[0]
    by_motion = sorted(seen, key=lambda c: c.motion / c.frames, reverse=True)
    best, second = by_motion[0], by_motion[1]
    if best.motion / best.frames > 1.3 * (second.motion / second.frames) + 0.5:
        return best
    return max(seen, key=lambda c: c.width)


def fill_faceless(shots: list[tuple[float, float, float | None]]) -> list[tuple[float, float, float]]:
    """A shot without faces (an empty studio) borrows the crop of the shot before it, or the one after."""
    known = [s[2] for s in shots if s[2] is not None]
    first = known[0] if known else 0.5
    out: list[tuple[float, float, float]] = []
    previous = first
    for start, end, cx in shots:
        previous = cx if cx is not None else previous
        out.append((start, end, previous))
    return out


def steps_from_shots(shots: list[tuple[float, float, float]], min_shot_s: float = MIN_SHOT_S) -> list[tuple[float, float]]:
    """(start time, crop) steps. Very short shots keep the previous crop; equal neighbours are merged."""
    steps: list[tuple[float, float]] = []
    for start, end, cx in shots:
        if steps and (end - start < min_shot_s):
            continue
        if steps and abs(steps[-1][1] - cx) < 0.02:
            continue
        steps.append((start if steps else 0.0, cx))
    return steps or [(0.0, 0.5)]


FLASH_MARGIN_S = 0.25


CUT_SPIKE = 2.5  # a cut stands alone; fast motion spreads over several frames (measured: cuts 60-90, motion 18-24)


def cut_frames(diffs: list[float], threshold: float = CUT_THRESHOLD) -> list[int]:
    """Indices of the first frame of each new shot. diffs[k] compares frame k with frame k-1.
    A cut is a single spike: above the threshold and well above the frames on both sides."""
    out = []
    for k, d in enumerate(diffs):
        if k == 0 or d <= threshold:
            continue
        around = max(diffs[k - 1], diffs[k + 1] if k + 1 < len(diffs) else 0.0)
        if d >= CUT_SPIKE * around:
            out.append(k)
    return out


def cut_times(frames: list[int], fps: float) -> list[float]:
    """The moment the crop must switch: half a frame before the new shot's first frame,
    so the last frame of the old shot keeps the old crop and the first new frame gets the new one."""
    return [(k - 0.5) / fps for k in frames]


OUTER_MARGIN_S = 1.0  # seen in real clips: a 0.6-0.9 s shot at a clip's start or end reads as a flash


def trim_flashes(
    start: float, end: float, cuts: list[float], margin: float = FLASH_MARGIN_S,
    head: float | None = None, tail: float | None = None,
) -> tuple[float, float, list[float]]:
    """A camera cut right at the edge of a segment leaves a few frames of another shot (a flash).
    Move the segment edge to the cut instead. Returns the new edges and the cuts relative to the new start."""
    duration = end - start
    head_margin = margin if head is None else head
    tail_margin = margin if tail is None else tail
    head = [c for c in cuts if c <= head_margin]
    tail = [c for c in cuts if c >= duration - tail_margin and c not in head]
    if head and tail and min(tail) - max(head) < duration / 2:
        tail = []  # a short segment keeps most of itself rather than shrink to nothing
    if head and not tail and duration - max(head) < duration / 2:
        head = []
    shift = max(head) if head else 0.0
    new_end = start + (min(tail) if tail else duration)
    inner = [c - shift for c in cuts if c not in head and c not in tail]
    return start + shift, new_end, inner


def shot_of(times: list[float], cuts: list[float]) -> list[int]:
    """Which shot (0-based) each time falls in, given sorted cut times."""
    import bisect

    return [bisect.bisect_right(cuts, t) for t in times]


def _probe_fps(src: Path) -> float:
    result = run_text(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                       "-of", "csv=p=0", str(src)], timeout=60)
    try:
        num, _, den = result.stdout.strip().partition("/")
        return float(num) / float(den or 1)
    except ValueError:
        return 25.0


def _probe_start(src: Path) -> float:
    result = run_text(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
                       "-of", "csv=p=0", str(src)], timeout=60)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def first_frame_offset(start: float, fps: float, stream_start: float = 0.0) -> float:
    """How far after `start` the first decoded frame sits (frames are on a fixed grid)."""
    import math

    n = math.ceil((start - stream_start) * fps - 1e-3)
    return max(0.0, stream_start + n / fps - start)


def exact_cuts(src: Path, start: float, end: float, fps: float, absolute: bool = False) -> list[float]:
    """Camera cuts at full frame rate, from tiny grey thumbnails piped out of ffmpeg.

    Times count from the first decoded frame, the same clock the render's crop uses. With
    `absolute`, they count from `start` instead, which is what moving a segment edge needs:
    the first frame sits up to one frame after `start`, and ignoring that can keep one frame
    of the old shot."""
    import subprocess

    import numpy as np

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
         "-i", str(src), "-fps_mode", "passthrough", "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, timeout=max(120.0, (end - start) * 5),
    )
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = len(data) // 576
    if proc.returncode != 0 or n < 2:
        return []
    thumbs = data[: n * 576].reshape(n, 576).astype(np.float32)
    diffs = [0.0] + [float(v) for v in np.mean(np.abs(np.diff(thumbs, axis=0)), axis=1)]
    times = cut_times(cut_frames(diffs), fps)
    if absolute:
        offset = first_frame_offset(start, fps, _probe_start(src))
        times = [round(t + offset, 4) for t in times]
    return times


# Measured on a real podcast: people speaking move 3.5-7.6 (mean grey difference between frames
# 0.1 s apart at 480 px), a listener sitting still while someone off screen talks moves 1.0-2.8.
STILL_MOTION = 3.0
STILL_MIN_S = 0.8


def still_intervals(
    shots: list[tuple[float, float, float]], threshold: float = STILL_MOTION, min_len: float = STILL_MIN_S
) -> list[tuple[float, float]]:
    """(start, end) of shots that look frozen: long enough to notice, with almost no motion."""
    return [(s, e) for s, e, motion in shots if e - s >= min_len and 0 <= motion < threshold]


SNAP_REACH_S = 1.0


def snap_joins(
    segments: list[tuple[float, float]], cuts: list[list[float]], reach: float = SNAP_REACH_S
) -> list[tuple[float, float]]:
    """Video edges for each segment, so the picture changes at a real camera cut near each join.

    A join that falls just after a camera cut leaves a sliver of a new shot before the jump to the
    next part (two picture changes within a second). Instead, the picture switches to the next part
    at the camera cut while the audio keeps going to the join (an L-cut), or the other way around
    when the cut falls just after the join. Total length never changes. `cuts` are relative to each
    segment's start."""
    video = [list(s) for s in segments]
    for i in range(len(segments) - 1):
        length = segments[i][1] - segments[i][0]
        tail = [c for c in cuts[i] if 0 < length - c <= reach]
        head = [c for c in cuts[i + 1] if 0 < c <= reach]
        if tail:
            shift = -(length - max(tail))
        elif head:
            shift = min(head)
        else:
            continue
        video[i][1] += shift
        video[i + 1][0] += shift
    return [(a, b) for a, b in video]


PUNCH_IN = 1.08  # 1.15 read as too strong; 8% still breaks the "same picture" jump
# A camera cut and a jump are different questions. Real camera changes in podcasts measure 60+;
# the same person from a slightly moved camera measures around 18 and still looks like a jump.
JUMP_THRESHOLD = 35.0


def looks_like_jump(diff: float) -> bool:
    """Would a join with this picture difference look like the person jumped in place?"""
    return diff <= JUMP_THRESHOLD


def punch_in_zooms(same_shot: list[bool], strength: float = PUNCH_IN) -> list[float]:
    """Zoom per segment. A join inside one camera shot is a jump cut: the person jumps in place.
    Switching zoom at such a join (1.0 <-> 1.08) makes it read as a deliberate cut, the way editors do it."""
    zooms = [1.0]
    for same in same_shot:
        zooms.append((strength if zooms[-1] == 1.0 else 1.0) if same else zooms[-1])
    return zooms


def _thumb_at(src: Path, t: float):
    import subprocess

    import numpy as np

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(src),
         "-frames:v", "1", "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, timeout=60,
    )
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    return data[:576].astype(np.float32) if len(data) >= 576 else None


def same_shot_joins(src: Path, segments: list[tuple[float, float]], fps: float = 25.0) -> list[bool]:
    """For each join between segments: is the picture on both sides the same camera shot?"""
    import numpy as np

    flags = []
    for (_, end_a), (start_b, _) in zip(segments, segments[1:]):
        a, b = _thumb_at(src, end_a - 1.5 / fps), _thumb_at(src, start_b + 0.5 / fps)
        flags.append(a is not None and b is not None and looks_like_jump(float(np.mean(np.abs(a - b)))))
    return flags


def _read(path: Path):
    import cv2
    import numpy as np

    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def _detector():
    import cv2
    import numpy as np

    buffer = np.frombuffer(MODEL_PATH.read_bytes(), dtype=np.uint8)
    return cv2.FaceDetectorYN.create("onnx", buffer, np.empty(0, dtype=np.uint8), (FRAME_WIDTH, 270), 0.6, 0.3, 5000)


def _clusters_for_shot(frames: list[list[tuple[float, float, float]]]) -> list[FaceCluster]:
    clusters: list[FaceCluster] = []
    for faces in frames:
        for cx, width, motion in faces:
            target = next((c for c in clusters if abs(c.cx - cx) < 0.08), None)
            if target is None:
                clusters.append(FaceCluster(cx, width, 1, motion))
            else:
                n = target.frames
                target.cx = (target.cx * n + cx) / (n + 1)
                target.width = (target.width * n + width) / (n + 1)
                target.frames = n + 1
                target.motion += motion
    return clusters


def shot_motion(src: Path, start: float, end: float, cuts: list[float]) -> list[float]:
    """Mean grey difference between frames 0.1 s apart inside each shot (-1 when unknown)."""
    import subprocess

    import numpy as np

    size = 480 * 270
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
         "-i", str(src), "-vf", f"setpts=PTS-STARTPTS,fps={SAMPLE_FPS},scale=480:270,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, timeout=max(120.0, (end - start) * 5),
    )
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    frames = data[: len(data) // size * size].reshape(-1, size).astype(np.float32)
    which = shot_of([i / SAMPLE_FPS for i in range(len(frames))], cuts)
    out = []
    for s in range(len(cuts) + 1):
        diffs = [float(np.mean(np.abs(frames[i] - frames[i - 1]))) for i in range(1, len(frames))
                 if which[i] == s and which[i - 1] == s]
        out.append(sum(diffs) / len(diffs) if diffs else -1.0)
    return out


def analyze_segment(
    src: Path, start: float, end: float, ws_path: Path, index: int, fps: float = 25.0,
    head: float | None = None, tail: float | None = None,
) -> tuple[float, float, list[tuple[float, float]], list[tuple[float, float]]]:
    """(new start, new end, crop steps, still intervals) for one rendered segment; times are
    relative to the new start.

    Cuts are found at the exact frame, so the crop switches together with the picture,
    and a segment that starts or ends a few frames into another shot is trimmed to the cut.
    Still intervals are shots where nobody moves (a listener while someone off screen talks).
    """
    import cv2
    import numpy as np

    start, end, cuts = trim_flashes(start, end, exact_cuts(src, start, end, fps), head=head, tail=tail)
    bounds = [0.0] + cuts + [end - start]
    motions = shot_motion(src, start, end, cuts)
    stills = still_intervals([(bounds[k], bounds[k + 1], m) for k, m in enumerate(motions)])
    out_dir = ws_path / f"fc{index}"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir()
    result = run_text(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
         "-i", str(src), "-vf", f"fps={SAMPLE_FPS},scale={FRAME_WIDTH}:-2", "-q:v", "3", f"fc{index}/%04d.jpg"],
        timeout=max(120.0, (end - start) * 10), cwd=ws_path,
    )
    frames = sorted(out_dir.glob("*.jpg"))
    if result.returncode != 0 or not frames:
        shutil.rmtree(out_dir, ignore_errors=True)
        return start, end, [(0.0, 0.5)], stills
    detector = _detector()
    shots: list[list[list[tuple[float, float, float]]]] = [[] for _ in range(len(cuts) + 1)]
    boundaries = [0.0] + cuts
    which = shot_of([i / SAMPLE_FPS for i in range(len(frames))], cuts)
    previous_gray = None
    previous_shot = -1
    aspect = 16 / 9
    for i, path in enumerate(frames):
        image = _read(path)
        if image is None:
            continue
        h, w = image.shape[:2]
        aspect = w / h
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        cut = which[i] != previous_shot
        previous_shot = which[i]
        detector.setInputSize((w, h))
        _, faces = detector.detect(image)
        found: list[tuple[float, float, float]] = []
        for f in (faces if faces is not None else []):
            x, y, fw, fh = (float(v) for v in f[:4])
            motion = 0.0
            if previous_gray is not None and not cut:
                mx = (float(f[10]) + float(f[12])) / 2
                my = (float(f[11]) + float(f[13])) / 2
                x0, x1 = int(max(0, mx - 0.35 * fw)), int(min(w, mx + 0.35 * fw))
                y0, y1 = int(max(0, my - 0.25 * fw)), int(min(h, my + 0.25 * fw))
                if x1 > x0 and y1 > y0:
                    motion = float(np.mean(np.abs(gray[y0:y1, x0:x1].astype(np.float32) - previous_gray[y0:y1, x0:x1])))
            found.append(((x + fw / 2) / w, fw / w, motion))
        shots[which[i]].append(found)
        previous_gray = gray.astype(np.float32)
    shutil.rmtree(out_dir, ignore_errors=True)

    duration = end - start
    ends = boundaries[1:] + [duration]
    raw: list[tuple[float, float, float | None]] = []
    for s_start, s_end, frame_faces in zip(boundaries, ends, shots):
        subject = choose_subject(_clusters_for_shot(frame_faces), total_frames=len(frame_faces))
        raw.append((s_start, s_end, crop_fraction(subject.cx, aspect) if subject else None))
    return start, end, steps_from_shots(fill_faceless(raw)), stills


EXTEND_REACH_S = 1.5   # how far a clip edge may move outwards, into silence, to meet a camera cut
EDGE_MIN_SHOT_S = 1.0  # a first or last shot shorter than this reads as a flash


def _silent(lo: float, hi: float, words) -> bool:
    return not any(w.text and w.end > lo and w.start < hi for w in (words or []))


def settle_edges(
    start: float, end: float, cuts: list[float], words=None,
    reach: float = OUTER_MARGIN_S, extend: float = EXTEND_REACH_S, min_shot: float = EDGE_MIN_SHOT_S,
    head: bool = True, tail: bool = True,
) -> tuple[float, float]:
    """Outer clip edges that meet the camera instead of leaving a flash of a shot.

    `cuts` are camera cuts in source time, also some outside the clip. A cut just inside an edge
    leaves a short first or last shot. If the cut comes before any speech, the edge moves in to it.
    If speech has already started, the edge moves outwards to an earlier (or later) cut instead, but
    only through silence, so no word is lost or pulled in from the next sentence. Otherwise the edge
    stays, and the short shot is reported by the motion check."""
    cuts = sorted(cuts)
    duration = end - start
    head_cuts = [c for c in cuts if start < c <= start + reach] if head else []
    if head_cuts:
        first = next((w.start for w in (words or []) if w.text and w.end > start), None)
        before_speech = [c for c in head_cuts if words is None or first is None or c <= first + 1e-6]
        if before_speech and end - max(before_speech) >= duration / 2:
            start = max(before_speech)
        elif words is not None:
            shot_end = min(head_cuts)
            earlier = [p for p in cuts if start - extend <= p < start and shot_end - p >= min_shot
                       and _silent(p, start, words)]
            if earlier:
                start = max(earlier)
    tail_cuts = [c for c in cuts if end - reach <= c < end and c > start] if tail else []
    if tail_cuts:
        last = next((w.end for w in reversed(words or []) if w.text and w.start < end), None)
        after_speech = [c for c in tail_cuts if words is None or last is None or c >= last - 1e-6]
        if after_speech and min(after_speech) - start >= duration / 2:
            end = min(after_speech)
        elif words is not None:
            shot_start = max(tail_cuts)
            later = [q for q in cuts if end < q <= end + extend and q - shot_start >= min_shot
                     and _silent(end, q, words)]
            if later:
                end = min(later)
    return start, end


def auto_crops(src: Path, segments: list[tuple[float, float]], ws_path: Path, words=None):
    """Plan the picture for a clip made of audio segments.

    Returns (audio segments, video segments, crop steps per video segment, still intervals per
    video segment). The clip's outer edges move to a camera cut within OUTER_MARGIN_S (no flash at
    the start or end), but never into speech when `words` (source timing) are given; at joins the
    picture switches at a nearby camera cut (snap_joins), so there is one change of picture
    instead of two and no word is lost."""
    fps = _probe_fps(src)
    audio: list[tuple[float, float]] = []
    cuts: list[list[float]] = []
    last = len(segments) - 1
    for i, (a, b) in enumerate(segments):
        lo = max(0.0, a - (EXTEND_REACH_S if i == 0 else 0.0))
        hi = b + (EXTEND_REACH_S if i == last else 0.0)
        found = [lo + c for c in exact_cuts(src, lo, hi, fps, absolute=True)]
        # only the clip's outer edges move; an edge at a join stays, snap_joins handles it
        s, e = settle_edges(a, b, found, words, head=i == 0, tail=i == last)
        audio.append((round(s, 3), round(e, 3)))
        cuts.append([round(c - s, 4) for c in found if s < c < e])
    video = [(round(s, 3), round(e, 3)) for s, e in snap_joins(audio, cuts)]
    results = [analyze_segment(src, s, e, ws_path, i, fps, head=0.0, tail=0.0) for i, (s, e) in enumerate(video)]
    return audio, video, [r[2] for r in results], [r[3] for r in results]
