"""One-pass ffmpeg rendering of a clip: cut, reframe to 1080x1920, burn captions.

Always a single re-encode (never stream copy). Each segment is its own seeked
input so a late clip in a long video does not decode everything before it.
ffmpeg runs with the workspace as cwd and bare relative names for the caption
file and fonts, so Hebrew or spaces in the project path never reach a filtergraph.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from captions import FONTS, PRESETS, SKILL_ROOT, build_ass, build_srt, make_blocks, remap_words
from hooks import HookPlan
from messages import UserFacingError
from runtime import run_text
from selection import Clip
from transcript import Word
from workspace import Workspace, final_filename

Reframe = Literal["center", "blur"]
# No fade to black: the user found it bad at the end of a short. Only the sound gets a short
# fade at the clip's edges, so it does not start or stop with a click.
FADE_IN_S = 0.05
FADE_OUT_S = 0.15
BLACK_IN_S = 0.25   # only with fade_to_black
BLACK_OUT_S = 0.4
JOIN_FADE_S = 0.04


def crop_x_expression(steps: list[tuple[float, float]]) -> str:
    """ffmpeg expression for a crop position that changes at given times (seconds from the segment start)."""
    if len(steps) == 1:
        return f"(iw-1080)*{steps[0][1]:g}"
    expr = f"{steps[-1][1]:g}"
    for (_, cx), (t_next, _) in zip(reversed(steps[:-1]), reversed(steps[1:])):
        expr = f"if(lt(t,{t_next:g}),{cx:g},{expr})"
    return f"(iw-1080)*{expr}"


PUSH_RATE = 0.03  # zoom per second during a still shot (3% a second: felt, not seen)
PUSH_MAX = 0.10


def push_expression(intervals: list[tuple[float, float]], var: str = "t") -> str:
    """Zoom factor over time (`var` = seconds from the segment start): a slow push-in inside each
    still shot, back to 1 at the next camera cut, where the reset is hidden by the change of picture."""
    if not intervals:
        return "1"
    terms = "+".join(
        f"if(between({var},{s:g},{e:g}),min({PUSH_RATE:g}*({var}-{s:g}),{PUSH_MAX:g}),0)" for s, e in intervals
    )
    return f"1+{terms}"


def push_filter(intervals: list[tuple[float, float]], fps: float = 25.0) -> str:
    """A smooth slow zoom that keeps the upper part of the frame (the head) in view.

    Measured on a still frame: resizing by whole pixels per frame stutters (frames that do not
    change, then a jump of 3.6), while perspective interpolates sub-pixel and moves evenly (spread
    0.04). perspective has no time variable, so time is the frame count `in` over the frame rate;
    the clock restarts per segment because each segment is its own filter chain."""
    z = f"({push_expression(intervals, var=f'in/{fps:g}')})"
    dx, top, bottom = f"(W*(1-1/{z})/2)", f"(H*(1-1/{z})*0.3)", f"(H*(1-1/{z})*0.7)"
    return (
        f"perspective=x0='{dx}':y0='{top}':x1='W-{dx}':y1='{top}':x2='{dx}':y2='H-{bottom}':"
        f"x3='W-{dx}':y3='H-{bottom}':interpolation=linear:eval=frame"
    )


def build_ffmpeg_cmd(
    src: str,
    segments: list[tuple[float, float]],
    *,
    ass_name: str,
    reframe: Reframe,
    out_name: str,
    loudnorm_i: float = -16.0,
    has_audio: bool = True,
    crop_x: list[float] | float | None = None,
    zoom: list[float] | None = None,
    video_segments: list[tuple[float, float]] | None = None,
    push: list[list[tuple[float, float]]] | None = None,
    fps: float = 25.0,
    fade_to_black: bool = False,
) -> list[str]:
    """`crop_x` (0..1, one per segment or one for all) places the vertical crop in `center` mode.
    `zoom` (one per segment, 1.0 = none) punches in on a segment; the crop then keeps the upper
    part of the picture, where the head is. `video_segments`, when given, are the picture's edges
    for each segment (an L-cut at a join); `segments` stay the audio's edges."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    bases: list[float] = []
    for i, (start, end) in enumerate(segments):
        if video_segments:
            vs, ve = video_segments[i]
            base, top = min(start, vs), max(end, ve)
        else:
            base, top = start, end
        bases.append(base)
        cmd += ["-ss", f"{base:.3f}", "-t", f"{top - base:.3f}", "-i", src]

    n = len(segments)
    total = sum(b - a for a, b in segments)
    fade_in = min(FADE_IN_S, total / 4)
    fade_out = min(FADE_OUT_S, total / 4)
    xs = crop_x if isinstance(crop_x, list) else [0.5 if crop_x is None else crop_x]
    xs = xs or [0.5]
    parts: list[str] = []
    centered = reframe != "blur"
    vlabels: list[str] = []
    alabels: list[str] = []
    for i, (a, b) in enumerate(segments):
        # Every segment's clock restarts at 0 on its first real frame, so the crop and zoom times
        # (counted in frames from the segment start) land on the right frame. Without it the first
        # frame sits up to one frame in, and a crop switch at a camera cut lands one frame late.
        vtrim = "setpts=PTS-STARTPTS,"
        atrim = ""
        if video_segments:
            vs, ve = video_segments[i]
            vtrim = f"trim=start={vs - bases[i]:g}:end={ve - bases[i]:g},setpts=PTS-STARTPTS,"
            atrim = f"atrim=start={a - bases[i]:g}:end={b - bases[i]:g},asetpts=PTS-STARTPTS"
        if centered:
            cx = xs[min(i, len(xs) - 1)]
            z = zoom[i] if zoom and i < len(zoom) else 1.0
            height = 1920 if z <= 1.0 else int(round(1920 * z / 2)) * 2
            y = "0" if z <= 1.0 else "(ih-1920)*0.3"
            stills = push[i] if push and i < len(push) else []
            slow = f",{push_filter(stills, fps)}" if stills else ""
            if isinstance(cx, list):
                parts.append(f"[{i}:v]{vtrim}scale=-2:{height},crop=1080:1920:x='{crop_x_expression(cx)}':y='{y}'{slow},setsar=1[v{i}]")
            else:
                parts.append(f"[{i}:v]{vtrim}scale=-2:{height},crop=1080:1920:(iw-1080)*{cx:g}:{y}{slow},setsar=1[v{i}]")
            vlabels.append(f"[v{i}]")
        elif vtrim:
            parts.append(f"[{i}:v]{vtrim.rstrip(',')}[v{i}]")
            vlabels.append(f"[v{i}]")
        else:
            vlabels.append(f"[{i}:v]")
        if has_audio:
            if n > 1:
                length = b - a
                parts.append(
                    f"[{i}:a]{atrim + ',' if atrim else ''}afade=t=in:st=0:d={JOIN_FADE_S:g},"
                    f"afade=t=out:st={length - JOIN_FADE_S:g}:d={JOIN_FADE_S:g}[a{i}]"
                )
                alabels.append(f"[a{i}]")
            elif atrim:
                parts.append(f"[{i}:a]{atrim}[a{i}]")
                alabels.append(f"[a{i}]")
            else:
                alabels.append(f"[{i}:a]")
    if n == 1:
        vin, ain = vlabels[0], (alabels[0] if has_audio else "")
    else:
        # Picture and sound are joined separately. One concat starts each part after the longer of
        # its two streams, so an L-cut (picture shorter than sound in one part, longer in the next)
        # would freeze the picture and leave it out of sync for the rest of the clip. snap_joins keeps
        # the total length of both the same, so separate concats stay in sync.
        parts.append(f"{''.join(vlabels)}concat=n={n}:v=1:a=0[vc]")
        if has_audio:
            parts.append(f"{''.join(alabels)}concat=n={n}:v=0:a=1[ac]")
        vin, ain = "[vc]", "[ac]"

    black = ""
    if fade_to_black:  # off by default (profile.fade_to_black): it looked bad at the end of a short
        fin, fout = min(BLACK_IN_S, total / 4), min(BLACK_OUT_S, total / 4)
        black = f"fade=t=in:st=0:d={fin:g},fade=t=out:st={total - fout:g}:d={fout:g},"
    captions = f"ass={ass_name}:fontsdir=fonts,{black}format=yuv420p[vout]"
    if reframe == "blur":
        parts.append(
            f"{vin}split[s0][s1];"
            "[s0]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg];"
            "[s1]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{captions}"
        )
    else:
        parts.append(f"{vin}{captions}")
    if has_audio:
        parts.append(
            f"{ain}loudnorm=I={loudnorm_i:g}:TP=-1.5:LRA=11,"
            f"afade=t=in:st=0:d={fade_in:g},afade=t=out:st={total - fade_out:g}:d={fade_out:g}[aout]"
        )

    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", out_name]
    return cmd


