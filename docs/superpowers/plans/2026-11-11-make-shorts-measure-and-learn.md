# make-shorts: Measurement and Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the skill's quality measurable and make it learn from the user's verdicts, so it improves as an editor instead of only gaining features.

**Architecture:** A new pure module `feedback.py` holds the decision log (one JSON line per verdict, per project), the cross-project summary, and the lessons store (candidates that become rules after repeated evidence). Three thin CLI commands in `make_shorts.py` (`feedback`, `summary`, `lessons`) wrap it. SKILL.md tells the model when to ask for verdicts, when to record lessons, and to read the active rules at the start of every run. The blind critic and the DaVinci export are later, separate plans (roadmap at the end).

**Tech Stack:** Python 3.13 standard library only, pytest. Windows 11, weak CPU, no GPU.

**Spec:** this conversation's agreed direction, summarized in "Decisions" below (no separate spec file). Original product spec: `docs/superpowers/specs/2026-11-08-podcast-to-shorts-design.md`.

## Decisions (agreed with the user, 2026-11-11)

- Technical reliability comes first. Then measure. Then learn. No new editing features (B-roll, music, colour) in this plan.
- Every clip gets a verdict: `approved` (used as is), `changed` (used after a fix), or `rejected` (would not publish).
- Every `changed`/`rejected` verdict says its kind, `technical` or `creative`, and a category, plus the reason in the user's words. Technical and creative quality are counted separately, so better captions cannot hide worse clip choices.
- A lesson is learned only with a threshold: it becomes a rule after evidence from 2 different projects, or when the user confirms it explicitly. A one-off fix stays in the log only.
- Lessons are of two types, kept apart: `style` (a preference, "thick captions") and `principle` (an editing rule, "open with the question, not the answer").
- The user's labeling effort is small: one question at the end of each run ("which clips would you not publish as they are, and why?"); the model classifies the answer and records it.

## Global Constraints

- No new Python dependencies; standard library only (`json`, `dataclasses`, `datetime`, `pathlib`).
- Every user-facing string is plain Hebrew. Code, comments and identifiers are English.
- Commands print a Markdown report on stdout and exit 0 on success, 1 when nothing was produced, 2 for a config problem (existing convention in `make_shorts.py`).
- Files are UTF-8, written with `ensure_ascii=False`.
- Never read, print or show the `.env` file in the config directory.
- Python on this machine: `$LOCALAPPDATA/Programs/Python/Python313/python.exe`. Run tests from the repo root: `python -m pytest -q`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Commit on a branch, not on `main`.

## Review Focus

1. A corrupt or half-written line in `feedback.jsonl`: the rest of the log is still read, and the report says one line was skipped.
2. A project in the registry whose folder was moved or deleted: `summary` skips it and says so, instead of failing.
3. Two verdicts for the same clip in one project (the user changed their mind): the latest one counts.
4. A verdict for a clip number that was never rendered: refused with a clear Hebrew message.
5. A lesson recorded twice from the same project: still a candidate (evidence must come from 2 different projects).

---

## Task 0: Clean run on a podcast (no code)

Validates the latest framing and caption changes on the content type the skill is built for, and becomes the first entry in the log once Task 2 exists.

- [ ] **Step 1:** Ask the user for a 10-20 minute Hebrew podcast link (two people, a fixed camera per person is ideal).
- [ ] **Step 2:** Start a separate session that runs the skill as a new user from SKILL.md only (same brief as the 2026-11-10 clean run: may not edit `skills/` or `tests/`, writes `findings.md` in `Documents\make-shorts-clean-run-2`).
- [ ] **Step 3:** Read `findings.md`, verify each claim against the code or the clips, fix confirmed problems with a failing test first, commit.
- [ ] **Step 4:** Ask the user the verdict question for each clip and keep the answers; record them with `feedback` once Task 2 is merged.

Expected: no technical fault in the render check; framing keeps faces whole and near the middle; the yellow word matches the spoken word.

---

### Task 1: Decision log (pure module)

**Files:**
- Create: `skills/make-shorts/scripts/feedback.py`
- Test: `tests/test_feedback_log.py`

