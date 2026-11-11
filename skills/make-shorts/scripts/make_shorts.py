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
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import acquire
import audio as audio_mod
import captions
import clipplan
import config
import engines
import feedback
import facecrop
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
    """Cheap identity for a big file: size and the first and last megabyte (not the time, so a re-download matches)."""
    stat = path.stat()
    digest = hashlib.sha256(f"{stat.st_size}".encode())
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
    status["key_present"] = config.has_setting("GROQ_API_KEY")  # presence only, never the value
    if args.prepare_key:
        key_file = config.prepare_key_file("GROQ_API_KEY")
        status["key_file"] = str(key_file)
        if os.name == "nt":
            try:
                os.startfile(str(key_file))  # opens Notepad on the file so the user can paste the key
            except OSError:
                pass
    status["profile_exists"] = (config.config_dir() / "profile.json").exists()
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
    warning = acquire.quality_warning(info)
    if warning:
        report.missing(warning)
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
    cached = None
    if tpath.exists() and not args.force:
        cached = transcript_mod.load_transcript(tpath)
        if cached.gaps:
            progress("בתמלול השמור חסרים חלקים. מתמלל מחדש כדי להשלים אותם.")
            cached = None

    if cached is not None and spath.exists():
        progress("משתמש בתמלול שמור מהרצה קודמת.")
        t = cached
    else:
        ws = _workspace(state)
        audio_path = ws.path / "audio.mp3"
        if not audio_path.exists():
            progress("מחלץ את האודיו מהסרטון...")
            audio_mod.extract_audio(media, audio_path)
        if cached is not None:
            t = cached
        else:
            engine = engines.get_engine(name)
            t = engine.transcribe(audio_path, language=profile.language, progress=progress)
            transcript_mod.save_transcript(t, tpath)  # saved at once: an upload is expensive to repeat
        try:
            progress("מזהה שתיקות...")
            silences = audio_mod.detect_silences(audio_path)
            t.words = transcript_mod.drop_words_in_trailing_silence(
                t.words, silences, audio_mod.probe_duration(audio_path)
            )
            transcript_mod.save_transcript(t, tpath)
            spath.write_text(json.dumps(silences), encoding="utf-8")
        except UserFacingError as err:
            report.missing(
                f"לא הצלחתי לזהות שתיקות ({err.message_he}). אפשר להמשיך, אבל החיתוכים יהיו פחות מדויקים."
            )

    state.engine, state.transcript_path = name, str(tpath)
    state.silences_path = str(spath) if spath.exists() else ""
    state.save()
    report.bullet(f"מנוע: {name}, שפה: {profile.language}")
    report.bullet(f"מילים: {len(t.words)}")
    if t.approximate_timing:
        report.missing("תזמון המילים משוער (המנוע החזיר רק זמנים לקטעים), ולכן החיתוכים והכתוביות פחות מדויקים.")
    for start_s, end_s in t.gaps:
        report.missing(f"לא תומלל החלק שבין {mmss(start_s)} ל-{mmss(end_s)}. הרצה חוזרת של transcribe תנסה להשלים אותו.")
    for a, b in transcript_mod.odd_stretches(t.words, profile.language):
        report.missing(f"מילים {a}-{b} ({mmss(t.words[a].start)}-{mmss(t.words[b].end)}): אותיות משפה אחרת או סימני מנוע "
                       "בתמלול. אולי מדברים שם בשפה אחרת, ואולי המנוע המציא טקסט. לא לבחור קטע משם בלי לבדוק.")
    if not t.words:
        report.missing("לא זוהה דיבור בסרטון (אולי יש בו רק מוזיקה או שקט), ולכן אין ממה לבחור קטעים. כדאי לנסות סרטון אחר.")
        print(report.markdown())
        return 1
    print(report.markdown())
    return 0