def render_clip(
    src: Path,
    clip: Clip,
    plan: HookPlan,
    words: list[Word],
    ws: Workspace,
    *,
    preset: str,
    reframe: str,
    out_dir: Path,
    title: str,
    index: int,
    font: str = "heebo",
    has_audio: bool = True,
    crop_x: list[float] | None = None,
    caption_words: list[Word] | None = None,
    zoom: list[float] | None = None,
    push: list[list[tuple[float, float]]] | None = None,
    video_segments: list[tuple[float, float]] | None = None,
    fps: float = 25.0,
    fade_to_black: bool = False,
) -> Path:
    """Render one clip into out_dir and return the final file path.

    `words` are the transcript words in original time; captions follow the hook plan's
    segments, so a teaser quote is captioned both in its opening and in place.
    `caption_words`, when given, are already on the clip's own timeline (a re-transcription
    of the finished clip) and replace the remapped words.
    """
    if font not in FONTS:
        raise UserFacingError("unknown", detail=f"פונט לא מוכר: {font}")
    choice = FONTS[font]
    fonts_dir = ws.path / "fonts"
    fonts_dir.mkdir(exist_ok=True)
    shutil.copy(SKILL_ROOT / "fonts" / choice.folder / choice.file, fonts_dir / choice.file)

    joins, acc = [], 0.0
    for a, b in plan.segments[:-1]:
        acc += b - a
        joins.append(acc)
    on_clip = [w for w in caption_words if w.text] if caption_words is not None else remap_words(words, plan.segments)
    blocks = make_blocks(on_clip, PRESETS[preset].words_per_block, breaks=joins)
    ass_name = f"clip{index:02d}.ass"
    (ws.path / ass_name).write_text(
        build_ass(blocks, preset=preset, font=font, title_text=plan.title_text), encoding="utf-8"
    )

    out_name = f"out{index:02d}.mp4"
    total = sum(b - a for a, b in plan.segments)
    cmd = build_ffmpeg_cmd(
        str(src), plan.segments, ass_name=ass_name, reframe=reframe, out_name=out_name,
        has_audio=has_audio, crop_x=crop_x, zoom=zoom, push=push, video_segments=video_segments, fps=fps,
        fade_to_black=fade_to_black,
    )
    result = run_text(cmd, timeout=max(600.0, total * 20), cwd=ws.path)
    if result.returncode != 0:
        raise UserFacingError("unknown", detail="הרינדור נכשל: " + result.stderr.strip()[:300])

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / final_filename(title, index)
    shutil.move(str(ws.path / out_name), final)
    final.with_suffix(".srt").write_text(build_srt(blocks), encoding="utf-8")
    return final