**Interfaces:**
- Produces:
  - `OUTCOMES = ("approved", "changed", "rejected")`, `KINDS = ("technical", "creative")`
  - `CATEGORIES = {"technical": ("sync", "frozen", "framing", "captions", "cut", "audio", "other"), "creative": ("start", "end", "moment", "length", "pacing", "hook", "other")}`
  - `@dataclass Verdict(clip: int, outcome: str, kind: str | None = None, category: str | None = None, reason: str = "", at: str = "")`
  - `validate_verdict(v: Verdict) -> None` raises `UserFacingError("bad_feedback", reason=...)`
  - `append_verdict(project: Path, v: Verdict) -> None`, which writes one line to `<project>/feedback.jsonl`, with `at` set to the ISO time when empty
  - `read_verdicts(project: Path) -> tuple[list[Verdict], int]`, which returns (latest verdict per clip, in clip order; number of unreadable lines skipped)

- [ ] **Step 1: Write the failing tests**

```python
def test_append_and_read_keep_the_latest_verdict_per_clip(tmp_path):
    feedback.append_verdict(tmp_path, Verdict(1, "approved"))
    feedback.append_verdict(tmp_path, Verdict(2, "changed", "creative", "end", "נגמר לפני השיא"))
    feedback.append_verdict(tmp_path, Verdict(1, "rejected", "technical", "sync", "תקוע"))
    verdicts, skipped = feedback.read_verdicts(tmp_path)
    assert [(v.clip, v.outcome) for v in verdicts] == [(1, "rejected"), (2, "changed")]
    assert verdicts[1].reason == "נגמר לפני השיא" and skipped == 0

def test_a_corrupt_line_is_skipped_and_counted(tmp_path):
    feedback.append_verdict(tmp_path, Verdict(1, "approved"))
    with (tmp_path / "feedback.jsonl").open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    verdicts, skipped = feedback.read_verdicts(tmp_path)
    assert len(verdicts) == 1 and skipped == 1

@pytest.mark.parametrize("v", [
    Verdict(1, "maybe"),                                  # unknown outcome
    Verdict(1, "changed"),                                # changed without kind/category
    Verdict(1, "rejected", "creative", "sync"),           # category of the other kind
    Verdict(1, "approved", "technical", "sync"),          # approved carries no kind
])
def test_invalid_verdicts_are_refused(v):
    with pytest.raises(UserFacingError):
        feedback.validate_verdict(v)
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest -q tests/test_feedback_log.py`
Expected: FAIL, `ModuleNotFoundError: No module named 'feedback'`

- [ ] **Step 3: Implement the module and add the message key**

`bad_feedback` goes into `messages.py` with Hebrew text: "המשוב לא תקין: {reason}". `append_verdict` calls `validate_verdict` first.

- [ ] **Step 4: Run them to see them pass**

Run: `python -m pytest -q tests/test_feedback_log.py`
Expected: all pass. Then run the full suite: all pass.

- [ ] **Step 5: Commit** with the message `feat: per-project decision log for clip verdicts`.

---

### Task 2: `feedback` command and project registry

**Files:**
- Modify: `skills/make-shorts/scripts/feedback.py` (registry)
- Modify: `skills/make-shorts/scripts/make_shorts.py` (`cmd_feedback`, parser)
- Test: `tests/test_feedback_log.py`

**Interfaces:**
- Consumes: Task 1's `Verdict`, `append_verdict`, `validate_verdict`.
- Produces:
  - `register_project(project: Path, registry: Path) -> None`, which adds the absolute path to the JSON list at `registry` once
  - `registered_projects(registry: Path) -> list[Path]`
  - `REGISTRY_NAME = "projects.json"`, stored in `config.config_dir()`
  - CLI: `feedback --project P --clip N --outcome O [--kind K --category C] [--reason "..."]`, or `feedback --project P --file verdicts.json` (a JSON list of verdict objects with the same field names)

- [ ] **Step 1: Write the failing tests**

```python
def test_feedback_refuses_a_clip_that_was_never_rendered(tmp_path, monkeypatch, capsys):
    (tmp_path / "process_config.json").write_text(json.dumps({"rendered": {"1": {}}}), encoding="utf-8")
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "cfg")
    code = make_shorts.main(["feedback", "--project", str(tmp_path), "--clip", "7", "--outcome", "approved"])
    assert code == 2 and "7" in capsys.readouterr().err

def test_feedback_records_and_registers_the_project(tmp_path, monkeypatch):
    (tmp_path / "process_config.json").write_text(json.dumps({"rendered": {"1": {}, "2": {}}}), encoding="utf-8")
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "cfg")
    assert make_shorts.main(["feedback", "--project", str(tmp_path), "--clip", "2", "--outcome", "changed",
                             "--kind", "creative", "--category", "end", "--reason", "נקטע"]) == 0
    assert feedback.read_verdicts(tmp_path)[0][0].category == "end"
    assert feedback.registered_projects(tmp_path / "cfg" / "projects.json") == [tmp_path.resolve()]
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest -q tests/test_feedback_log.py`
Expected: FAIL, because the parser does not know `feedback`.

