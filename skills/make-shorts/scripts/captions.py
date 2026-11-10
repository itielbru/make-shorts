"""Captions as an ASS file burned with ffmpeg/libass.

Rules from the spec: keep text exactly as transcribed (no uppercase, Hebrew
punctuation kept), leave the bottom 15% of the frame free, and use a bundled
OFL Hebrew font chosen by mood.

No colour tags inside a line: around mid-line tags libass moves Hebrew
punctuation to the wrong side of a word (seen in the visual check), so the
text of every event is plain. The spoken-word highlight (presets with
`highlight`) draws the same plain line again in the highlight colour, clipped
to the box of the word being spoken; the boxes are measured by libass itself.
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
    words_per_block: int = 3
    margin_v_frac: float | None = None  # distance of the text from the bottom, as a fraction of the height
    pop: bool = False  # soft fade and scale-in at the start of each caption
    highlight: str | None = None  # BGR colour of the word being spoken, or None for plain blocks


PRESETS: dict[str, Preset] = {
    "bold": Preset("bold", "בולט", 84, "FFFFFF", "000000", 6, 0, 1, "00"),
    "clean": Preset("clean", "נקי", 72, "FFFFFF", "000000", 4, 1, 1, "80"),
    "boxed": Preset("boxed", "קופסה", 72, "FFFFFF", "000000", 14, 0, 3, "99"),
    # No black line around the letters; a soft shadow keeps white text readable.
    "plain": Preset("plain", "ללא מסגרת", 72, "FFFFFF", "000000", 0, 3, 1, "99"),
    # Social-media style: big, two words at a time, raised well above the bottom edge.
    "social": Preset("social", "רשתות חברתיות", 112, "FFFFFF", "000000", 0, 5, 1, "B0",
                     words_per_block=2, margin_v_frac=0.30, pop=True),
    # Like social, bigger and heavier: a user found social thin and hard to read over a light shirt.
    # The thick dark outline makes the letters read as bolder and keeps them clear on any background.
    # Round 2 of the user's design feedback: the word being spoken turns yellow (the look viewers
    # expect), a thicker outline, and the text lower, on the chest instead of over the mouth and chin
    # (seen on the QA sheets: at 0.30 the captions sat on the lower face of every close-up). 0.22
    # still clears the bottom bar that Reels and TikTok draw over the video.
    "social_bold": Preset("social_bold", "רשתות, עבה וגדול", 146, "FFFFFF", "000000", 8, 4, 1, "70",
                          words_per_block=2, margin_v_frac=0.22, pop=True, highlight="00E5FF"),
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


# Words a caption should not end on: the thought continues on the next one.
DANGLING = frozenset({"של", "את", "על", "עם", "כי", "אם", "או", "אבל", "אל", "כל", "מה", "לא", "גם", "אז", "כמו", "בין", "רק"})
PAUSE_BREAK_S = 0.5   # a pause this long always starts a new caption
MIN_BLOCK_S = 0.45    # shortest time a caption stays on screen, when there is room
LEAD_IN_S = 0.06      # a caption appears a hair before the word is heard
CLOSE_GAP_S = 0.45    # gaps shorter than this between captions are bridged, so nothing flickers


def _core(text: str) -> str:
    return text.strip("\"'”’)»(.,!?;:…-–")


def make_blocks(words: list[Word], words_per_block: int = 3, breaks: list[float] | None = None) -> list[Block]:
    """Readable caption blocks: no dangling function words, breaks at pauses, no flicker.

    `breaks` are times of hard cuts in the clip (joins between parts): no caption crosses one.
    """
    groups: list[list[Word]] = []
    current: list[Word] = []
    cuts = sorted(breaks or [])

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for w in words:
        if current and w.start - current[-1].end > PAUSE_BREAK_S:
            flush()
        if current and any(current[-1].start < c <= w.start + 1e-6 for c in cuts):
            flush()
        current.append(w)
        sentence_end = w.text.rstrip("\"'”’)»").endswith(SENTENCE_PUNCT)
        if sentence_end:
            flush()
        elif len(current) >= words_per_block:
            if _core(w.text) in DANGLING and len(current) < words_per_block + 1:
                continue
            flush()
    flush()

    blocks: list[Block] = []
    for group in groups:
        start = group[0].start - LEAD_IN_S
        if blocks:
            start = max(start, blocks[-1].end)
        for c in cuts:  # the lead-in never reaches back over a join
            if start < c <= group[0].start + 1e-6:
                start = c
        blocks.append(Block(group, max(0.0, start), group[-1].end))
    for i, block in enumerate(blocks):
        following = blocks[i + 1].start if i + 1 < len(blocks) else None
        end = max(block.end, block.start + MIN_BLOCK_S)
        if following is not None:
            end = min(end, following)
            if following - end <= CLOSE_GAP_S:
                end = following
        for c in cuts:  # a caption never carries over a hard cut
            if block.start < c < end:
                end = c
        block.end = end
    return blocks


def remap_words(words: list[Word], segments: list[tuple[float, float]]) -> list[Word]:
    """Move words onto the output timeline of a clip made from several segments."""
    # A word belongs to the segment its middle falls in. The waveform cut may land a hair inside a
    # word's (approximate) timing; requiring the whole word inside dropped heard words from captions.
    mapped: list[Word] = []
    out_offset = 0.0
    for a, b in segments:
        for w in words:
            middle = (w.start + w.end) / 2
            if w.text and a - 1e-6 <= middle <= b + 1e-6:
                start, end = max(w.start, a), min(w.end, b)
                mapped.append(Word(w.text, round(start - a + out_offset, 6), round(end - a + out_offset, 6)))
        out_offset += b - a
    return mapped


def clip_to_source(t: float, segments: list[tuple[float, float]]) -> float | None:
    """The source time shown at second `t` of a clip made of `segments`, or None past its end."""
    offset = 0.0
    for a, b in segments:
        if t <= offset + (b - a) + 1e-9:
            return a + (t - offset)
        offset += b - a
    return None


def retime_words(
    words: list[Word], old_segments: list[tuple[float, float]], new_segments: list[tuple[float, float]]
) -> list[Word]:
    """Move words that sit on one clip timeline (made of `old_segments`) onto the timeline of the same
    clip with new segment edges. A word whose audio is no longer in the clip is dropped."""
    def to_source(t: float) -> float | None:
        offset = 0.0
        for a, b in old_segments:
            if t <= offset + (b - a) + 1e-6:
                return a + (t - offset)
            offset += b - a
        return None

    moved: list[Word] = []
    for w in words:
        s, e = to_source(w.start), to_source(w.end)
        if s is None or e is None:
            continue
        mapped = remap_words([Word(w.text, s, e)], new_segments)
        moved += mapped
    return sorted(moved, key=lambda w: w.start)


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
    seam: list[tuple[float, float]] | None = None,
    word_edges: list[list[float] | None] | None = None,
) -> str:
    """`seam`: times of a top/bottom split screen. Captions shown then sit on the line between the
    two halves, where they cover no face, instead of at their usual height over the lower face.

    `word_edges` (from `measure_word_edges`, one entry per block) turns on the spoken-word
    highlight: the block is drawn as always, and the same text in the highlight colour is drawn
    over it, clipped to the box of the word being spoken. Colour tags inside an RTL line move
    Hebrew punctuation in libass, so the text of every event stays plain."""
    if preset not in PRESETS:
        raise UserFacingError("unknown", detail=f"סגנון כתוביות לא מוכר: {preset}")
    if font not in FONTS:
        raise UserFacingError("unknown", detail=f"פונט לא מוכר: {font}")
    p = PRESETS[preset]
    family = FONTS[font].family
    margin_v = math.ceil(height * (p.margin_v_frac if p.margin_v_frac is not None else safe_bottom_frac))

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
    for i, block in enumerate(blocks):
        text = _rtl(" ".join(_escape(w.text) for w in block.words))
        pop = r"{\fad(70,40)\fscx92\fscy92\t(0,90,\fscx100\fscy100)}" if p.pop else ""
        middle = (block.start + block.end) / 2
        at_seam = any(a <= middle <= b for a, b in (seam or []))
        place = rf"{{\an5\pos({width // 2},{height // 2})}}" if at_seam else ""
        lines.append(f"Dialogue: 0,{_ts(block.start)},{_ts(block.end)},Default,,0,0,0,,{place}{pop}{text}")
        edges = word_edges[i] if word_edges and i < len(word_edges) else None
        if not p.highlight or not edges or len(block.words) < 2:
            continue
        for k, (x1, x2) in enumerate(word_boxes(edges, width)):
            w = block.words[k]
            start = block.start if k == 0 else max(block.start, w.start)
            end = block.end if k == len(block.words) - 1 else min(block.end, block.words[k + 1].start)
            if end - start < 0.05:
                continue
            clip = rf"{{\clip({x1:.0f},0,{x2:.0f},{height})\1c&H{p.highlight}&}}"
            lines.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Default,,0,0,0,,{place}{pop if k == 0 else ''}{clip}{text}")
    return "\n".join(lines) + "\n"


def word_boxes(edges: list[float], width: int = 1080) -> list[tuple[float, float]]:
    """Horizontal box of each word (logical order) from the measured edges: `edges[k]` is the x of
    the gap before word k, counted from the right (RTL); the outer words reach the frame's edges,
    so a small measuring error never cuts a letter at the ends of the line."""
    n = len(edges) + 1
    bounds = [float(width)] + list(edges) + [0.0]
    return [(bounds[k + 1], bounds[k]) for k in range(n)]


def measure_word_edges(blocks: list[Block], preset: str, font: str, ws_dir: Path, width: int = 1080) -> list[list[float] | None]:
    """Where the gaps between the words of each block fall on screen, measured by libass itself (the
    renderer that burns the captions), so Hebrew shaping and RTL order are exactly as burned.

    Each block and each of its leading word runs is drawn on its own row of one wide, plain image;
    the ink width of the first k words gives the gap after word k, from the right end of the centred
    line. A block too wide for one line (libass would wrap it) gets None: no highlight there."""
    import subprocess

    import numpy as np

    p = PRESETS[preset]
    family = FONTS[font].family
    canvas_w, row_h = 3000, int(p.size * 1.6)
    rows: list[str] = []
    plan: list[tuple[int, int]] = []  # (first row, number of words) per block
    for block in blocks:
        n = len(block.words)
        plan.append((len(rows), n))
        rows.append(" ".join(_escape(w.text) for w in block.words))
        rows += [" ".join(_escape(w.text) for w in block.words[:k]) for k in range(1, n)]
    if not rows:
        return []
    height = row_h * len(rows)
    style = (f"Style: M,{family},{p.size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
             f"-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1")
    events = [f"Dialogue: 0,0:00:00.00,0:00:01.00,M,,0,0,0,,{{\\pos(20,{r * row_h + 10})}}{_rtl(text)}"
              for r, text in enumerate(rows)]
    ass = "\n".join(["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {canvas_w}", f"PlayResY: {height}",
                     "WrapStyle: 2", "", "[V4+ Styles]",
                     "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
                     "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                     "Alignment, MarginL, MarginR, MarginV, Encoding", style, "", "[Events]",
                     "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text", *events]) + "\n"
    (ws_dir / "measure.ass").write_text(ass, encoding="utf-8")
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                          f"color=c=black:s={canvas_w}x{height}:d=0.1", "-vf", "ass=measure.ass:fontsdir=fonts,format=gray",
                          "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True, timeout=120, cwd=ws_dir).stdout
    if len(raw) < canvas_w * height:
        return [None] * len(blocks)
    image = np.frombuffer(raw[: canvas_w * height], np.uint8).reshape(height, canvas_w)
    widths = []
    for r in range(len(rows)):
        cols = np.where(image[r * row_h:(r + 1) * row_h].max(axis=0) > 60)[0]
        widths.append(float(cols[-1] - cols[0] + 1) if len(cols) else 0.0)
    space = p.size * 0.25
    out: list[list[float] | None] = []
    for first, n in plan:
        line = widths[first]
        if line <= 0 or line > width - 120 - 2 * p.outline:  # 60 px margins: libass would wrap it
            out.append(None)
            continue
        right = width / 2 + line / 2
        out.append([right - widths[first + k] - space / 2 for k in range(1, n)])
    return out


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
