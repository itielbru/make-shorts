import subprocess

import audio
from transcript import Word

MAX_UPLOAD_BYTES = 24_000_000


def test_plan_chunks_equal_time_and_last_absorbs_rounding():
    chunks = audio.plan_chunks(60_000_000, 600.0, MAX_UPLOAD_BYTES - 64_000)
    assert len(chunks) == 3
    assert chunks[0][0] == 0.0
    assert chunks[-1][1] == 600.0
    lengths = [round(b - a, 3) for a, b in chunks]
    assert lengths == [200.0, 200.0, 200.0]
    assert all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))


def test_plan_chunks_last_chunk_absorbs_rounding_for_uneven_duration():
    chunks = audio.plan_chunks(50_000_000, 100.0, 24_000_000)
    assert len(chunks) == 3
    assert chunks[-1][1] == 100.0
    assert all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))


def test_plan_chunks_single_chunk_when_under_24MB():
    assert audio.plan_chunks(20_000_000, 300.0, MAX_UPLOAD_BYTES - 64_000) == [(0.0, 300.0)]


def test_extract_audio_command_has_ar_16000_ac_1_b_a_64k(tmp_path):
    cmd = audio.extract_audio_cmd(tmp_path / "in.mp4", tmp_path / "out.mp3")
    joined = " ".join(cmd)
    assert "-ar 16000" in joined
    assert "-ac 1" in joined
    assert "-b:a 64k" in joined
    assert "libmp3lame" in cmd
    assert "-vn" in cmd


def test_extract_audio_produces_mp3_from_synthetic_video(synthetic_video, tmp_path):
    out = audio.extract_audio(synthetic_video, tmp_path / "audio.mp3")
    assert out.exists()
    size = out.stat().st_size
    assert 50_000 < size < 150_000  # about 64 kbps for 10 s


def test_split_audio_writes_one_file_per_chunk(synthetic_video, tmp_path):
    full = audio.extract_audio(synthetic_video, tmp_path / "audio.mp3")
    chunks = [(0.0, 5.0), (5.0, 10.0)]
    parts = audio.split_audio(full, chunks, tmp_path / "chunks")
    assert [p.name for p in parts] == ["chunk_000.mp3", "chunk_001.mp3"]
    assert all(p.stat().st_size > 10_000 for p in parts)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(parts[0])],
        capture_output=True, text=True, check=True,
    )
    assert 4.5 < float(probe.stdout.strip()) < 5.6


def test_probe_duration_matches_synthetic_audio(synthetic_video, tmp_path):
    full = audio.extract_audio(synthetic_video, tmp_path / "audio.mp3")
    assert 9.5 < audio.probe_duration(full) < 10.6


def test_shift_words_rounds_to_6_decimals():
    shifted = audio.shift_words([Word("שלום", 0.1234567, 0.7654321)], 10.0)
    assert shifted == [Word("שלום", 10.123457, 10.765432)]
