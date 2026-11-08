import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """Redirect the user's home so config files never touch the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def hebrew_tmp(tmp_path) -> Path:
    """A temp directory whose name contains Hebrew letters and a space."""
    d = tmp_path / "פודקסט לשורט test"
    d.mkdir()
    return d


@pytest.fixture
def synthetic_video(tmp_path) -> Path:
    """10 s, 1920x1080 test pattern with a sine tone. Skipped without ffmpeg."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    out = tmp_path / "synthetic.mp4"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out
