# make-shorts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `make-shorts` Claude skill: a long Hebrew video (YouTube link or local file) in, captioned 9:16 shorts out, with Claude choosing the strong segments and teaching a non-technical user as it goes.

**Architecture:** One skill (`SKILL.md` prose + deterministic Python scripts). Prose holds the wizard, questions, guardrails and Claude's judgement steps; scripts never ask questions, they return a Markdown report on stdout, progress on stderr, and exit codes. Transcription engines are interchangeable behind one word-timestamp format. Clip selection is Claude picking word-index spans, then code snaps them to real word, sentence and silence boundaries. Rendering is one ffmpeg re-encode with ASS captions; QA extracts frames that Claude must look at.

**Tech Stack:** Python 3.10+ (standard library only for the base runtime), pytest, ffmpeg/ffprobe (full build with libass), yt-dlp, Deno (YouTube), optional faster-whisper in a managed venv, Groq / ElevenLabs HTTP APIs via `urllib`.

**Spec:** `docs/superpowers/specs/2026-11-08-podcast-to-shorts-design.md`

## Global Constraints

- One skill named `make-shorts`; scripts are Python 3.10+, standard library only for the base runtime; never `shell=True`; subprocess output decoded as UTF-8 with `errors="replace"`.
- Windows 11 is the primary platform. ffmpeg must be a full build with libass (`winget install --id Gyan.FFmpeg --exact`); other winget IDs: `yt-dlp.yt-dlp`, `DenoLand.Deno`.
- Config dir is `Path.home() / ".config" / "make-shorts"` on every OS. Settings live in `.env` (UTF-8, atomic write via `mkstemp` + `os.replace`, created `0o600`, precedence env > file > default). Secrets are never printed, never put in command lines, never requested in chat.
- Exit codes: 0 success, 2 missing dependency or config error, 3 API key needed.
- Script output is a Markdown report on stdout, progress and errors on stderr as `[make-shorts] ...`. Each stage catches its own failure and adds it to "Unavailable evidence"; a partial report is still produced.
- Transcript word format is `{text, start, end}` in seconds. The language hint always comes from the profile; never auto-detect.
- Engines (first version implements Groq only; the local and ElevenLabs engines are deferred): Groq `whisper-large-v3` (default on weak hardware), local `ivrit-ai/whisper-large-v3-turbo-ct2` with `language="he"`, ElevenLabs `scribe_v2`. No silent switch between engines. No audio leaves the machine unless the profile records cloud consent.
- Cloud audio: mp3, 16000 Hz, mono, 64k. `MAX_UPLOAD_BYTES = 24_000_000`; chunks are equal-duration, planned with `MAX_UPLOAD_BYTES - 64_000`. Up to 4 attempts; honour `Retry-After` on 429 (max 2 retries); 5xx waits `2.0 * 2**attempt`; custom User-Agent (Groq's WAF rejects the default); a failed chunk becomes a recorded gap; request timeout 300 s.
- Selection: scoring windows about 90 s with about 30 s overlap, aligned to sentence ends; shortlist `max(3, min(10, duration // 90 + 2))`; soft duration 20-90 s capped by the profile maximum; snap to word boundaries within 1.5 s; lead-in 0.35 s and tail 0.45 s (starting values); sentence snap within 12 words; overlap of 25% or more of the shorter clip drops the lower-scored clip; clip count floor `max(2, min(6, n // 2 + 2))`, ceiling `min(12, max(4, 2 * n))` where n is the shortlist size; never pad a clip to reach a length; clip opens on its strongest moment, stands alone, no two clips make the same point.
- Hooks: modes `none`, `title`, `teaser`, `reorder`; `reorder` is high risk and never a default; a quoted hook sentence must appear verbatim in the transcript.
- Render: one re-encode, never stream copy; 1080x1920; `loudnorm`; reframe is `center` or `blur` (blurred background); no face tracking. Captions are ASS burned with `ass=` and `fontsdir` using a bundled OFL Hebrew font (Heebo); no uppercase transform; keep `׳` `״` `־`; the bottom 15% of the height stays free of captions; default 3 words per block; three presets with plain names.
- Work happens in a temp directory with ASCII-safe names and is deleted at the end. Final file names may be Hebrew but are sanitized for Windows-invalid characters (`<>:"/\|?*`, trailing dots/spaces, reserved names).
- QA: frames are extracted at known caption times; contact sheet of 6-8 frames per clip; Claude must view every listed frame before any visual claim. Candidate-preview frames use scene threshold 0.20 and drop frames whose 16x16 mean absolute difference is 2.0 or less.
- Teaching mode is on by default. User-facing strings are simple Hebrew: what happened, why, what to do now; no raw tracebacks.
- Questions use `AskUserQuestion`: at most 4 questions per call, 2-4 options each, header at most 12 characters, a recommended default marked, a plain-language description per option; fall back to plain-text questions if the tool is missing.
- Commit steps assume the user agreed to `git init` in this folder (it is not a repository today). Do not commit before that.

## Review Focus

- Hebrew and space characters in the project path (the folder is literally `פודקסט לשורט`): ffmpeg `ass=`/`fontsdir` filter paths must not break. Pinned in Task 12.
- A caption line mixing Hebrew, English words and digits plus `׳ ״ ־`: order, punctuation and case must survive. Pinned in Task 11.
- The LLM returns word indices that are reversed, out of range, duplicated or overlapping: drop with a plain-Hebrew reason, never crash. Pinned in Task 9.
- Inputs with nothing to clip: audio-only file, video shorter than the minimum clip length, no speech, Whisper hallucinating in a silent tail: say so plainly. Pinned in Tasks 4, 8 and 9.
- A Hebrew title containing characters Windows rejects for file names (`?` `:` `"`), or very long: output file must still be written. Pinned in Task 8.

---

## File Structure

```
.claude-plugin/plugin.json, marketplace.json      packaging
pyproject.toml, .gitignore
skills/make-shorts/
  SKILL.md                      Hebrew prose: wizard, flow, teaching, guardrails
  references/                   glossary.md, selection-criteria.md, hebrew-rtl.md, windows-setup.md, wizard.json
  profiles/                     therapist.json, podcaster.json, educator.json, business.json
  fonts/                        Heebo-Bold.ttf, OFL.txt
  scripts/
    runtime.py  messages.py  config.py  profiles.py  setup_env.py
    workspace.py  acquire.py  transcript.py  audio.py
    engines/__init__.py base.py groq.py ivrit_local.py elevenlabs.py
    selection.py  hooks.py  captions.py  render.py  qa.py
    make_shorts.py              CLI entry (subcommands, report, exit codes)
tests/                          one test file per module + test_e2e.py
```

---

### Task 1: Scaffold, runtime, messages, config

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `skills/make-shorts/scripts/{runtime.py,messages.py,config.py}`, `tests/{conftest.py,test_runtime.py,test_config.py}`

**Interfaces:**
- Produces: `class UserFacingError(Exception)` with `__init__(self, key: str, **fmt)`, `.message_he: str`, `.exit_code: int`; `run_text(cmd: list[str], *, timeout: float | None, cwd: Path | None = None) -> CompletedText` (fields `stdout, stderr, returncode`); `CONFIG_DIR: Path`; `get_setting(name: str, default: str | None = None) -> str | None`; `load_secret(name: str) -> str | None`; `write_settings(updates: dict[str, str]) -> None`; `MESSAGES: dict[str, str]` (Hebrew templates, each states what/why/what now).
- Fixtures in `conftest.py`: `config_home` (redirects `HOME` and `USERPROFILE` to a tmp dir), `synthetic_video` (10 s 1920x1080 ffmpeg `lavfi` testsrc plus sine, skipped when ffmpeg is missing), `hebrew_tmp` (tmp dir whose name contains `פודקסט לשורט test`).

- [ ] **Step 1: Write failing tests**
  `test_run_text_missing_executable` (raises `UserFacingError` with key `cannot_run`), `test_run_text_timeout` (key `timed_out`), `test_run_text_never_uses_shell`, `test_env_roundtrip_preserves_comments_and_unrelated_lines`, `test_precedence_env_over_file_over_default`, `test_write_is_atomic_and_0600_on_posix`, `test_rejects_key_with_invalid_name`, `test_reads_utf16_bom_file`, `test_secret_never_in_error_message`.
- [ ] **Step 2: Run** `python -m pytest tests -q` and confirm failures.
- [ ] **Step 3: Implement** the three modules per the Interfaces block. `configure_stdio()` reconfigures stdout/stderr to UTF-8 with `backslashreplace`.
- [ ] **Step 4: Run tests**; expected all pass.
- [ ] **Step 5: Commit** `feat: scaffold runtime, messages and config`

### Task 2: Creator profiles

**Files:**
- Create: `scripts/profiles.py`, `profiles/{therapist,podcaster,educator,business}.json`, `tests/test_profiles.py`

**Interfaces:**
- Consumes: `UserFacingError`, `CONFIG_DIR`.
- Produces: `@dataclass Profile` with `name, language, domain, strong_clip_types: list[str], min_s: float, max_s: float, caption_preset: str, font: str, hook_default: str, red_flags: list[str], engine: str, cloud_allowed: bool, teaching_mode: bool`; `load_profile(path: Path) -> Profile`; `save_profile(p: Profile, path: Path) -> None`; `list_templates() -> list[str]`; `load_template(name: str) -> Profile`. Defaults: `min_s=20`, `max_s=90`, `hook_default="none"`, `cloud_allowed=False`, `teaching_mode=True`.

- [ ] **Step 1: Write failing tests**: `test_therapist_template_has_hebrew_red_flags_for_client_mention_and_therapeutic_promises`, `test_roundtrip`, `test_invalid_hook_default_rejected` (valid: `none,title,teaser,reorder`), `test_cloud_allowed_defaults_false`, `test_max_below_min_rejected`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement** with JSON on disk, validation in `load_profile`. Templates carry the spec's section 5 fields; the therapist red flags are Hebrew strings.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: creator profiles and templates`

### Task 3: Environment check and engine recommendation

**Files:**
- Create: `scripts/setup_env.py`, `tests/test_setup_env.py`

**Interfaces:**
- Produces: `check_environment() -> EnvStatus` (`ffmpeg, ffprobe, ytdlp, deno: bool`, `libass: bool`, `missing: list[str]`); `detect_hardware() -> Hardware` (`cpu_name, cores, ram_gb, has_discrete_gpu`); `recommend_engine(hw: Hardware, cloud_allowed: bool) -> EngineAdvice` (`engine: str, reason_he: str, warn_slow_local: bool`); `install_hints_windows(status: EnvStatus) -> list[str]`; `status_json(...) -> dict` with `first_run`, `can_proceed`, `missing`, `hardware`, `advice`.

- [ ] **Step 1: Write failing tests** (monkeypatch `shutil.which` and the ffmpeg `-filters` output): `test_libass_detected_from_filters_output`, `test_missing_ffmpeg_reports_winget_id_Gyan_FFmpeg`, `test_no_discrete_gpu_and_cloud_allowed_recommends_groq`, `test_no_discrete_gpu_and_cloud_denied_recommends_local_with_slow_warning`, `test_first_run_true_until_setup_complete_setting`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** Hardware via PowerShell `Get-CimInstance` on Windows (Win32_Processor, Win32_VideoController, Win32_ComputerSystem), minimal equivalents elsewhere. `can_proceed` is false only when ffmpeg, ffprobe, libass or yt-dlp (for URL input) are missing. No automatic installs; return hints only.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: environment preflight and engine advice`

### Task 4: Transcript model and audio chunking

**Files:**
- Create: `scripts/transcript.py`, `scripts/audio.py`, `tests/test_transcript.py`, `tests/test_audio.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) Word(text: str, start: float, end: float)`; `@dataclass Transcript(words: list[Word], language: str, engine: str, gaps: list[tuple[float, float]], approximate_timing: bool = False)`; `save_transcript(t, path)`, `load_transcript(path) -> Transcript`; `cache_key(media_sha256: str, engine: str, language: str) -> str`; `drop_words_in_trailing_silence(words: list[Word], silences: list[tuple[float, float]], audio_end: float) -> list[Word]`; `interpolate_words(segment_text: str, start: float, end: float) -> list[Word]`; `extract_audio(video: Path, out: Path) -> Path`; `plan_chunks(total_bytes: int, duration_s: float, max_bytes: int) -> list[tuple[float, float]]`; `split_audio(audio: Path, chunks: list[tuple[float, float]], out_dir: Path) -> list[Path]`; `shift_words(words: list[Word], offset: float) -> list[Word]`.

- [ ] **Step 1: Write failing tests**: `test_plan_chunks_equal_time_and_last_absorbs_rounding`, `test_plan_chunks_single_chunk_when_under_24MB`, `test_extract_audio_command_has_ar_16000_ac_1_b_a_64k` (assert the argv), `test_drop_words_in_trailing_silence_removes_thank_you_after_last_speech`, `test_drop_keeps_words_in_silence_that_is_not_trailing`, `test_interpolate_words_evenly_spans_segment`, `test_cache_key_changes_with_engine_and_language`, `test_shift_words_rounds_to_6_decimals`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** `plan_chunks` uses `n = ceil(total_bytes / max_bytes)` with equal-time splits; `split_audio` uses `-ss`, `-t`, `-c copy` (chunks of a constant-bitrate mp3 only).
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: transcript model and audio chunking`

