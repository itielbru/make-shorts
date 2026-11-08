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

from captions import FONTS, SKILL_ROOT, build_ass, build_srt, group_words, remap_words
from hooks import HookPlan
from messages import UserFacingError
from runtime import run_text
from selection import Clip
from transcript import Word
from workspace import Workspace, final_filename

Reframe = Literal["center", "blur"]


def build_ffmpeg_cmd(
    src: str,
    segments: list[tuple[float, float]],
    *,
    ass_name: str,
    reframe: Reframe,
    out_name: str,
    loudnorm_i: float = -16.0,
    has_audio: bool = True,
) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for start, end in segments:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", src]

    n = len(segments)
    parts: list[str] = []
    if n == 1:
        vin, ain = "[0:v]", "[0:a]"
    else:
        streams = "".join(f"[{i}:v][{i}:a]" if has_audio else f"[{i}:v]" for i in range(n))
        audio_flag = 1 if has_audio else 0
        parts.append(f"{streams}concat=n={n}:v=1:a={audio_flag}[vc]" + ("[ac]" if has_audio else ""))
        vin, ain = "[vc]", "[ac]"

    captions = f"ass={ass_name}:fontsdir=fonts,format=yuv420p[vout]"
    if reframe == "blur":
        parts.append(
            f"{vin}split[s0][s1];"
            "[s0]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg];"
            "[s1]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{captions}"
        )
    else:
        parts.append(f"{vin}scale=-2:1920,crop=1080:1920,{captions}")
    if has_audio:
        parts.append(f"{ain}loudnorm=I={loudnorm_i:g}:TP=-1.5:LRA=11[aout]")

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
) -> Path:
    """Render one clip into out_dir and return the final file path.

    `words` are the transcript words in original time; captions follow the hook plan's
    segments, so a teaser quote is captioned both in its opening and in place.
    """
    if font not in FONTS:
        raise UserFacingError("unknown", detail=f"פונט לא מוכר: {font}")
    choice = FONTS[font]
    fonts_dir = ws.path / "fonts"
    fonts_dir.mkdir(exist_ok=True)
    shutil.copy(SKILL_ROOT / "fonts" / choice.folder / choice.file, fonts_dir / choice.file)

    blocks = group_words(remap_words(words, plan.segments), 3)
    ass_name = f"clip{index:02d}.ass"
    (ws.path / ass_name).write_text(
        build_ass(blocks, preset=preset, font=font, title_text=plan.title_text), encoding="utf-8"
    )

    out_name = f"out{index:02d}.mp4"
    total = sum(b - a for a, b in plan.segments)
    cmd = build_ffmpeg_cmd(
        str(src), plan.segments, ass_name=ass_name, reframe=reframe, out_name=out_name, has_audio=has_audio
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