def cmd_windows(args: argparse.Namespace) -> int:
    state = ProjectState.load(_project(args))
    t = _load_transcript(state)
    windows = selection.build_windows(t)
    page = windows[args.start_window : args.start_window + args.count]
    report = Report("חלונות לדירוג")
    report.bullet(f"סה\"כ חלונות: {len(windows)}. יעד לרשימה הקצרה: {selection.shortlist_target(state.media_duration)}")
    if not windows:
        report.bullet("אין חלונות: בתמלול אין מילים, ולכן אין ממה לבחור.")
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
    state.rendered = {}  # numbers now mean different clips; old renders must not be reused
    state.save()

    report = Report("הקטעים שנבחרו")
    for i, c in enumerate(clips, start=1):
        excerpt = " ".join(w.text for w in t.words[c.start_word : c.end_word + 1][:8])
        report.bullet(
            f"{i}. {c.title}: {mmss(c.start)}-{mmss(c.end)} ({c.end - c.start:.0f} שניות), "
            f"ציון {c.score}. {c.reason} | \"{excerpt}...\""
        )
    moved = []
    for c in clips:  # a silently shortened end once cut the punchline; say so every time
        p = next((p for p in picks if p.title == c.title), None)
        if p is None:
            continue
        if c.end_word < p.end_word:
            cut = " ".join(w.text for w in t.words[c.end_word + 1 : p.end_word + 1])
            moved.append(f"- \"{c.title}\": הסוף קוצר מ-[{p.end_word}] ל-[{c.end_word}], והמילים \"{cut}\" יצאו. "
                         "אם הן חלק מהנקודה, החזר אותן דרך parts ב-plan.json.")
        elif c.end_word > p.end_word:
            moved.append(f"- \"{c.title}\": הסוף הוארך מ-[{p.end_word}] ל-[{c.end_word}] (סוף המשפט).")
        if c.start_word != p.start_word:
            moved.append(f"- \"{c.title}\": ההתחלה הוזזה מ-[{p.start_word}] ל-[{c.start_word}] (תחילת המשפט).")
    if moved:
        report.section("גבולות שהזזתי (השווה לבחירה שלך)", moved)
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
    if getattr(args, "recaptioned", False):
        if not state.rendered:
            raise UserFacingError("no_rendered")
        return _correct_recaptioned(args, project, state)
    if not state.clips and not args.glossary:
        raise UserFacingError("no_clips")
    t = _load_transcript(state)
    report = Report("טקסט הכתוביות")
    if args.glossary:
        mapping = read_glossary(Path(args.glossary))
        for warning in transcript_mod.glossary_warnings(mapping):
            report.bullet(warning)
        before = [w.text for w in t.words]
        t.words = transcript_mod.apply_glossary(t.words, mapping)
        changed = sum(1 for a, w in zip(before, t.words) if a != w.text)
        transcript_mod.save_transcript(t, Path(state.transcript_path))
        report.bullet(f"המילון הוחל על כל התמלול: {changed} מילים תוקנו. התזמון והאינדקסים נשמרו.")
    if args.edits:
        try:
            edits = json.loads(Path(args.edits).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise UserFacingError("bad_edits_file") from None
        t.words = transcript_mod.apply_edits(t.words, edits)
        transcript_mod.save_transcript(t, Path(state.transcript_path))
        record_word_fixes(project, edits)  # recaption carries these over to the new words
        report.bullet(f"תוקנו {len(edits)} מילים. התזמון נשמר, והתיקונים יעברו גם לתמלול מחדש (recaption).")
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


def _correct_recaptioned(args: argparse.Namespace, project: Path, state: ProjectState) -> int:
    """Show and fix the re-transcribed words of clips (clip_words/NN.json) by index, the way
    `correct` does for the transcript. Edits: [{"clip": N, "index": i, "text": "..."}]; "clip" may
    be left out when --clips names a single clip."""
    wanted = sorted({int(x) for x in args.clips.split(",")}) if args.clips else sorted(int(k) for k in state.rendered)
    report = Report("טקסט הכתוביות (אחרי תמלול מחדש)")
    if args.edits:
        try:
            edits = json.loads(Path(args.edits).read_text(encoding="utf-8"))
            if not isinstance(edits, list):
                raise ValueError
        except (OSError, ValueError):
            raise UserFacingError("bad_edits_file") from None
        by_clip: dict[int, list[dict]] = {}
        for e in edits:
            clip_id = e.get("clip") if isinstance(e, dict) else None
            if clip_id is None:
                if len(wanted) != 1:
                    raise UserFacingError("bad_edits_file")
                clip_id = wanted[0]
            by_clip.setdefault(int(clip_id), []).append(e)
        for clip_id, items in sorted(by_clip.items()):
            path = _clip_words_path(project, clip_id)
            if not path.exists():
                report.missing(f"קליפ {clip_id}: אין לו כתוביות מתמלול מחדש. הרץ קודם recaption.")
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            words = [transcript_mod.Word(str(r["text"]), float(r["start"]), float(r["end"])) for r in raw["words"]]
            words = transcript_mod.apply_edits(words, items)
            save_clip_words(project, clip_id, words, [tuple(s) for s in raw["segments"]])
            report.bullet(f"קליפ {clip_id}: תוקנו {len(items)} מילים. כדי לראות אותן בקליף, הרץ render --recaptioned.")
    for clip_id in wanted:
        path = _clip_words_path(project, clip_id)
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw["words"] if isinstance(raw, dict) else raw
        text = " ".join(f"[{i}]{r['text']}" for i, r in enumerate(items) if r["text"])
        report.section(f"קליפ {clip_id}", [text])
    print(report.markdown())
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    project = _project(args)
    state = ProjectState.load(project)
    if not state.clips:
        raise UserFacingError("no_clips")
    if not state.media_path:
        raise UserFacingError("no_media")
    if not Path(state.media_path).exists():
        raise UserFacingError("media_missing")
    t = _load_transcript(state)
    profile = _profile()
    default_hook, hook_overrides = _hook_modes(args, profile)
    wanted = {int(x) for x in args.clips.split(",")} if args.clips else {c["id"] for c in state.clips}
    plans = clipplan.load_plan(Path(args.plan)) if args.plan else {}
    ws = _workspace(state)
    out_dir = project / "clips"

    report = Report("הקליפים")
    produced = 0
    for entry in state.clips:
        if entry["id"] not in wanted:
            continue
        clip, clip_id = _clip_from(entry), entry["id"]
        progress(f"מרנדר קליפ {clip_id}: {clip.title}...")
        for old in out_dir.glob(f"{clip_id:02d} - *"):  # drop files from an earlier selection
            old.unlink(missing_ok=True)
        try:
            cplan = plans.get(clip_id, clipplan.ClipPlan())
            base_segments = None
            if not args.keep_cuts:
                chosen = cplan.parts or [(clip.start_word, clip.end_word)]
                cplan.parts = clipplan.finish_sentences(t, chosen)
                for number, ((_, b0), (_, b1)) in enumerate(zip(chosen, cplan.parts), start=1):
                    if b1 > b0:
                        label = f"חלק {number}" if len(chosen) > 1 else "הסוף"
                        report.bullet(f"קליפ {clip_id}: {label} הוארך עד סוף המשפט, עד המילה [{b1}] \"{t.words[b1].text}\". "
                                      "אם זה נכנס לקטע שלא רצית, הרץ עם --keep-cuts.")
            if cplan.parts:
                base_segments = clipplan.segments_from_parts(
                    t, cplan.parts, quietest=audio_mod.waveform_quietest(Path(state.media_path)) if state.has_audio else None
                )
                lo = min(a for a, _ in cplan.parts)
                hi = max(b for _, b in cplan.parts)
                clip = dataclasses.replace(clip, start_word=lo, end_word=hi,
                                           start=base_segments[0][0], end=base_segments[-1][1])
            for warning in clipplan.check_parts(t, cplan.parts or [(clip.start_word, clip.end_word)]):
                report.bullet(f"קליפ {clip_id}, שים לב: {warning}")
            if cplan.hook_text:
                if cplan.parts and not clipplan.quote_in_parts(t, cplan.parts, cplan.hook_text):
                    raise UserFacingError("hook_quote_not_verbatim")  # a teaser must be heard in the clip
                clip = dataclasses.replace(clip, hook_text=cplan.hook_text)
            mode = HookMode(cplan.hook) if cplan.hook else hook_overrides.get(clip_id, default_hook)
            plan = plan_hook(clip, t, mode, segments=base_segments)
            preset_name = args.preset or profile.caption_preset
            zoom = None
            push = None
            split = None
            video_segments = None
            crop = clipplan.crop_for_segments(plan.segments, base_segments or [(clip.start, clip.end)], cplan.crop_x)
            if cplan.crop_x and args.reframe == "center":
                report.bullet(f"קליפ {clip_id}: בתוכנית יש crop_x, ולכן המסגור האוטומטי כבוי (מעקב פנים, מסך מפוצל, "
                              "החלפה בחילוף מצלמה). אם זה לא בכוונה, מחק את crop_x מ-plan.json.")
            if not cplan.crop_x and args.reframe == "center" and not args.no_auto_crop:
                if facecrop.available():
                    progress(f"קליפ {clip_id}: מזהה פנים ומחליט איפה לחתוך...")
                    try:
                        trimmed, video, crop, push, split = facecrop.auto_crops(
                            Path(state.media_path), plan.segments, ws.path, words=t.words
                        )
                        plan = dataclasses.replace(plan, segments=trimmed)
                        if video != trimmed:
                            video_segments = video
                            report.bullet(f"קליפ {clip_id}: בחיבורים, התמונה מתחלפת יחד עם חילוף מצלמה סמוך, והקול ממשיך עד החיבור.")
                        if not (args.slow_zoom or profile.slow_zoom):
                            push = None  # off by default: on a talking podcast it read as too strong
                        elif any(push):
                            report.bullet(f"קליפ {clip_id}: {sum(len(p) for p in push)} צילומים כמעט קפואים (מישהו מקשיב בשקט), הוספתי להם זום איטי.")
                        if any(split):
                            report.bullet(f"קליפ {clip_id}: {sum(len(s) for s in split)} צילומים רחבים של שניים, הוצגו במסך מפוצל (אחד למעלה ואחד למטה).")
                    except Exception as err:  # keep the fixed crop rather than lose the clip
                        report.bullet(f"קליפ {clip_id}: זיהוי הפנים נכשל ({type(err).__name__}), נעשה חיתוך מרכזי.")
                    if len(plan.segments) > 1:
                        try:
                            same = facecrop.same_shot_joins(Path(state.media_path), video_segments or plan.segments)
                            zoom = facecrop.punch_in_zooms(same, strength=profile.punch_in)
                            if any(same) and profile.punch_in > 1.0:
                                report.bullet(f"קליפ {clip_id}: {sum(same)} חיבורים בתוך אותו צילום, הוספתי זום קל כדי שלא ייראו כקפיצה.")
                        except Exception as err:  # only the punch-in is lost; the face crop stays
                            report.bullet(f"קליפ {clip_id}: בדיקת החיבורים נכשלה ({type(err).__name__}), בלי זום בחיבורים.")
                else:
                    report.bullet("זיהוי פנים לא זמין (חסר OpenCV או קובץ המודל), נעשה חיתוך מרכזי.")
            # every part a whole number of frames, picture and sound alike, so joins never drift apart
            src_fps = facecrop._probe_fps(Path(state.media_path))
            aligned = clipplan.frame_aligned(plan.segments, src_fps)
            if video_segments is not None:
                video_segments = clipplan.frame_aligned_video(aligned, plan.segments, video_segments, src_fps)
            plan = dataclasses.replace(plan, segments=aligned)
            clip_words = None
            if args.recaptioned:
                last = state.rendered.get(str(clip_id), {}).get("segments")
                clip_words = _load_clip_words(project, clip_id, plan.segments, fallback_segments=last)
                if clip_words is not None:  # stored against these segments from now on
                    save_clip_words(project, clip_id, clip_words, plan.segments)
            final = render.render_clip(
                Path(state.media_path), clip, plan, t.words, ws,
                preset=preset_name, reframe=args.reframe,
                out_dir=out_dir, title=clip.title, index=clip_id,
                font=args.font or profile.font, has_audio=state.has_audio, crop_x=crop,
                caption_words=clip_words,
                zoom=zoom, push=push, split=split, video_segments=video_segments,
                fps=src_fps,
                fade_to_black=profile.fade_to_black, hide_bottom=args.hide_bottom,
            )
        except UserFacingError as err:
            report.missing(f"קליפ {clip_id} ({clip.title}): {err.message_he}")
            continue
        except Exception as err:  # keep going: one bad clip must not lose the others
            report.missing(f"קליפ {clip_id} ({clip.title}): משהו לא צפוי השתבש ({type(err).__name__}).")
            continue
        produced += 1
        checks = []  # one fixed line per clip: silence must never be the only sign a check passed
        try:
            found = _render_check(final, state.has_audio)
            for finding in found:
                report.missing(f"קליפ {clip_id}: {finding}")
            checks.append("בדיקת רינדור: עברה" if not found else f"בדיקת רינדור: {len(found)} תקלות (ראה ראיות חסרות)")
        except Exception as err:  # the check must never lose a rendered clip, nor pass for clean
            report.missing(f"קליפ {clip_id}: בדיקת הרינדור לא רצה ({type(err).__name__}), אז אין לדעת אם הקליף תקין.")
            checks.append("בדיקת רינדור: לא רצה")
        if args.reframe == "center":
            try:
                moves = _motion_check(final, video_segments or plan.segments, crop, zoom)
                for finding in moves:
                    report.bullet(f"קליפ {clip_id}, בדיקת קפיצות: {finding}")
                checks.append(f"בדיקת קפיצות: {len(moves)} ממצאים")
            except Exception as err:  # the check must never lose a rendered clip
                report.bullet(f"קליפ {clip_id}: בדיקת הקפיצות לא רצה ({type(err).__name__}).")
                checks.append("בדיקת קפיצות: לא רצה")
        joins = len(plan.segments) - 1
        if joins:
            punched = sum(1 for i in range(joins) if zoom and zoom[i] != zoom[i + 1])
            checks.append(f"חיבורים: {joins}" + (f" ({punched} עם זום קל)" if punched else ""))
        for finding in qa.edge_findings(t.words, plan.segments):
            report.bullet(f"קליפ {clip_id}: {finding}")
        report.bullet(f"קליפ {clip_id}: " + ". ".join(checks) + ".")
        state.rendered[str(clip_id)] = {
            "path": str(final), "segments": [list(s) for s in plan.segments],
            "title_text": plan.title_text, "expected_duration": sum(b - a for a, b in plan.segments),
            "high_risk": plan.high_risk, "preset": preset_name,
        }
        report.bullet(f"קליפ {clip_id}: `{final}`" + (" (פתיח בסיכון גבוה: שונה סדר הדברים)" if plan.high_risk else ""))
        state.save()
    state.save()
    print(report.markdown())
    return 0 if produced else 1


def measure_render(path: Path, has_audio: bool = True):
    """(picture seconds, decoded sound seconds or None, frame-to-frame differences) of a finished clip.
    The sound is decoded rather than read from the file's header, which is wrong after loudnorm."""
    import subprocess

    import numpy as np

    fps = facecrop._probe_fps(path)
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0",
                          "-fps_mode", "passthrough", "-vf", "scale=64:36,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, timeout=600).stdout
    frames = np.frombuffer(raw, np.uint8)
    frames = frames[: len(frames) // 2304 * 2304].reshape(-1, 2304).astype(np.float32)
    if len(frames) < 2:  # an unreadable or missing clip must never pass as clean
        raise UserFacingError("unknown", detail=f"לא הצלחתי לקרוא את הקליף `{path}`.")
    diffs = [float(v) for v in np.abs(np.diff(frames, axis=0)).mean(axis=1)]
    audio_s = None
    if has_audio:
        sound = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                                "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=600).stdout
        audio_s = len(sound) / 2 / 8000
    return len(frames) / fps, audio_s, diffs, fps


def _packet_times(path: Path, stream: str) -> list[float]:
    """Timestamps of a stream's packets, as a player reads them from the file."""
    import subprocess

    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=pts_time",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=300).stdout.split()
    return [float(v.strip(",")) for v in out if v.strip(",") not in ("", "N/A")]


def _sound_packet_times(path: Path) -> tuple[list[float], float]:
    import subprocess

    rate = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60).stdout.strip()
    try:
        frame_s = 1024 / float(rate)
    except ValueError:
        frame_s = 1024 / 48000
    return _packet_times(path, "a:0"), frame_s


def _render_check(final: Path, has_audio: bool) -> list[str]:
    """Faults the render can cause, measured the way a player meets the file: lengths, held frames,
    and the timestamps of every packet. Decoding to samples or frames hides bad timestamps (seen: a
    clip whose samples were perfect stalled in every player), so the timestamps are read directly."""
    video_s, audio_s, diffs, fps = measure_render(final, has_audio)
    found = qa.render_findings(video_s, audio_s, qa.duplicate_runs(diffs, fps))
    video_pts = _packet_times(final, "v:0")
    found += qa.picture_timestamp_findings(video_pts, 1 / fps)
    if has_audio:
        pts, frame_s = _sound_packet_times(final)
        found += qa.sound_timestamp_findings(pts, frame_s)
        found += qa.start_findings(min(video_pts) if video_pts else 0.0, min(pts) if pts else None)
    return found


def _motion_check(final: Path, segments, crop, zoom) -> list[str]:
    """Scan the finished clip for flashes, jumps at joins and a crop stuck at the edge."""
    fps = facecrop._probe_fps(final)
    duration = sum(b - a for a, b in segments)
    fade_end, fade_start = render.FADE_IN_S + 0.1, duration - render.FADE_OUT_S - 0.1
    cuts = [c for c in facecrop.exact_cuts(final, 0.0, duration, fps) if fade_end < c < fade_start]
    joins, steps, offset = [], [], 0.0
    for i, (a, b) in enumerate(segments):
        seg_crop = crop[i] if isinstance(crop, list) and i < len(crop) else crop
        if isinstance(seg_crop, list):
            steps += [(offset + s[0], s[1]) for s in seg_crop]
        elif seg_crop is not None:
            steps.append((offset, float(seg_crop)))
        offset += b - a
        if i + 1 < len(segments):
            before, after = facecrop._thumb_at(final, offset - 1.5 / fps), facecrop._thumb_at(final, offset + 0.5 / fps)
            same_zoom = not zoom or zoom[i] == zoom[i + 1]
            diff = float(abs(before - after).mean()) if before is not None and after is not None else 99.0
            joins.append((round(offset, 2), same_zoom and facecrop.looks_like_jump(diff)))
    return qa.motion_findings(cuts, joins, duration, steps)


def _clip_words_path(project: Path, clip_id: int) -> Path:
    return project / "clip_words" / f"{clip_id:02d}.json"


def hint_leaked(words, hint: str, n: int = 3) -> bool:
    """Whisper sometimes "hears" its prompt instead of the audio. Any run of n hint words, in order,
    inside the result means the hint leaked."""
    def norm(s: str) -> str:
        return s.strip("\"'”’)»(.,!?;:…-–").casefold()

    tokens = [norm(x) for x in hint.split() if norm(x)]
    grams = {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}
    got = [norm(w.text) for w in words if norm(w.text)]
    return any(tuple(got[i:i + n]) in grams for i in range(len(got) - n + 1))


_HALLUCINATED_TAILS = (("תודה", "רבה"), ("תודה", "שצפיתם"), ("תודה",))
HOLE_S = 2.0


def merge_holes(new, old, duration: float, min_hole: float = HOLE_S):
    """A re-transcription can stop early or skip a stretch (Whisper then often "hears" a closing
    'תודה רבה'). Drop such a tail, and fill every gap longer than `min_hole` with the old words
    that fall inside it, so no speech is left without captions."""
    def norm(s: str) -> str:
        return s.strip("\"'”’)»(.,!?;:…-–")

    words = list(new)
    for tail in _HALLUCINATED_TAILS:
        if len(words) >= len(tail) and tuple(norm(w.text) for w in words[-len(tail):]) == tail:
            lo, hi = words[-len(tail)].start, words[-1].end
            real = any(norm(w.text) == "תודה" and w.end > lo and w.start < hi for w in old)
            if not real:  # the original transcript has no "thanks" there: it was invented
                words = words[: -len(tail)]
            break
    edges = [0.0] + [x for w in words for x in (w.start, w.end)] + [duration]
    holes = [(edges[i], edges[i + 1]) for i in range(0, len(edges) - 1, 2) if edges[i + 1] - edges[i] > min_hole]
    if not holes:
        return words if len(words) != len(new) else new
    fill = [w for w in old if any(a - 1e-6 <= w.start and w.end <= b + 1e-6 for a, b in holes)]
    return sorted(words + fill, key=lambda w: w.start)


def cmd_recaption(args: argparse.Namespace) -> int:
    """Transcribe each finished clip again, on its own, with names and context as a hint.

    A short clean file is transcribed far better than an hour-long chunk, and the words come
    already on the clip's timeline. The result is saved for review and used by `render --recaptioned`.
    """
    project = _project(args)
    state = ProjectState.load(project)
    if not state.rendered:
        raise UserFacingError("no_rendered")
    profile = _profile()
    if not profile.cloud_allowed:
        raise UserFacingError("unknown", detail="תמלול מחדש של קליפים דורש שירות ענן, והוא לא מאושר בפרופיל.")
    hint = ""
    if args.prompt:
        hint = Path(args.prompt).read_text(encoding="utf-8").strip()
    mapping = read_glossary(Path(args.glossary)) if args.glossary else {}
    wanted = {int(x) for x in args.clips.split(",")} if args.clips else None
    ws = _workspace(state)
    t = _load_transcript(state)
    fixes = load_word_fixes(project)
    engine = engines.get_engine("groq")
    report = Report("כתוביות מחדש")
    for warning in transcript_mod.glossary_warnings(mapping):
        report.bullet(warning)
    for clip_id_s, info in sorted(state.rendered.items(), key=lambda kv: int(kv[0])):
        clip_id = int(clip_id_s)
        if wanted and clip_id not in wanted:
            continue
        progress(f"מתמלל מחדש את קליפ {clip_id}...")
        mp3 = audio_mod.extract_audio(Path(info["path"]), ws.path / f"recaption{clip_id:02d}.mp3")
        words = engine.transcribe_short(mp3, language=profile.language, prompt=hint or None)
        if hint and hint_leaked(words, hint):
            progress(f"קליפ {clip_id}: התמלול העתיק את הרמז במקום הדיבור, מתמלל שוב בלי רמז...")
            words = engine.transcribe_short(mp3, language=profile.language, prompt=None)
            report.bullet(f"קליפ {clip_id}: תומלל בלי רמז השמות, כי עם הרמז הוא הוחלף בטקסט הרמז עצמו.")
        old = captions.remap_words(t.words, [tuple(s) for s in info["segments"]])
        merged = merge_holes(words, old, float(info["expected_duration"]))
        if merged is not words:
            report.bullet(f"קליפ {clip_id}: בתמלול החדש היו חורים או סיום מומצא, והם מולאו מהתמלול המקורי. כדאי לבדוק את המקומות האלה.")
        words = merged
        if mapping:
            words = transcript_mod.apply_glossary(words, mapping)
        if fixes:
            words, carried, lost = carry_word_fixes(words, t.words, fixes, [tuple(s) for s in info["segments"]])
            if carried or lost:
                report.bullet(f"קליפ {clip_id}: {carried} תיקוני מילים מ-correct הועברו לתמלול החדש"
                              + (f". לא מצאתי איפה לשים: {', '.join(lost)}. תקן אותם ב-correct --recaptioned." if lost else "."))
        target = save_clip_words(project, clip_id, words, [tuple(s) for s in info["segments"]])
        report.bullet(f"קליפ {clip_id}: {len(words)} מילים, נשמר ב-`{target}`")
    print(report.markdown())
    return 0


def read_glossary(path: Path) -> dict[str, str]:
    try:
        mapping = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            raise ValueError("not a mapping")
    except (OSError, ValueError):
        raise UserFacingError("bad_edits_file") from None
    return mapping


WORD_FIXES_FILE = "word_fixes.json"


def _word_fixes_path(project: Path) -> Path:
    return project / WORD_FIXES_FILE


def load_word_fixes(project: Path) -> dict[int, str]:
    """Every `correct --edits` fix, by transcript index. Kept so a later recaption can carry them
    over: re-transcribing a clip used to bring nearly every fixed word back wrong."""
    path = _word_fixes_path(project)
    if not path.exists():
        return {}
    try:
        return {int(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def record_word_fixes(project: Path, edits: list[dict]) -> None:
    fixes = load_word_fixes(project)
    for e in edits:
        fixes[int(e["index"])] = str(e["text"]).strip()
    _word_fixes_path(project).write_text(json.dumps({str(k): v for k, v in sorted(fixes.items())}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")


def carry_word_fixes(new_words, source_words, fixes: dict[int, str], segments):
    """Put each recorded fix on the re-transcribed word heard at the same moment. The fixed word is
    moved to the clip's timeline and matched to the new word that overlaps it most (at least half of
    the shorter of the two). Returns (words, carried, not placed)."""
    words = list(new_words)
    carried, lost = 0, []
    for index, text in sorted(fixes.items()):
        if not 0 <= index < len(source_words):
            continue
        src = source_words[index]
        mapped = captions.remap_words([transcript_mod.Word(src.text or "x", src.start, src.end)], segments)
        if not mapped:
            continue  # this fix is outside the clip
        m = mapped[0]
        best, best_overlap = None, 0.0
        for j, w in enumerate(words):
            overlap = min(w.end, m.end) - max(w.start, m.start)
            if overlap > best_overlap:
                best, best_overlap = j, overlap
        shorter = min(m.end - m.start, (words[best].end - words[best].start) if best is not None else 0.0)
        if best is None or best_overlap < 0.5 * max(shorter, 1e-6):
            lost.append(text)
            continue
        if words[best].text != text:
            words[best] = transcript_mod.Word(text, words[best].start, words[best].end)
        carried += 1
    return words, carried, lost


def save_clip_words(project: Path, clip_id: int, words, segments) -> Path:
    """Clip captions are kept with the segments (source time) they were timed against, so a later
    render with moved edges can move them along instead of showing them out of sync."""
    target = _clip_words_path(project, clip_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "segments": [[float(a), float(b)] for a, b in segments],
        "words": [{"text": w.text, "start": w.start, "end": w.end} for w in words],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


def _load_clip_words(project: Path, clip_id: int, segments, fallback_segments=None):
    """The clip's corrected words on the timeline of `segments`. Files from before segments were
    stored are a bare list, timed against the last render (`fallback_segments`)."""
    path = _clip_words_path(project, clip_id)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    stored = raw.get("segments") if isinstance(raw, dict) else fallback_segments
    items = raw["words"] if isinstance(raw, dict) else raw
    words = [transcript_mod.Word(str(r["text"]), float(r["start"]), float(r["end"])) for r in items]
    if stored:
        old = [(float(a), float(b)) for a, b in stored]
        new = [(float(a), float(b)) for a, b in segments]
        if any(abs(x - y) > 1e-3 for p, q in zip(old, new) for x, y in zip(p, q)) or len(old) != len(new):
            words = captions.retime_words(words, old, new)
    return words


def cmd_feedback(args: argparse.Namespace) -> int:
    """Record the user's verdict on rendered clips in the project's decision log."""
    project = _project(args)
    state = ProjectState.load(project)
    single = args.clip is not None or args.outcome is not None
    if args.file and single:
        raise UserFacingError("bad_feedback", reason="אפשר לתת --file או --clip עם --outcome, לא את שניהם")
    if args.file:
        try:
            raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise UserFacingError("bad_feedback", reason="אי אפשר לקרוא את קובץ המשוב (צריך רשימת JSON)") from None
        if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
            raise UserFacingError("bad_feedback", reason="הקובץ צריך להכיל רשימה של אובייקטים")
        try:
            verdicts = [feedback.Verdict(**x) for x in raw]
        except TypeError:
            raise UserFacingError("bad_feedback", reason="שדות לא מוכרים או חסרים בקובץ המשוב") from None
    elif args.clip is not None and args.outcome is not None:
        verdicts = [feedback.Verdict(args.clip, args.outcome, args.kind, args.category, args.reason or "")]
    else:
        raise UserFacingError("bad_feedback", reason="צריך --clip ו---outcome, או --file")
    if not verdicts:
        raise UserFacingError("bad_feedback", reason="אין פסקי דין לרשום")
    for v in verdicts:  # check everything before writing anything
        feedback.validate_verdict(v)
        if str(v.clip) not in state.rendered:
            raise UserFacingError("bad_feedback", reason=f"קליפ {v.clip} לא רונדר בפרויקט הזה")
    for v in verdicts:
        feedback.append_verdict(project, v)
    feedback.register_project(project, config.config_dir() / feedback.REGISTRY_NAME)
    names = {"approved": "אושר", "changed": "דורש שינוי", "rejected": "נדחה"}
    report = Report("משוב נרשם")
    for v in verdicts:
        detail = f" ({v.kind}/{v.category})" if v.kind else ""
        reason = f": {v.reason}" if v.reason else ""
        report.bullet(f"קליפ {v.clip}: {names[v.outcome]}{detail}{reason}")
    print(report.markdown())
    return 0


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
            words_per_block = captions.PRESETS[info.get("preset", "plain")].words_per_block
            blocks = captions.make_blocks(captions.remap_words(t.words, segments), words_per_block)
            cues = qa.caption_cue_times(blocks)
            if len(cues) > 6:
                cues = [cues[round(i * (len(cues) - 1) / 5)] for i in range(6)]
            duration = info["expected_duration"]
            times = sorted({0.2, *cues, max(0.2, duration - 0.3)})
            qa_dir = project / "qa" / f"clip_{entry['id']:02d}"
            frames = qa.extract_frames(video, times, qa_dir)
            sheet = qa.contact_sheet(frames, qa_dir / "sheet.png")
            findings = qa.auto_checks(video, duration, clip, t, segments=segments)
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
            source_dir = state.project / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            kept = source_dir / Path(state.media_path).name
            shutil.move(state.media_path, kept)
            state.media_path = str(kept)
            report.bullet(f"הסרטון שהורד נשמר ב-`{source_dir}`, כך שאפשר להמשיך לעבוד עליו בלי להוריד ולתמלל שוב.")
        Workspace(workspace_path).cleanup()
        state.workspace = ""
        state.save()
    report.bullet("תיקיית העבודה הזמנית נמחקה. הקליפים והתמלול נשארו.")
    print(report.markdown())
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Measure a problem the user sees before changing anything: is it in the source or the render?

    Writes a side-by-side video (source left, clip right, same moment, clip's sound) and a strip
    of frames around the moment, and reports the render checks, the source quality, and how much
    the picture moves in the clip against the source at that moment."""
    import subprocess

    import numpy as np

    project = _project(args)
    state = ProjectState.load(project)
    info = state.rendered.get(str(args.clip))
    if not info:
        raise UserFacingError("no_rendered")
    if not Path(state.media_path).exists():
        raise UserFacingError("media_missing")
    clip, src = Path(info["path"]), Path(state.media_path)
    if not clip.exists():
        raise UserFacingError("unknown", detail=f"קובץ הקליף לא נמצא (`{clip}`). אולי הוא מרונדר ממש עכשיו.")
    segments = [tuple(s) for s in info["segments"]]
    begin = max(0.0, args.at - args.span / 2)
    source_at = captions.clip_to_source(begin, segments)
    out_dir = project / "diagnose"
    out_dir.mkdir(exist_ok=True)
    stem = f"clip{args.clip:02d}_{args.at:.1f}s"
    report = Report(f"אבחון קליף {args.clip} סביב {args.at:.1f} שניות")

    warning = acquire.quality_warning(acquire.probe(src))
    report.bullet("איכות המקור: " + (warning or "טובה לחיתוך אנכי."))
    findings = _render_check(clip, state.has_audio)
    report.bullet("בדיקת הרינדור: " + (" ".join(findings) if findings else "התמונה והקול באותו אורך, ואין פריימים שחוזרים."))

    def motion(path: Path, start: float, crop: str) -> list[float]:
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", "1.0",
                              "-i", str(path), "-vf", f"{crop}scale=32:56,format=gray", "-f", "rawvideo", "-"],
                             capture_output=True, timeout=120).stdout
        frames = np.frombuffer(raw, np.uint8)
        frames = frames[: len(frames) // 1792 * 1792].reshape(-1, 1792).astype(np.float32)
        return [float(v) for v in np.abs(np.diff(frames, axis=0)).mean(axis=1)]

    at_source = captions.clip_to_source(args.at, segments)
    clip_motion = motion(clip, max(0.0, args.at - 0.5), "") if at_source is not None else []
    source_motion = motion(src, max(0.0, at_source - 0.5), "") if at_source is not None else []
    if clip_motion and source_motion:
        report.bullet(
            f"תנועה בתמונה סביב הרגע (ממוצע שינוי בין פריימים): בקליף {np.mean(clip_motion):.1f}, "
            f"במקור {np.mean(source_motion):.1f}. ערכים דומים אומרים שהתמונה זזה כמו במקור."
        )

    strip = out_dir / f"{stem}_frames.jpg"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, args.at - 0.3):.3f}",
                    "-i", str(clip), "-t", "0.6", "-vf", "scale=160:-2,tile=15x1", "-frames:v", "1", str(strip)],
                   timeout=120)
    if strip.exists():
        report.bullet(f"פריימים סביב הרגע (כל פריים בנפרד): `{strip}`")
    if source_at is not None:
        side = out_dir / f"{stem}_source_vs_clip.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{source_at:.3f}", "-t", f"{args.span:.3f}",
             "-i", str(src), "-ss", f"{begin:.3f}", "-t", f"{args.span:.3f}", "-i", str(clip), "-filter_complex",
             "[0:v]setpts=PTS-STARTPTS,scale=-2:960[a];[1:v]setpts=PTS-STARTPTS,scale=540:960[b];"
             "[a][b]hstack=inputs=2[v]", "-map", "[v]", "-map", "1:a?", "-c:v", "libx264", "-crf", "18",
             "-pix_fmt", "yuv420p", "-c:a", "aac", str(side)], timeout=600)
        if side.exists():
            joins = [sum(b - a for a, b in segments[: i + 1]) for i in range(len(segments) - 1)]
            crossing = [j for j in joins if begin < j < begin + args.span]
            note = " (יש חיבור בטווח הזה, ואחריו המקור משמאל כבר לא מתאים לקליף)" if crossing else ""
            report.bullet(f"השוואה צד לצד, משמאל המקור ומימין הקליף: `{side}`{note}")
    print(report.markdown())
    return 0


# ------------------------------------------------------------------ parser

def _hide_share(value: str) -> float:
    share = float(value)
    if not 0.0 <= share <= 0.4:
        raise argparse.ArgumentTypeError("--hide-bottom צריך להיות בין 0 ל-0.4 (חלק מגובה התמונה)")
    return share


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
    p.add_argument("--prepare-key", action="store_true", help="מכין את קובץ המפתח (GROQ_API_KEY=) ופותח אותו ב-Notepad")
    p.add_argument("--skip-selftest", action="store_true")

    p = add("ingest", cmd_ingest)
    p.add_argument("source", help="קישור או נתיב לקובץ וידאו")

    p = add("transcribe", cmd_transcribe)
    p.add_argument("--engine")
    p.add_argument("--force", action="store_true", help="לתמלל מחדש גם אם יש תמלול שמור")

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
    p.add_argument("--glossary", help='קובץ JSON עם מילון {"שגוי": "נכון"} שמוחל על כל התמלול, גם לפני בחירת קטעים')
    p.add_argument("--edits", help='קובץ JSON עם תיקוני מילים: [{"index": 12, "text": "מילה"}]')
    p.add_argument("--recaptioned", action="store_true",
                   help='להציג ולתקן את כתוביות הקליפים אחרי recaption (clip_words). תיקונים: [{"clip": 3, "index": 12, "text": "מילה"}]')

    p = add("render", cmd_render)
    p.add_argument("--clips", help="מספרי קליפים מופרדים בפסיק (ברירת מחדל: כולם)")
    p.add_argument("--hook", choices=[m.value for m in HookMode])
    p.add_argument("--hooks", help='פתיח לכל קליפ, למשל "1=teaser,2=title"')
    p.add_argument("--preset", choices=list(captions.PRESETS))
    p.add_argument("--font", choices=list(captions.FONTS))
    p.add_argument("--reframe", choices=["center", "blur"], default="center")
    p.add_argument("--no-auto-crop", action="store_true", help="בלי זיהוי פנים: חיתוך קבוע לפי plan.json או מרכזי")
    p.add_argument("--plan", help="קובץ JSON עם תוכנית עריכה לכל קליפ: חלקים, מיקום חיתוך ופתיח")

    p.add_argument("--keep-cuts", action="store_true", help="לא להאריך חלקים עד סוף המשפט")
    p.add_argument("--hide-bottom", type=_hide_share, default=0.0,
                   help="חלק מגובה התמונה שיוסתר למטה (למשל 0.17), כשבמקור כבר יש כתוביות צרובות")
    p.add_argument("--slow-zoom", action="store_true", help="זום איטי על צילומים כמעט קפואים לריצה הזו (ברירת המחדל בפרופיל: slow_zoom)")
    p.add_argument("--recaptioned",action="store_true", help="כתוביות מהתמלול החדש של כל קליפ (אחרי recaption ובדיקה)")

    p = add("recaption", cmd_recaption)
    p.add_argument("--clips")
    p.add_argument("--prompt", help="קובץ טקסט עם שמות והקשר שנשלחים כרמז לתמלול")
    p.add_argument("--glossary", help='קובץ JSON {"שגוי": "נכון"} שמוחל על התמלול החדש')

    p = add("feedback", cmd_feedback)
    p.add_argument("--clip", type=int, help="מספר הקליפ")
    p.add_argument("--outcome", choices=feedback.OUTCOMES, help="approved / changed / rejected")
    p.add_argument("--kind", choices=feedback.KINDS)
    p.add_argument("--category", help="קטגוריה, לפי הסוג")
    p.add_argument("--reason", help="במילים שלך: מה לא עבד")
    p.add_argument("--file", help="קובץ JSON עם רשימת פסקי דין (אותם שדות)")

    p = add("qa", cmd_qa)
    p.add_argument("--clips")

    p = add("diagnose", cmd_diagnose, help="בדיקה של תקלה שהמשתמש רואה: מקור מול קליף באותו רגע")
    p.add_argument("--clip", type=int, required=True)
    p.add_argument("--at", type=float, required=True, help="השנייה בקליף שבה רואים את הבעיה")
    p.add_argument("--span", type=float, default=6.0, help="כמה שניות להשוות סביב הרגע")

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
