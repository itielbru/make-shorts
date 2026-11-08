#!/usr/bin/env python3
"""make-shorts command line.

Scripts never ask questions. Each command prints a Markdown report on stdout,
progress and errors on stderr, and exits 0 on success, 1 when a stage produced
nothing, 2 for a missing dependency or a config problem, 3 when an API key is
needed. A stage that fails is listed under "ראיות חסרות" and the rest goes on.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import acquire
import audio as audio_mod
import captions
import config
import engines
import qa
import render
import selection
import setup_env
import transcript as transcript_mod
from hooks import HookMode, plan_hook
from messages import UserFacingError
from profiles import Profile, load_profile
from runtime import configure_stdio
from selection import Clip, Pick
from workspace import Workspace

STATE_FILE = "process_config.json"
CLOUD_ENGINES = {"groq", "elevenlabs"}


def progress(message: str) -> None:
    print(f"[make-shorts] {message}", file=sys.stderr, flush=True)


def mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


class Report:
    def __init__(self, title: str) -> None:
        self.title = title
        self.bullets: list[str] = []
        self.sections: list[tuple[str, list[str]]] = []
        self.raw_blocks: list[str] = []
        self.unavailable: list[str] = []

    def bullet(self, text: str) -> None:
        self.bullets.append(text)

    def section(self, title: str, lines: list[str]) -> None:
        self.sections.append((title, lines))

    def raw(self, text: str) -> None:
        self.raw_blocks.append(text)

    def missing(self, text: str) -> None:
        self.unavailable.append(text)

    def markdown(self) -> str:
        out = [f"# {self.title}", ""]
        out += [f"- {b}" for b in self.bullets]
        for title, lines in self.sections:
            out += ["", f"## {title}", *lines]
        out += ["", *self.raw_blocks] if self.raw_blocks else []
        if self.unavailable:
            out += ["", "## ראיות חסרות", *[f"- {u}" for u in self.unavailable]]
        return "\n".join(out).rstrip() + "\n"


@dataclass
class ProjectState:
    project: Path
    media_path: str = ""
    media_title: str = ""
    media_duration: float = 0.0
    has_audio: bool = True
    workspace: str = ""
    engine: str = ""
    transcript_path: str = ""
    silences_path: str = ""
    clips: list[dict] = field(default_factory=list)
    rendered: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, project: Path) -> "ProjectState":
        path = project / STATE_FILE
        if not path.exists():
            return cls(project=project)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("project", None)
        return cls(project=project, **data)

    def save(self) -> None:
        self.project.mkdir(parents=True, exist_ok=True)
        data = dataclasses.asdict(self)
        data.pop("project")
        (self.project / STATE_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _project(args: argparse.Namespace) -> Path:
    return Path(args.project) if args.project else Path.cwd() / "make-shorts-project"


def _profile() -> Profile:
    return load_profile(config.config_dir() / "profile.json")


def _workspace(state: ProjectState) -> Workspace:
    if state.workspace and Path(state.workspace).is_dir():
        return Workspace(Path(state.workspace))
    ws = Workspace.create(state.project)
    state.workspace = str(ws.path)
    return ws


def _fingerprint(path: Path) -> str:
    """Cheap identity for a big file: size, mtime and the first and last megabyte."""
    stat = path.stat()
    digest = hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1 << 20))
        if stat.st_size > 2 << 20:
            handle.seek(-(1 << 20), 2)
            digest.update(handle.read(1 << 20))
    return digest.hexdigest()


def _load_transcript(state: ProjectState) -> transcript_mod.Transcript:
    if not state.transcript_path or not Path(state.transcript_path).exists():
        raise UserFacingError("no_transcript")
    return transcript_mod.load_transcript(Path(state.transcript_path))


def _load_silences(state: ProjectState) -> list[tuple[float, float]]:
    if state.silences_path and Path(state.silences_path).exists():
        return [tuple(s) for s in json.loads(Path(state.silences_path).read_text(encoding="utf-8"))]
    return []


def _clip_from(d: dict) -> Clip:
    names = {f.name for f in dataclasses.fields(Clip)}
    return Clip(**{k: v for k, v in d.items() if k in names})


# ------------------------------------------------------------------ commands

def cmd_setup(args: argparse.Namespace) -> int:
    env = setup_env.check_environment()
    hw = setup_env.detect_hardware()
    # Before a profile exists the user has made no privacy choice, so advise as if cloud were
    # possible (it is the only engine in this version) and let the wizard ask for consent.
    cloud_allowed = True
    try:
        cloud_allowed = _profile().cloud_allowed
    except UserFacingError:
        pass
    advice = setup_env.recommend_engine(hw, cloud_allowed)

    if args.finish:
        _profile()  # raises profile_missing / bad_profile
        config.write_settings({"SETUP_COMPLETE": "true"})

    status = setup_env.status_json(env, hw, advice, first_run=setup_env.is_first_run())
    selftest = None
    if status["can_proceed"] and not args.skip_selftest:
        progress("בודק שהכתוביות העבריות מצטיירות נכון...")
        ws = Workspace.create()
        try:
            produced = captions.render_selftest(ws)
            target = config.config_dir() / "selftest.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(produced, target)
            selftest = {"ok": True, "path": str(target)}
        except UserFacingError as err:
            selftest = {"ok": False, "error": err.message_he}
            status["can_proceed"] = False
        finally:
            ws.cleanup()
    status["caption_selftest"] = selftest

    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=1))
    else:
        report = Report("הגדרה ראשונית")
        report.bullet("כלים חסרים: " + (", ".join(status["missing"]) or "אין"))
        report.bullet(f"מחשב: {hw.cpu_name}, {hw.cores} ליבות, {hw.ram_gb:.1f}GB זיכרון")
        report.bullet(f"המלצה לתמלול: {advice.engine}. {advice.reason_he}")
        if selftest:
            report.bullet("מבחן כתוביות: " + ("עבר" if selftest["ok"] else "נכשל: " + selftest["error"]))
        for hint in status["install_hints"]:
            report.bullet(f"להתקנה: {hint}")
        print(report.markdown())
    return 0 if status["can_proceed"] else 2


def cmd_ingest(args: argparse.Namespace) -> int:
    project = _project(args)
    state = ProjectState.load(project)
    if state.workspace:
        Workspace(Path(state.workspace)).cleanup()
    project.mkdir(parents=True, exist_ok=True)
    ws = Workspace.create(project)
    progress("טוען את הסרטון...")
    info = acquire.fetch(args.source, ws)
    state = ProjectState(
        project=project, media_path=str(info.path), media_title=info.title,
        media_duration=info.duration, has_audio=info.has_audio, workspace=str(ws.path),
    )
    state.save()
    report = Report("הסרטון נטען")
    report.bullet(f"כותרת: {info.title}")
    report.bullet(f"אורך: {mmss(info.duration)} ({info.width}x{info.height})")
    report.bullet(f"קובץ: `{info.path}`")
    report.bullet(f"תיקיית הפרויקט: `{project}`")
    if not info.has_audio:
        report.missing("בסרטון אין רצועת קול, ולכן אי אפשר לתמלל אותו.")
    print(report.markdown())
    return 0


def _resolve_engine(profile: Profile, override: str | None) -> str:
    name = override or profile.engine
    return "groq" if name == "auto" else name


def cmd_transcribe(args: argparse.Namespace) -> int:
    project = _project(args)
    state = ProjectState.load(project)
    if not state.media_path:
        raise UserFacingError("no_media")
    if not Path(state.media_path).exists():
        raise UserFacingError("media_missing")
    if not state.has_audio:
        raise UserFacingError("no_audio_track")
    profile = _profile()
    name = _resolve_engine(profile, args.engine)
    if name in CLOUD_ENGINES and not profile.cloud_allowed:
        raise UserFacingError("cloud_not_allowed", engine=name)

    media = Path(state.media_path)
    key = transcript_mod.cache_key(_fingerprint(media), name, profile.language)
    tdir = project / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    tpath, spath = tdir / f"{key}.json", tdir / f"{key}.silences.json"

    report = Report("התמלול")
    if tpath.exists() and spath.exists():
        progress("משתמש בתמלול שמור מהרצה קודמת.")
        t = transcript_mod.load_transcript(tpath)
    else:
        ws = _workspace(state)
        audio_path = ws.path / "audio.mp3"
        if not audio_path.exists():
            progress("מחלץ את האודיו מהסרטון...")
            audio_mod.extract_audio(media, audio_path)
        if tpath.exists():
            t = transcript_mod.load_transcript(tpath)
        else:
            engine = engines.get_engine(name)
            t = engine.transcribe(audio_path, language=profile.language, progress=progress)
        progress("מזהה שתיקות...")
        silences = audio_mod.detect_silences(audio_path)
        t.words = transcript_mod.drop_words_in_trailing_silence(
            t.words, silences, audio_mod.probe_duration(audio_path)
        )
        transcript_mod.save_transcript(t, tpath)
        spath.write_text(json.dumps(silences), encoding="utf-8")

    state.engine, state.transcript_path, state.silences_path = name, str(tpath), str(spath)
    state.save()
    report.bullet(f"מנוע: {name}, שפה: {profile.language}")
    report.bullet(f"מילים: {len(t.words)}")
    if t.approximate_timing:
        report.missing("תזמון המילים משוער (המנוע החזיר רק זמנים לקטעים), ולכן החיתוכים והכתוביות פחות מדויקים.")
    for start, end in t.gaps:
        report.missing(f"לא תומלל החלק שבין {mmss(start)} ל-{mmss(end)}.")
    print(report.markdown())
    return 0


def cmd_windows(args: argparse.Namespace) -> int:
    state = ProjectState.load(_project(args))
    t = _load_transcript(state)
    windows = selection.build_windows(t)
    page = windows[args.start_window : args.start_window + args.count]
    report = Report("חלונות לדירוג")
    report.bullet(f"סה\"כ חלונות: {len(windows)}. יעד לרשימה הקצרה: {selection.shortlist_target(state.media_duration)}")
    for w in page:
        text = selection.render_indexed_transcript(t, w.start_word, w.end_word)
        report.section(
            f"חלון {w.id}: {mmss(w.start)}-{mmss(w.end)} (מילים {w.start_word}-{w.end_word})", [text]
        )
    nxt = args.start_window + args.count
    if nxt < len(windows):
        report.bullet(f"להמשך: --start-window {nxt}")
    print(report.markdown())
    return 0


def _read_picks(path: Path) -> list[Pick]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("not a list")
        return [
            Pick(int(d["start_word"]), int(d["end_word"]), int(d.get("score", 0)),
                 str(d.get("reason", "")), str(d.get("title", "")), d.get("hook_text"))
            for d in raw
        ]
    except (OSError, ValueError, KeyError, TypeError):
        raise UserFacingError("bad_picks_file") from None


def cmd_refine(args: argparse.Namespace) -> int:
    project = _project(args)
    state = ProjectState.load(project)
    t = _load_transcript(state)
    profile = _profile()
    picks = _read_picks(args.picks)
    options = {
        key: value
        for key, value in dict(lead_s=args.lead_s, tail_s=args.tail_s, snap_s=args.snap_s,
                               sentence_snap_words=args.sentence_snap_words).items()
        if value is not None
    }
    clips, warnings = selection.refine(
        t, picks,
        min_s=args.min_s if args.min_s is not None else profile.min_s,
        max_s=args.max_s if args.max_s is not None else profile.max_s,
        silences=_load_silences(state), **options,
    )
    floor, ceiling = selection.clip_count_targets(len(picks))
    clips = selection.dedupe_and_trim(clips, ceiling)
    state.clips = [{"id": i, **dataclasses.asdict(c)} for i, c in enumerate(clips, start=1)]
    state.save()

    report = Report("הקטעים שנבחרו")
    for i, c in enumerate(clips, start=1):
        excerpt = " ".join(w.text for w in t.words[c.start_word : c.end_word + 1][:8])
        report.bullet(
            f"{i}. {c.title}: {mmss(c.start)}-{mmss(c.end)} ({c.end - c.start:.0f} שניות), "
            f"ציון {c.score}. {c.reason} | \"{excerpt}...\""
        )
    if len(clips) < floor:
        report.bullet(f"נבחרו {len(clips)} קטעים, פחות מהיעד ({floor}). אפשר לבקש עוד.")
    if warnings:
        report.section("קטעים שדילגתי עליהם", [f"- {w}" for w in warnings])
    print(report.markdown())
    return 0


def _hook_modes(args: argparse.Namespace, profile: Profile) -> tuple[HookMode, dict[int, HookMode]]:
    default = HookMode(args.hook or profile.hook_default)
    overrides: dict[int, HookMode] = {}
    for item in (args.hooks or "").split(","):
        if "=" in item:
            clip_id, mode = item.split("=", 1)
            overrides[int(clip_id)] = HookMode(mode.strip())
    return default, overrides


def cmd_correct(args: argparse.Namespace) -> int:
    """Show the caption text of the chosen clips and fix misheard words before they are burned in."""
    project = _project(args)
    state = ProjectState.load(project)
    if not state.clips:
        raise UserFacingError("no_clips")
    t = _load_transcript(state)
    report = Report("טקסט הכתוביות")
    if args.edits:
        try:
            edits = json.loads(Path(args.edits).read_text(encoding="utf-8"))
            fixes = [(int(e["index"]), str(e["text"]).strip()) for e in edits]
        except (OSError, ValueError, KeyError, TypeError):
            raise UserFacingError("bad_edits_file") from None
        for index, text in fixes:
            if not (0 <= index < len(t.words)) or not text or any(ch.isspace() for ch in text):
                raise UserFacingError("bad_edits_file")
        for index, text in fixes:
            word = t.words[index]
            t.words[index] = transcript_mod.Word(text, word.start, word.end)
        transcript_mod.save_transcript(t, Path(state.transcript_path))
        report.bullet(f"תוקנו {len(fixes)} מילים. התזמון נשמר.")
    wanted = {int(x) for x in args.clips.split(",")} if args.clips else None
    for entry in state.clips:
        if wanted and entry["id"] not in wanted:
            continue
        clip = _clip_from(entry)
        report.section(
            f"קליפ {entry['id']}: {clip.title}",
            [selection.render_indexed_transcript(t, clip.start_word, clip.end_word)],
        )
    print(report.markdown())
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    project = _project(args)
    state = ProjectState.load(project)
    if not state.clips:
        raise UserFacingError("no_clips")
    if not Path(state.media_path).exists():
        raise UserFacingError("media_missing")
    t = _load_transcript(state)
    profile = _profile()
    default_hook, hook_overrides = _hook_modes(args, profile)
    wanted = {int(x) for x in args.clips.split(",")} if args.clips else {c["id"] for c in state.clips}
    ws = _workspace(state)
    out_dir = project / "clips"

    report = Report("הקליפים")
    produced = 0
    for entry in state.clips:
        if entry["id"] not in wanted:
            continue
        clip, clip_id = _clip_from(entry), entry["id"]
        progress(f"מרנדר קליפ {clip_id}: {clip.title}...")
        try:
            plan = plan_hook(clip, t, hook_overrides.get(clip_id, default_hook))
            final = render.render_clip(
                Path(state.media_path), clip, plan, t.words, ws,
                preset=args.preset or profile.caption_preset, reframe=args.reframe,
                out_dir=out_dir, title=clip.title, index=clip_id,
                font=args.font or profile.font, has_audio=state.has_audio,
            )
        except UserFacingError as err:
            report.missing(f"קליפ {clip_id} ({clip.title}): {err.message_he}")
            continue
        except Exception as err:  # keep going: one bad clip must not lose the others
            report.missing(f"קליפ {clip_id} ({clip.title}): משהו לא צפוי השתבש ({type(err).__name__}).")
            continue
        produced += 1
        state.rendered[str(clip_id)] = {
            "path": str(final), "segments": [list(s) for s in plan.segments],
            "title_text": plan.title_text, "expected_duration": sum(b - a for a, b in plan.segments),
            "high_risk": plan.high_risk,
        }
        report.bullet(f"קליפ {clip_id}: `{final}`" + (" (פתיח בסיכון גבוה: שונה סדר הדברים)" if plan.high_risk else ""))
    state.save()
    print(report.markdown())
    return 0 if produced else 1


def cmd_qa(args: argparse.Namespace) -> int:
    project = _project(args)
    state = ProjectState.load(project)
    if not state.rendered:
        raise UserFacingError("no_rendered")
    t = _load_transcript(state)
    wanted = {int(x) for x in args.clips.split(",")} if args.clips else None
    report = Report("בדיקת איכות")
    for entry in state.clips:
        info = state.rendered.get(str(entry["id"]))
        if not info or (wanted and entry["id"] not in wanted):
            continue
        clip = _clip_from(entry)
        video = Path(info["path"])
        try:
            segments = [tuple(s) for s in info["segments"]]
            blocks = captions.group_words(captions.remap_words(t.words, segments), 3)
            cues = qa.caption_cue_times(blocks)
            if len(cues) > 6:
                cues = [cues[round(i * (len(cues) - 1) / 5)] for i in range(6)]
            duration = info["expected_duration"]
            times = sorted({0.2, *cues, max(0.2, duration - 0.3)})
            qa_dir = project / "qa" / f"clip_{entry['id']:02d}"
            frames = qa.extract_frames(video, times, qa_dir)
            sheet = qa.contact_sheet(frames, qa_dir / "sheet.png")
            findings = qa.auto_checks(video, duration, clip, t)
            report.raw(f"## קליפ {entry['id']}: {clip.title}\n" + qa.qa_report_markdown(frames, sheet, findings))
        except UserFacingError as err:
            report.missing(f"קליפ {entry['id']}: {err.message_he}")
        except Exception as err:
            report.missing(f"קליפ {entry['id']}: הבדיקה נכשלה ({type(err).__name__}).")
    print(report.markdown())
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    state = ProjectState.load(_project(args))
    report = Report("ניקוי")
    if state.workspace:
        workspace_path = Path(state.workspace)
        if state.media_path and workspace_path in Path(state.media_path).parents:
            report.bullet("הסרטון הורד מקישור ונמחק יחד עם תיקיית העבודה. להמשך עבודה עליו צריך להוריד אותו שוב (ingest).")
            state.media_path = ""
        Workspace(workspace_path).cleanup()
        state.workspace = ""
        state.save()
    report.bullet("תיקיית העבודה הזמנית נמחקה. הקליפים והתמלול נשארו.")
    print(report.markdown())
    return 0


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="make_shorts.py")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, **kw) -> argparse.ArgumentParser:
        p = sub.add_parser(name, **kw)
        p.add_argument("--project", help="תיקיית הפרויקט (ברירת מחדל: make-shorts-project בתיקייה הנוכחית)")
        p.set_defaults(func=func)
        return p

    p = add("setup", cmd_setup)
    p.add_argument("--json", action="store_true")
    p.add_argument("--finish", action="store_true", help="לסמן שההגדרה הראשונית הסתיימה (דורש פרופיל תקין)")
    p.add_argument("--skip-selftest", action="store_true")

    p = add("ingest", cmd_ingest)
    p.add_argument("source", help="קישור או נתיב לקובץ וידאו")

    p = add("transcribe", cmd_transcribe)
    p.add_argument("--engine")

    p = add("windows", cmd_windows)
    p.add_argument("--start-window", type=int, default=0)
    p.add_argument("--count", type=int, default=10)

    p = add("refine", cmd_refine)
    p.add_argument("--picks", required=True)
    p.add_argument("--min-s", type=float)
    p.add_argument("--max-s", type=float)
    p.add_argument("--lead-s", type=float)
    p.add_argument("--tail-s", type=float)
    p.add_argument("--snap-s", type=float)
    p.add_argument("--sentence-snap-words", type=int)

    p = add("correct", cmd_correct)
    p.add_argument("--clips")
    p.add_argument("--edits", help='קובץ JSON עם תיקוני מילים: [{"index": 12, "text": "מילה"}]')

    p = add("render", cmd_render)
    p.add_argument("--clips", help="מספרי קליפים מופרדים בפסיק (ברירת מחדל: כולם)")
    p.add_argument("--hook", choices=[m.value for m in HookMode])
    p.add_argument("--hooks", help='פתיח לכל קליפ, למשל "1=teaser,2=title"')
    p.add_argument("--preset", choices=list(captions.PRESETS))
    p.add_argument("--font", choices=list(captions.FONTS))
    p.add_argument("--reframe", choices=["center", "blur"], default="blur")

    p = add("qa", cmd_qa)
    p.add_argument("--clips")

    add("cleanup", cmd_cleanup)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UserFacingError as err:
        print(err.message_he, file=sys.stderr)
        return err.exit_code
    except Exception as err:
        print(
            f"משהו לא צפוי השתבש ({type(err).__name__}). אפשר לנסות שוב, ואם זה חוזר כדאי לספר מה עשית לפני כן.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