- [ ] **Step 3: Implement `cmd_feedback` and the registry**

A clip that is not in `state.rendered` raises `UserFacingError("bad_feedback", reason="קליף {N} לא רונדר בפרויקט הזה")`. The report lists each verdict recorded, in Hebrew.

- [ ] **Step 4: Run them to see them pass.** Run the full suite: all pass.
- [ ] **Step 5: Commit** with the message `feat: feedback command records clip verdicts and registers the project`.

---

### Task 3: `summary` command (quality across projects)

**Files:**
- Modify: `skills/make-shorts/scripts/feedback.py`
- Modify: `skills/make-shorts/scripts/make_shorts.py`
- Test: `tests/test_feedback_summary.py`

**Interfaces:**
- Consumes: `read_verdicts`, `registered_projects`.
- Produces:
  - `@dataclass Summary(clips: int, approved: int, changed: int, rejected: int, technical: int, creative: int, by_category: dict[str, int], missing_projects: list[Path], skipped_lines: int)`
  - `summarize(projects: list[Path]) -> Summary`
  - `summary_markdown(s: Summary, per_project: list[tuple[str, Summary]]) -> str`
  - CLI: `summary [--project P]` (default: every registered project)

- [ ] **Step 1: Write the failing tests**

```python
def test_summary_counts_technical_and_creative_apart(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b): p.mkdir()
    feedback.append_verdict(a, Verdict(1, "approved"))
    feedback.append_verdict(a, Verdict(2, "changed", "technical", "framing", "פנים בקצה"))
    feedback.append_verdict(b, Verdict(1, "rejected", "creative", "end", "נגמר מוקדם"))
    s = feedback.summarize([a, b])
    assert (s.clips, s.approved, s.changed, s.rejected) == (3, 1, 1, 1)
    assert (s.technical, s.creative) == (1, 1)
    assert s.by_category == {"technical/framing": 1, "creative/end": 1}

def test_summary_skips_a_project_that_no_longer_exists(tmp_path):
    s = feedback.summarize([tmp_path / "gone"])
    assert s.clips == 0 and s.missing_projects == [tmp_path / "gone"]

def test_summary_markdown_shows_the_rate_without_fix_in_hebrew():
    s = feedback.Summary(4, 3, 1, 0, 1, 0, {"technical/sync": 1}, [], 0)
    text = feedback.summary_markdown(s, [])
    assert "75%" in text and "אושרו בלי תיקון" in text
```

- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** The report has three numbers at the top: "אושרו בלי תיקון" (as a percent of all clips), "תקלות טכניות" and "תיקונים יצירתיים". Then the categories, most frequent first. Then one row per project. It ends with a line naming any missing projects or skipped lines.
- [ ] **Step 4: Run them to see them pass.** Run the full suite: all pass.
- [ ] **Step 5: Commit** with the message `feat: summary of clip quality across projects, technical and creative apart`.

---

### Task 4: Lessons with a threshold

**Files:**
- Modify: `skills/make-shorts/scripts/feedback.py`
- Modify: `skills/make-shorts/scripts/make_shorts.py` (`cmd_lessons`)
- Test: `tests/test_feedback_lessons.py`

**Interfaces:**
- Produces:
  - `LESSON_TYPES = ("style", "principle")`, `RULE_MIN_PROJECTS = 2`, `LESSONS_NAME = "lessons.json"` (in `config.config_dir()`)
  - `@dataclass Lesson(id: str, text: str, type: str, category: str, projects: list[str], confirmed: bool = False)`
  - `Lesson.is_rule` (property): `confirmed or len(set(projects)) >= RULE_MIN_PROJECTS`
  - `record_lesson(store: Path, text: str, type: str, category: str, project: Path, lesson_id: str | None = None, confirm: bool = False) -> Lesson`. This adds evidence to `lesson_id` when it is given; otherwise it creates a new lesson with id `L<n>`.
  - `load_lessons(store: Path) -> list[Lesson]`
  - CLI: `lessons` (lists rules, then candidates, with ids), `lessons --add "text" --type T --category C --project P [--id L3] [--confirm]`, and `lessons --drop L3`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_lesson_from_one_project_stays_a_candidate(tmp_path):
    store, p = tmp_path / "lessons.json", tmp_path / "proj"
    l = feedback.record_lesson(store, "לפתוח בשאלה ולא בתשובה", "principle", "start", p)
    l = feedback.record_lesson(store, "", "principle", "start", p, lesson_id=l.id)
    assert not l.is_rule