### Task 5: Groq engine

**Files:**
- Create: `scripts/engines/{__init__.py,base.py,groq.py}`, `tests/test_engine_groq.py`

**Interfaces:**
- Consumes: `Transcript`, `Word`, `extract_audio`, `plan_chunks`, `split_audio`, `shift_words`, `interpolate_words`, `load_secret`.
- Produces: `class TranscriptionEngine(Protocol): name: str; def transcribe(self, audio: Path, *, language: str, progress: Callable[[str], None]) -> Transcript`; `get_engine(name: str) -> TranscriptionEngine` (only `"groq"` is implemented; `"ivrit_local"` and `"elevenlabs"` raise `UserFacingError("engine_not_available")` with a plain Hebrew message that the engine is planned for later); `GroqEngine`. Missing key raises `UserFacingError("api_key_needed", provider="Groq")` with `exit_code == 3`.

- [ ] **Step 1: Write failing tests** against a fake HTTP server in a thread: `test_request_fields_model_language_verbose_json_and_word_granularity` (`model=whisper-large-v3`, `language=he`, `response_format=verbose_json`, `timestamp_granularities[]=word`), `test_custom_user_agent_sent`, `test_429_honours_retry_after_max_two_retries`, `test_5xx_backoff_is_2_times_2_pow_attempt` (patch sleep), `test_other_4xx_not_retried`, `test_failed_chunk_recorded_as_gap_and_others_kept`, `test_all_chunks_fail_raises`, `test_chunk_offsets_applied`, `test_segment_only_response_falls_back_to_interpolated_words_and_sets_approximate_timing`, `test_deferred_engines_raise_engine_not_available`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement** `groq.py` with hand-built multipart and `urllib.request`; endpoint `https://api.groq.com/openai/v1/audio/transcriptions`. Keys come only from `load_secret("GROQ_API_KEY")`.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Live probe (manual, user's own key, never pasted in chat):** transcribe a 30 s Hebrew sample and record in `references/windows-setup.md` whether Groq returns word timestamps and honours `language=he`. If words are missing, the interpolation fallback is the supported degraded mode and the report warns "approximate word timing".
- [ ] **Step 6: Commit** `feat: Groq transcription engine`

### Task 6: Local ivrit.ai engine — DEFERRED (the user chose a slim first version; do not execute)

**Files:**
- Create: `scripts/engines/ivrit_local.py`, `tests/test_engine_ivrit_local.py`

**Interfaces:**
- Consumes: `TranscriptionEngine`, `Transcript`, `run_text`.
- Produces: `install_ivrit_env(*, consent: bool, progress: Callable[[str], None]) -> Path` (managed venv at `Path.home()/".cache"/"make-shorts"/"ivrit-venv"`, `python -m venv` plus `pip install faster-whisper`, sentinel file `.deps-ok` written after a 2 s silent warm-up); `IvritLocalEngine`; `benchmark_realtime_factor(audio: Path, seconds: float = 120.0) -> float`. Without `consent=True`, raises `UserFacingError("install_needs_consent", size_gb=..., minutes=...)`.

- [ ] **Step 1: Write failing tests**: `test_install_refused_without_consent`, `test_venv_python_path_on_windows_uses_Scripts`, `test_transcribe_command_uses_model_ivrit_ai_whisper_large_v3_turbo_ct2_language_he_word_timestamps_int8_cpu`, `test_parses_runner_json_into_words`, `test_broken_venv_without_sentinel_is_rebuilt`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** The runner is a small script executed inside the venv that prints JSON words; the engine parses it. Never falls back to cloud on failure.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Manual measurement (needs user consent, about 1-1.5 GB download):** run `benchmark_realtime_factor` on the user's machine and write the measured number into `references/windows-setup.md`, replacing the spec's 5-25 min/hour estimate.
- [ ] **Step 6: Commit** `feat: local ivrit.ai engine`

### Task 7: ElevenLabs engine — DEFERRED (the user chose a slim first version; do not execute)

**Files:**
- Create: `scripts/engines/elevenlabs.py`, `tests/test_engine_elevenlabs.py`

**Interfaces:**
- Produces: `ElevenLabsEngine`; reads `ELEVENLABS_API_KEY` via `load_secret`, raises the same `api_key_needed` error (exit 3). Refuses to run unless the profile allows cloud.

- [ ] **Step 1: Write failing tests** against a fake server: `test_request_has_model_scribe_v2_language_he_and_word_timestamps`, `test_only_type_word_items_become_words` (ignores `spacing` and `audio_event`), `test_key_sent_in_header_not_url_or_body`, `test_retries_follow_same_policy_as_groq`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** Confirm endpoint path and field names against ElevenLabs' official API reference before coding; record the confirmed values in a code comment with the date.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: ElevenLabs transcription engine`

### Task 8: Workspace and acquisition

**Files:**
- Create: `scripts/workspace.py`, `scripts/acquire.py`, `tests/test_workspace.py`, `tests/test_acquire.py`

**Interfaces:**
- Produces: `Workspace.create(parent: Path | None = None) -> Workspace` (ASCII-safe dir `ms-<8 hex>`, `.path`, `.cleanup()`); `safe_slug(title: str) -> str` (ASCII only); `final_filename(title: str, index: int, ext: str = ".mp4") -> str`; `@dataclass MediaInfo(path: Path, title: str, duration: float, width: int, height: int, has_video: bool, has_audio: bool)`; `probe(path: Path) -> MediaInfo`; `fetch(source: str, ws: Workspace) -> MediaInfo`; `ytdlp_args(url: str, out_dir: Path) -> list[str]`.

- [ ] **Step 1: Write failing tests**: `test_final_filename_keeps_hebrew_and_strips_windows_invalid_chars` (title `מה זה חרדה? "הסבר": קצר` yields no `?:"`), `test_final_filename_avoids_reserved_names_and_trailing_dots`, `test_final_filename_length_capped_at_100`, `test_workspace_path_is_ascii_even_when_parent_is_hebrew`, `test_cleanup_removes_directory`, `test_ytdlp_args_contain_no_playlist_N8_format_and_merge_mp4` (exact format string `bv*[height<=720]+ba/b[height<=720]/bv+ba/b`), `test_ytdlp_percent_in_dir_is_escaped`, `test_403_error_maps_to_update_ytdlp_message`, `test_js_runtime_error_maps_to_deno_message`, `test_probe_audio_only_raises_audio_only_unsupported`, `test_probe_reads_dimensions_from_synthetic_video`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** Timeouts: ffprobe 30 s, yt-dlp 1800 s. A local path is used in place; a URL is downloaded into the workspace. Updating yt-dlp is only offered, never run without consent.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: workspace sanitizing and media acquisition`

### Task 9: Clip selection engine

**Files:**
- Create: `scripts/selection.py`, `tests/test_selection.py`

**Interfaces:**
- Consumes: `Transcript`, `Word`, `Profile`.
- Produces: `@dataclass Window(id: int, start_word: int, end_word: int, start: float, end: float)`; `build_windows(t: Transcript, window_s: float = 90.0, overlap_s: float = 30.0) -> list[Window]`; `render_indexed_transcript(t: Transcript, start_word: int, end_word: int) -> str` (`[index]word` tags with a time every 4 s); `shortlist_target(duration_s: float) -> int`; `clip_count_targets(n: int) -> tuple[int, int]`; `@dataclass Pick(start_word: int, end_word: int, score: int, reason: str, title: str, hook_text: str | None)`; `@dataclass Clip(start: float, end: float, start_word: int, end_word: int, score: int, reason: str, title: str, hook_text: str | None, warnings: list[str])`; `refine(t: Transcript, picks: list[Pick], *, min_s: float, max_s: float, silences: list[tuple[float, float]]) -> tuple[list[Clip], list[str]]`; `dedupe_and_trim(clips: list[Clip], max_clips: int) -> list[Clip]`.

- [ ] **Step 1: Write failing tests**: `test_windows_never_split_a_sentence`, `test_shortlist_target_3600s_is_10_and_200s_is_4`, `test_clip_count_targets_n5_is_4_and_10`, `test_refine_snaps_start_to_sentence_start_within_12_words`, `test_refine_snaps_to_word_boundary_with_lead_0_35_and_tail_0_45_into_silence`, `test_refine_extends_short_clip_to_later_sentence_end_never_pads`, `test_refine_95s_clip_ending_on_sentence_is_kept_when_profile_max_allows`, `test_refine_drops_clip_that_cannot_reach_min_without_padding`, `test_dedupe_drops_lower_score_when_overlap_at_least_25_percent_of_shorter`, `test_trim_keeps_highest_scores_not_earliest`; **review-focus:** `test_reversed_out_of_range_and_duplicate_picks_are_dropped_with_hebrew_reason_not_crash`, `test_transcript_with_no_words_returns_no_clips_and_plain_message`, `test_video_shorter_than_min_clip_returns_no_clips_message`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** Sentence boundary = word ending in `.`, `?`, `!`, `…`, or a pause of 0.7 s or more. Silence alignment uses supplied silence intervals; with none, use the fallback lead 0.25 s and tail 0.35 s.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: clip selection windows, snapping and dedupe`

### Task 10: Hook planning

**Files:**
- Create: `scripts/hooks.py`, `tests/test_hooks.py`

**Interfaces:**
- Consumes: `Clip`, `Transcript`.
- Produces: `class HookMode(str, Enum)` (`NONE, TITLE, TEASER, REORDER`); `@dataclass HookPlan(segments: list[tuple[float, float]], title_text: str | None, teaser_text: str | None, high_risk: bool)`; `verify_quote(quote: str, t: Transcript, start_word: int, end_word: int) -> bool`; `plan_hook(clip: Clip, t: Transcript, mode: HookMode) -> HookPlan`.

- [ ] **Step 1: Write failing tests**: `test_none_returns_single_segment`, `test_title_keeps_content_and_sets_title_text`, `test_teaser_prepends_quoted_sentence_segment_and_keeps_original`, `test_reorder_is_flagged_high_risk`, `test_quote_not_verbatim_raises_hebrew_error`, `test_quote_outside_clip_range_rejected`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: hook planning with verbatim quote check`

### Task 11: Captions (ASS)

**Files:**
- Create: `scripts/captions.py`, `fonts/Heebo-Bold.ttf`, `fonts/OFL.txt`, `tests/test_captions.py`

**Interfaces:**
- Consumes: `Word`, `Workspace`.
- Produces: `@dataclass Block(words: list[Word], start: float, end: float)`; `group_words(words: list[Word], words_per_block: int = 3) -> list[Block]` (breaks at sentence ends); `PRESETS: dict[str, Preset]` with keys `bold`, `clean`, `boxed` and Hebrew labels; `FONTS: dict[str, FontChoice]` (key, OFL Hebrew family, file name, Hebrew mood label; candidates to confirm with the user: heebo clean, rubik friendly, assistant neutral, secular_one bold-playful, frank_ruhl_libre formal-serif) and `build_ass(..., font: str = "heebo")`; `build_ass(blocks: list[Block], *, preset: str, width: int = 1080, height: int = 1920, safe_bottom_frac: float = 0.15, title_text: str | None = None) -> str`; `selftest_sentence() -> str` (Hebrew with English word, digits, `צה״ל`, `מקף־עברי`); `render_selftest(ws: Workspace) -> Path` (5 s clip using the self-test sentence).

- [ ] **Step 1: Write failing tests**: `test_blocks_hold_at_most_3_words_and_break_at_sentence_end`, `test_ass_playres_is_1080x1920_and_font_is_Heebo`, `test_bottom_margin_is_at_least_15_percent_of_height` (288 px), `test_text_is_never_uppercased_and_latin_word_unchanged`, `test_geresh_gershayim_maqaf_preserved`, `test_ass_special_characters_braces_and_newlines_escaped`, `test_word_highlight_event_per_word_with_correct_times`, `test_ofl_license_file_present_next_to_font`; **review-focus:** `test_mixed_hebrew_english_digits_line_keeps_character_order_in_source_text`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** Before any download, show the user the candidate fonts by mood (`AskUserQuestion`) and download only the ones they choose, from each font's official OFL source, verifying each license text before committing it. Heebo is the default font and is needed for the self-test.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Visual acceptance (manual, blocking):** run `render_selftest`, extract frames, and view them. If bidi order or punctuation is wrong, wrap each block in `U+202B ... U+202C` and view again; keep whichever renders correctly and record the decision in `references/hebrew-rtl.md`. If neither renders correctly, stop: the spec's fallback is the HyperFrames extension (separate plan).
- [ ] **Step 6: Commit** `feat: ASS captions with Hebrew font and safe area`

### Task 12: Rendering

**Files:**
- Create: `scripts/render.py`, `tests/test_render.py`

**Interfaces:**
- Consumes: `Clip`, `HookPlan`, `build_ass`, `Workspace`.
- Produces: `build_ffmpeg_cmd(src: str, segments: list[tuple[float, float]], *, ass_name: str, reframe: Literal["center", "blur"], out_name: str, loudnorm_i: float = -16.0) -> list[str]`; `render_clip(src: Path, clip: Clip, plan: HookPlan, blocks: list[Block], ws: Workspace, *, preset: str, reframe: str, out_dir: Path, title: str, index: int) -> Path`.

- [ ] **Step 1: Write failing tests**: `test_cmd_reencodes_with_libx264_and_never_copies_streams`, `test_cmd_has_loudnorm_I_minus_16_and_ass_filter_with_fontsdir`, `test_blur_graph_overlays_foreground_on_scaled_cropped_background`, `test_center_graph_scales_height_1920_and_crops_to_1080`, `test_multiple_segments_use_trim_and_concat`, `test_render_produces_1080x1920_with_audio_and_expected_duration` (synthetic video), **review-focus:** `test_render_works_when_workspace_parent_has_hebrew_and_spaces` (runs ffmpeg with `cwd` set to the workspace and bare relative file names for the ASS file and fonts, using `hebrew_tmp`), `test_final_file_written_with_sanitized_hebrew_title`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** Run ffmpeg with `cwd` = workspace; never put drive-letter paths inside the filtergraph. Timeout scales with clip length.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: single-pass ffmpeg rendering with reframe`

### Task 13: Visual and automatic QA

**Files:**
- Create: `scripts/qa.py`, `tests/test_qa.py`

**Interfaces:**
- Consumes: `Clip`, `Block`, `Transcript`.
- Produces: `caption_cue_times(blocks: list[Block]) -> list[float]` (block midpoints); `extract_frames(video: Path, times: list[float], out_dir: Path) -> list[Frame]` (`Frame(path, t, reason)`, timestamps from the `showinfo` output, not burned in); `contact_sheet(frames: list[Frame], out: Path) -> Path` (6-8 tiles); `preview_frames(video: Path, out_dir: Path, *, scene_threshold: float = 0.20, dedup_threshold: float = 2.0, max_frames: int = 50) -> list[Frame]`; `auto_checks(video: Path, expected_duration: float, clip: Clip, t: Transcript) -> list[Finding]` (black frames, silences at edges, loudness, mid-word cut, duration mismatch); `qa_report_markdown(frames, sheet, findings) -> str` ending with the instruction line to view every listed frame before judging.

- [ ] **Step 1: Write failing tests** on synthetic videos built with `lavfi`: `test_black_segment_yields_blackdetect_finding`, `test_silent_edge_yields_finding`, `test_mid_word_cut_detected_when_clip_boundary_inside_a_word`, `test_duration_mismatch_over_0_3s_flagged`, `test_frames_labelled_with_showinfo_times`, `test_dedup_drops_frames_with_mean_diff_at_most_2_0`, `test_contact_sheet_has_between_6_and_8_tiles`, `test_report_lists_every_frame_path_and_view_all_instruction`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** Dedup compares 16x16 `rgb24` rawvideo frames against the last kept frame; fail open (skip dedup) on ffmpeg error.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: frame extraction, contact sheet and automatic QA`

### Task 14: CLI orchestration and project state

**Files:**
- Create: `scripts/make_shorts.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: every module above.
- Produces: subcommands `setup [--json]` (also runs `render_selftest` and reports the result), `ingest <url-or-path>`, `transcribe [--engine NAME]`, `windows`, `refine --picks picks.json`, `render --clips ids --hook MODE --preset NAME --reframe center|blur`, `qa`, `cleanup`; `ProjectState` persisted as `process_config.json` (profile, engine, language, media hash, transcript path, accepted clips, hook modes); `Report` with sections and an "Unavailable evidence" list.

- [ ] **Step 1: Write failing tests**: `test_setup_json_reports_first_run_and_missing_tools`, `test_missing_api_key_exits_3_with_hebrew_message_and_no_secret`, `test_missing_dependency_exits_2`, `test_transcript_cache_hit_skips_engine_call`, `test_failed_render_stage_still_produces_partial_report_with_unavailable_evidence`, `test_engine_failure_does_not_switch_engine_silently`, `test_cloud_engine_refused_when_profile_cloud_allowed_false`, `test_progress_goes_to_stderr_report_to_stdout`, `test_render_reuses_state_without_retranscribing`, `test_setup_runs_caption_selftest_and_stops_if_it_fails`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement.** Top level converts `UserFacingError` to a stderr Hebrew message plus its exit code; unexpected exceptions become a plain-language message with the error name only.
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: CLI entry point, report and project state`

### Task 15: SKILL.md, references, wizard, packaging

**Files:**
- Create: `skills/make-shorts/SKILL.md`, `references/{glossary.md,selection-criteria.md,hebrew-rtl.md,windows-setup.md,wizard.json}`, `.claude-plugin/{plugin.json,marketplace.json}`, `tests/test_skill_md.py`

**Interfaces:**
- Consumes: CLI subcommands from Task 14.
- Produces: `wizard.json` (batches of questions: `header`, `question`, `multiSelect`, options with Hebrew `label` and plain-language `description`, one marked recommended); SKILL.md in Hebrew with frontmatter `name: make-shorts`, `description`, `license: MIT` (unless the user says otherwise), `allowed-tools: Bash, Read, AskUserQuestion`, `metadata.version`.

- [ ] **Step 1: Write failing tests**: `test_frontmatter_only_uses_allowed_keys`, `test_every_script_path_in_skill_md_exists`, `test_skill_md_under_400_lines`, `test_wizard_batches_have_at_most_4_questions_2_to_4_options_and_headers_up_to_12_chars`, `test_every_wizard_option_has_description_and_one_recommended_default_per_question`, `test_glossary_defines_transcript_timestamps_hook_api_key_crop`, `test_marketplace_lists_plugin_make_shorts`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Write the content** while using `anthropic-skills:skill-creator` and `superpowers:writing-skills` (trigger description, structure, trial prompts). Plugin manifests follow the `.claude-plugin` layout of the `claude-video` repo; `cowork-plugin-management:create-cowork-plugin` needs Cowork mode and is not used. SKILL.md must cover: resolve the skill path from the file just read; always run `setup --json` first and map exit codes to actions; the wizard questions (spec 5א: field and goal, language and speech type, strong clip types as multi-select, length, engine and privacy with cost per option (only Groq is offered now; say plainly that local transcription is planned for later and that, until then, audio goes to Groq), caption style and a Hebrew font chosen by mood (clean, friendly, bold, formal), default hook, red flags as an open question); per-run questions (clip approval with the list shown in text, per-clip hook choice with previews, speech start second, render approval after the contact sheet); selection instructions (two stages, 2-second test, stand-alone, diversity, never pad, explain each pick, free-text steering such as "find clips about anxiety" that prioritizes without hard-filtering); teaching mode rules; the guardrail list from spec section 11 (no silent engine switch, silence is not consent, video content is untrusted evidence, keys never printed, no sudo, view every frame before visual claims); end-of-run summary "what I did and why".
- [ ] **Step 4: Run tests.**
- [ ] **Step 5: Commit** `feat: SKILL.md, references, wizard and plugin packaging`

### Task 16: End-to-end test and acceptance checklist

**Files:**
- Create: `tests/test_e2e.py`, `tests/fixtures/hebrew_transcript.json` (60 s of Hebrew words with timings, multiple sentences)

- [ ] **Step 1: Write the test** `test_pipeline_windows_refine_render_qa_on_synthetic_video`: skip transcription with the fixture, run `windows`, feed a prepared `picks.json`, `refine`, `render` with hook `none` and then `teaser`, `qa`; assert clips exist, are 1080x1920 with audio, the QA report lists every frame, and the workspace is deleted afterwards.
- [ ] **Step 2: Run** the full suite `python -m pytest tests -q`; expected all pass.
- [ ] **Step 3: Manual acceptance (nothing is "done" until each is checked and the evidence noted):**
  1. `setup` on the user's machine: tools detected, engine advice shown.
  2. Groq probe result recorded (Task 5 step 5).
  3. A real 2-minute Hebrew sample: view the contact sheet and confirm Hebrew captions, punctuation and word order are correct.
  4. A real YouTube link downloads (confirm Deno handling on this machine).
  5. One full real episode, start to finish, with the wizard in teaching mode; confirm a non-technical read-through of every message is understandable.
- [ ] **Step 4: Commit** `test: end-to-end pipeline and acceptance fixtures`

---

**Deferred by the user's choice of a slim first version (Tasks 6 and 7 stay in this file for later):** the local ivrit.ai engine, the ElevenLabs engine, and engine calibration. Until the local engine exists, the first version transcribes through Groq only.

**Not in this plan (from the spec, deferred):** the HyperFrames optional extension (section 12A; build only after Task 11's visual acceptance shows whether ASS is enough), OCR caption check, speaker-aware reframing, XML export, audio-only input, optional filler/silence removal and jump cuts inside a clip (spec 7, step 3; the renderer already accepts multiple segments), and CI on Windows/macOS/Linux (spec 10).
