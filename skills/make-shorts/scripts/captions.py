"""Captions as an ASS file burned with ffmpeg/libass.

Rules from the spec: keep text exactly as transcribed (no uppercase, Hebrew
punctuation kept), leave the bottom 15% of the frame free, and use a bundled
OFL Hebrew font chosen by mood.

No inline word highlight: libass lays out RTL words in the wrong order around
mid-line colour tags (seen in the visual check), so each block is one plain
event. Per-word highlight is deferred to the optional HyperFrames extension.
"""
from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from pathlib import Path

from messages import UserFacingError
from runtime import run_text
from transcript import Word
from workspace import Workspace

SKILL_ROOT = Path(__file__).resolve().parent.parent
SENTENCE_PUNCT = (".", "?", "!", "…")


@dataclass(frozen=True)
class FontChoice:
    key: str
    family: str
    folder: str
    file: str
    label_he: str


FONTS: dict[str, FontChoice] = {
    "heebo": FontChoice("heebo", "Heebo", "heebo", "Heebo[wght].ttf", "נקי ומודרני"),
    "rubik": FontChoice("rubik", "Rubik", "rubik", "Rubik[wght].ttf", "ידידותי ומעוגל"),
    "secular_one": FontChoice("secular_one", "Secular One", "secularone", "SecularOne-Regular.ttf", "בולט ושובב"),
    "frank_ruhl_libre": FontChoice("frank_ruhl_libre", "Frank Ruhl Libre", "frankruhllibre", "FrankRuhlLibre[wght].ttf", "רשמי עם שריפים"),
}


@dataclass(frozen=True)
class Preset:
    key: str
    label_he: str
    size: int
    primary: str       # BGR hex, 6 digits
    outline_color: str
    outline: int
    shadow: int
    border_style: int  # 1 = outline, 3 = opaque box
    back_alpha: str    # ASS alpha byte for the box/shadow colour, e.g. "99"


PRESETS: dict[str, Preset] = {
    "bold": Preset("bold", "בולט", 84, "FFFFFF", "000000", 6, 0, 1, "00"),
    "clean": Preset("clean", "נקי", 72, "FFFFFF", "000000", 4, 1, 1, "80"),
    "boxed": Preset("boxed", "קופסה", 72, "FFFFFF", "000000", 14, 0, 3, "99"),
    # No black line around the letters; a soft shadow keeps white text readable.
    "plain": Preset("plain", "ללא מסגרת", 72, "FFFFFF", "000000", 0, 3, 1, "99"),
}


@dataclass
class Block:
    words: list[Word]
    start: float
    end: float


def group_words(words: list[Word], words_per_block: int = 3) -> list[Block]:
    """Blocks of at most N words, always breaking at the end of a sentence."""
    blocks: list[Block] = []
    current: list[Word] = []
    for w in words:
        current.append(w)
        if len(current) >= words_per_block or w.text.rstrip("\"'”’)»").endswith(SENTENCE_PUNCT):
            blocks.append(Block(current, current[0].start, current[-1].end))
            current = []
    if current:
        blocks.append(Block(current, current[0].start, current[-1].end))
    return blocks


def remap_words(words: list[Word], segments: list[tuple[float, float]]) -> list[Word]:
    """Move words onto the output timeline of a clip made from several segments."""
    mapped: list[Word] = []
    out_offset = 0.0
    for a, b in segments:
        for w in words:
            if w.start >= a - 1e-6 and w.end <= b + 1e-6:
                mapped.append(Word(w.text, round(w.start - a + out_offset, 6), round(w.end - a + out_offset, 6)))
        out_offset += b - a
    return mapped


def _ts(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


_RTL_START, _RTL_END = "‫", "‬"


def _rtl(text: str) -> str:
    """Wrap Hebrew text in an RTL embedding so libass orders it correctly.

    Seen in the visual check: without it libass lays a Hebrew line out left-to-right, which
    puts a trailing comma on the wrong end and orders "123 עם word" backwards.
    """
    if any("֐" <= ch <= "׿" for ch in text):
        return _RTL_START + text + _RTL_END
    return text


def _escape(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ").replace("\\", "/")
    return text.replace("{", "\\{").replace("}", "\\}")


def _style_line(name: str, family: str, size: int, preset: Preset, margin_v: int, alignment: int) -> str:
    primary = f"&H00{preset.primary}"
    outline = f"&H{preset.back_alpha}{preset.outline_color}" if preset.border_style == 3 else f"&H00{preset.outline_color}"
    back = f"&H{preset.back_alpha}{preset.outline_color}"
    return (
        f"Style: {name},{family},{size},{primary},{primary},{outline},{back},"
        f"-1,0,0,0,100,100,0,0,{preset.border_style},{preset.outline},{preset.shadow},{alignment},60,60,{margin_v},1"
    )


def build_ass(
    blocks: list[Block],
    *,
    preset: str,
    font: str = "heebo",
    width: int = 1080,
    height: int = 1920,
    safe_bottom_frac: float = 0.15,
    title_text: str | None = None,
) -> str:
    if preset not in PRESETS:
        raise UserFacingError("unknown", detail=f"סגנון כתוביות לא מוכר: {preset}")
    if font not in FONTS:
        raise UserFacingError("unknown", detail=f"פונט לא מוכר: {font}")
    p = PRESETS[preset]
    family = FONTS[font].family
    margin_v = math.ceil(height * safe_bottom_frac)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        _style_line("Default", family, p.size, p, margin_v, 2),
        _style_line("Title", family, int(p.size * 1.2), p, 250, 8),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title_text:
        lines.append(f"Dialogue: 1,{_ts(0.0)},{_ts(3.0)},Title,,0,0,0,,{_rtl(_escape(title_text))}")
    for block in blocks:
        text = _rtl(" ".join(_escape(w.text) for w in block.words))
        lines.append(f"Dialogue: 0,{_ts(block.start)},{_ts(block.end)},Default,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def _srt_ts(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(blocks: list[Block]) -> str:
    """The same caption blocks as plain SRT, for editors and platforms that take a subtitle file."""
    entries = [
        f"{i}\n{_srt_ts(b.start)} --> {_srt_ts(b.end)}\n{' '.join(w.text for w in b.words)}\n"
        for i, b in enumerate(blocks, start=1)
    ]
    return "\n".join(entries)


def selftest_sentence() -> str:
    return "זה מבחן: בדיקה 123 עם word באנגלית, וגם צה״ל, ג׳ירפה ומקף־עברי."


def render_selftest(ws: Workspace, font: str = "heebo") -> Path:
    """Render a 5 s clip with the self-test sentence so the captions can be inspected."""
    choice = FONTS[font]
    fonts_dir = ws.path / "fonts"
    fonts_dir.mkdir(exist_ok=True)
    shutil.copy(SKILL_ROOT / "fonts" / choice.folder / choice.file, fonts_dir / choice.file)

    tokens = selftest_sentence().split()
    words = [Word(tok, round(0.2 + i * 0.4, 6), round(0.2 + (i + 1) * 0.4, 6)) for i, tok in enumerate(tokens)]
    (ws.path / "selftest.ass").write_text(
        build_ass(group_words(words, 3), preset="clean", font=font), encoding="utf-8"
    )
    result = run_text(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=0x203040:s=1080x1920:r=30:d=5",
         "-vf", "ass=selftest.ass:fontsdir=fonts",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "selftest.mp4"],
        timeout=120, cwd=ws.path,
    )
    if result.returncode != 0:
        raise UserFacingError("unknown", detail="מבחן הכתוביות נכשל: " + result.stderr.strip()[:200])
    return ws.path / "selftest.mp4"