def test_evidence_from_a_second_project_makes_it_a_rule(tmp_path):
    store = tmp_path / "lessons.json"
    l = feedback.record_lesson(store, "כתוביות עבות", "style", "captions", tmp_path / "a")
    l = feedback.record_lesson(store, "", "style", "captions", tmp_path / "b", lesson_id=l.id)
    assert l.is_rule and feedback.load_lessons(store)[0].is_rule

def test_the_user_can_confirm_a_rule_at_once(tmp_path):
    l = feedback.record_lesson(tmp_path / "s.json", "בלי פייד לשחור", "style", "other", tmp_path, confirm=True)
    assert l.is_rule
```

- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** The `lessons` report prints the rules first, as two lists: "העדפות סגנון" and "עקרונות עריכה". Then it prints the candidates, each with its project count. A missing store is not an error: the report says no lessons yet.
- [ ] **Step 4: Run them to see them pass.** Run the full suite: all pass.
- [ ] **Step 5: Commit** with the message `feat: lessons learned from verdicts become rules only after repeated evidence`.

---

### Task 5: SKILL.md wiring, end to end check

**Files:**
- Modify: `skills/make-shorts/SKILL.md`, in "הרצה רגילה" (new step 0 and a new last step) and in the command list
- Test: `tests/test_cli.py` (parser knows `feedback`, `summary`, `lessons`)

- [ ] **Step 1: Write the failing parser test**

```python
def test_parser_knows_the_measurement_commands():
    p = make_shorts.build_parser()
    assert p.parse_args(["summary"]).command == "summary"
    assert p.parse_args(["lessons", "--drop", "L1"]).drop == "L1"
    assert p.parse_args(["feedback", "--clip", "1", "--outcome", "approved"]).outcome == "approved"
```

- [ ] **Step 2: Run it.** It passes only if Tasks 2-4 registered the commands. Fix any gap.
- [ ] **Step 3: Write the SKILL.md text.** In Hebrew, add three things:
  - **A new step 0:** run `lessons` and apply every rule in this run. Rules override the defaults. Candidates are hints only.
  - **A new last step, after the user has watched the clips:** ask one question, "אילו קליפים לא היית מפרסם כמו שהם, ולמה?". Classify each answer into outcome, kind and category, and record it with `feedback`. The model classifies, the user doesn't.
  - **Recording lessons:** when a reason looks general rather than one-off, record it with `lessons --add`. Tell the user in one line that it was saved as a candidate and becomes a rule if it repeats. Never turn a single fix into a rule without the user's yes.
- [ ] **Step 4: End-to-end check on a real project.** Record Task 0's verdicts with `feedback`. Run `summary`, then `lessons`. Read both reports and check the numbers by hand against the answers given.
- [ ] **Step 5: Run the full suite and commit** with the message `docs: the skill asks for verdicts, records lessons and applies rules`.

---

## Later plans (not in this plan; each gets its own plan when this one is done)

1. **Blind critic.** A separate agent gets the finished clip's frames, its transcript, the brief (profile, audience, the user's goal) and the full source stretch around the clip. It does not get the editor's reasons. It reviews blind first, and only then compares its review with the editor's intent. It runs only after the code checks pass, so it never reports what a measurement already catches. Measure of success: how many of its findings the user's verdicts confirm (from the log above). Too many false alarms means it is not ready.
2. **Cheap sound signals** for clip choice: loudness peaks, laughter, a change in speaking rate. These are signals for the model, never a "virality" claim.
3. **Export to an editable project** (DaVinci Resolve, free), so an almost-good clip is fixed by hand instead of re-rendered. It comes last, because it makes fixes easier but does not improve the model's decisions.

## Success measures (example targets, not measured yet)

- Clips approved without a fix: 80%.
- Critical technical faults: 0.
- Fixes per clip: 1 or fewer on average.
- Technical and creative quality are always reported apart.
