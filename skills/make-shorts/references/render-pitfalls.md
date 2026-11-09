# Render faults found in real use, and what guards against each

Read this when a user reports that a clip "stalls", "is out of sync", "jumps" or "smears", or
before changing `render.py` or `facecrop.py`. Every row was a real report. The right column is
what now catches it: a test that fails if the fault comes back, a check that runs after every
render (`_render_check`, `qa.motion_findings`), or both.

| What the user saw | Real cause | Guard |
|---|---|---|
| Multi-part clips stall and drift out of sync at the start, only in some players | After `concat` + `loudnorm`, the first 2.4 s of sound were **timestamped** into 0.6 s and then a gap. The samples were perfect, so every check that decoded the sound passed. Fixed with `asetpts=N/SR/TB` at the end of the sound chain | `test_a_rendered_clip_has_evenly_spaced_timestamps_a_player_can_follow`; render check reads every packet's timestamp (`qa.sound_timestamp_findings`, `qa.picture_timestamp_findings`, `qa.start_findings`) |
| Sound at 96 kHz; stalls on phones and weak computers | `loudnorm` works at 192 kHz inside and the rate was never brought back | `aresample=48000`; test asserts 48 kHz |
| Picture and sound drift apart after a join | One `concat` for picture+sound with an L-cut: each part starts after the longer of its two streams | Separate picture and sound concats; real-render test measures both lengths |
| "Stuck picture, sound goes on" in a wide shot | The crop chose the **listener**: in a wide shot faces are ~40 px and "mouth motion" is noise | Wide two-person shots become a top/bottom split screen (`facecrop.wide_pair`, `render.split_chain`) |
| People "jump" at a join | A join inside the same shot; later, the punch-in zoom did not reach the split screen | Punch-in (`punch_in_zooms`), applied to split halves too; motion check flags jump joins |
| One-frame flash at a camera cut | ffmpeg duplicated the first frame of an input with audio; cut times were a frame off | `-fps_mode passthrough`, `setpts=PTS-STARTPTS`; real-render test that the crop switches on the exact frame |
| Flash of another shot at a clip's start or end | A camera cut just inside the edge | `facecrop.settle_edges` (moves the edge to the cut, only through silence); motion check flags short shots |
| Smearing and frozen faces | The **source** was 720p at ~1 Mbps, stretched 2.7x by the vertical crop | `acquire.quality_warning` at ingest; downloads take 1080p |
| Two sets of captions | The source had captions burned in | `render --hide-bottom`; SKILL.md tells to look for them at ingest |
| Clip ends before the point | The pick ended at a sentence, not at the end of the idea | SKILL.md: read 60 words past the end before fixing it |

## Lessons for diagnosing

1. **Measure the file the way a player reads it.** Decoding to samples or frames hides timestamp
   faults. `diagnose` and the render check read packet timestamps directly.
2. **Ask which clips share the fault.** Here only the multi-part clips stalled, which pointed
   straight at the joining code. One clip with a problem may be the source; a pattern is the code.
3. **Compare with the source before blaming the render**, and **before blaming the source,
   check the timing.** Twice a "render fault" was the source, and once a fault that looked like
   the source was in the timestamps.
4. **Every fault found gets a test that reproduces it first**, then the fix, then a check after
   every render if it can recur on new material.
