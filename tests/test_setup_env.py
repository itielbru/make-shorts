import json

import config
import setup_env
from runtime import CompletedText

FILTERS_WITH_ASS = (
    " ... ass               V->V       Render ASS subtitles onto input video using the libass library.\n"
    " T. asetrate           A->A       Change the sample rate without altering the data.\n"
)
FILTERS_WITHOUT_ASS = " T. asetrate           A->A       Change the sample rate without altering the data.\n"


def fake_which(present):
    return lambda name: f"/bin/{name}" if name in present else None


def fake_run(output):
    def run(cmd, *, timeout, cwd=None):
        return CompletedText(output, "", 0)
    return run


def test_libass_detected_from_filters_output():
    st = setup_env.check_environment(
        which=fake_which({"ffmpeg", "ffprobe", "yt-dlp", "deno"}), run=fake_run(FILTERS_WITH_ASS)
    )
    assert st.libass is True
    assert st.missing == []


def test_missing_libass_is_reported_when_filter_absent():
    st = setup_env.check_environment(
        which=fake_which({"ffmpeg", "ffprobe", "yt-dlp", "deno"}), run=fake_run(FILTERS_WITHOUT_ASS)
    )
    assert st.libass is False
    assert "libass" in st.missing


def test_missing_ffmpeg_reports_winget_id_Gyan_FFmpeg():
    st = setup_env.check_environment(which=fake_which({"yt-dlp", "deno"}), run=fake_run(""))
    assert "ffmpeg" in st.missing
    hints = setup_env.install_hints_windows(st)
    assert any("Gyan.FFmpeg" in h for h in hints)


def test_hints_cover_ytdlp_and_deno_winget_ids():
    st = setup_env.check_environment(which=fake_which({"ffmpeg", "ffprobe"}), run=fake_run(FILTERS_WITH_ASS))
    hints = " ".join(setup_env.install_hints_windows(st))
    assert "yt-dlp.yt-dlp" in hints
    assert "DenoLand.Deno" in hints


def test_hardware_parsed_from_powershell_json():
    payload = {
        "cpu": "Intel(R) Core(TM) 7 150U",
        "cores": 10,
        "gpus": [{"Name": "Intel(R) Graphics", "AdapterRAM": 2147479552}],
        "ram": 16860000000,
    }
    hw = setup_env.detect_hardware(run=fake_run(json.dumps(payload)), platform="win32")
    assert hw.cpu_name == "Intel(R) Core(TM) 7 150U"
    assert hw.cores == 10
    assert round(hw.ram_gb, 1) == 15.7
    assert hw.has_discrete_gpu is False


def test_discrete_gpu_name_heuristic():
    assert setup_env.is_discrete_gpu("NVIDIA GeForce RTX 3060 Laptop GPU") is True
    assert setup_env.is_discrete_gpu("AMD Radeon RX 6600M") is True
    assert setup_env.is_discrete_gpu("Intel(R) Graphics") is False
    assert setup_env.is_discrete_gpu("AMD Radeon(TM) Graphics") is False
    assert setup_env.is_discrete_gpu("Intel(R) UHD Graphics 770") is False


def test_no_discrete_gpu_and_cloud_allowed_recommends_groq():
    hw = setup_env.Hardware("cpu", 10, 16.0, False)
    advice = setup_env.recommend_engine(hw, cloud_allowed=True)
    assert advice.engine == "groq"
    assert advice.warn_slow_local is False
    assert advice.reason_he


def test_cloud_denied_recommends_no_engine_because_local_does_not_exist_yet():
    hw = setup_env.Hardware("cpu", 10, 16.0, False)
    advice = setup_env.recommend_engine(hw, cloud_allowed=False)
    assert advice.engine == "none"
    assert advice.warn_slow_local is False
    assert "עוד לא זמין" in advice.reason_he


def test_a_discrete_gpu_still_gets_an_engine_that_exists_in_this_version():
    hw = setup_env.Hardware("cpu", 16, 32.0, True)
    advice = setup_env.recommend_engine(hw, cloud_allowed=True)
    assert advice.engine in setup_env.AVAILABLE_ENGINES
    assert advice.warn_slow_local is False


def test_first_run_true_until_setup_complete_setting(config_home):
    assert setup_env.is_first_run() is True
    config.write_settings({"SETUP_COMPLETE": "true"})
    assert setup_env.is_first_run() is False


def test_can_proceed_needs_core_tools_but_not_ytdlp():
    st = setup_env.check_environment(which=fake_which({"ffmpeg", "ffprobe"}), run=fake_run(FILTERS_WITH_ASS))
    hw = setup_env.Hardware("cpu", 10, 16.0, False)
    status = setup_env.status_json(st, hw, setup_env.recommend_engine(hw, False), first_run=True)
    assert status["can_proceed"] is True
    assert status["url_input_ready"] is False
    assert "yt-dlp" in status["missing"]
    assert status["first_run"] is True
    json.dumps(status, ensure_ascii=False)
