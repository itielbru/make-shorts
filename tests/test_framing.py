"""Framing a wide podcast picture for a vertical clip: lead room toward where a face looks, and a
top/bottom split for wide shots of two people, where the picture cannot tell who is speaking."""
import shutil
import subprocess

import pytest

import captions
import facecrop
import render
from facecrop import FaceCluster
from transcript import Word

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
ASPECT = 16 / 9


def _face_in_crop(face_cx: float, frac: float) -> float:
    scaled_w = 1920 * ASPECT
    return (face_cx * scaled_w - frac * (scaled_w - 1080)) / 1080


# --- lead room ----------------------------------------------------------------------------------

def test_a_face_turned_to_the_right_sits_left_of_centre_with_room_ahead():
    # measured: the guest's close-up looks right toward the host, nose offset +0.4 eye-widths
    lead = facecrop.lead_for(0.4)
    frac = facecrop.crop_fraction(0.47, ASPECT, lead=lead)
    assert _face_in_crop(0.47, frac) == pytest.approx(0.5 - facecrop.LEAD_ROOM, abs=0.01)
    assert _face_in_crop(0.47, frac) < 0.4


def test_a_face_looking_into_the_camera_stays_centred():
    assert facecrop.lead_for(0.08) == 0.0  # measured frontal: +0.08
    assert _face_in_crop(0.47, facecrop.crop_fraction(0.47, ASPECT)) == pytest.approx(0.5, abs=0.01)


def test_lead_room_follows_the_direction_and_is_capped():
    assert facecrop.lead_for(-0.8) == pytest.approx(-facecrop.LEAD_ROOM)
    assert facecrop.lead_for(0.8) == pytest.approx(facecrop.LEAD_ROOM)


def test_clusters_average_the_turn_and_height_of_a_face():
    frames = [[(0.5, 0.1, 1.0, 0.4, 0.3)], [(0.5, 0.1, 1.0, 0.2, 0.4)]]
    (c,) = facecrop._clusters_for_shot(frames)
    assert c.yaw / c.frames == pytest.approx(0.3) and c.cy / c.frames == pytest.approx(0.35)


# --- wide two-person shots ----------------------------------------------------------------------

def _c(cx, width, frames=50, cy=0.35):
    return FaceCluster(cx, width, frames, 5.0 * frames, 0.0, cy * frames)


def test_a_wide_shot_of_two_people_apart_is_split():
    # measured: guest at 0.23, host at 0.83, faces 4.5% of the width
    pair = facecrop.wide_pair([_c(0.83, 0.047), _c(0.23, 0.045)], total_frames=56)
    assert pair is not None and pair[0].cx == pytest.approx(0.23) and pair[1].cx == pytest.approx(0.83)


def test_a_close_up_or_two_close_faces_is_not_split():
    assert facecrop.wide_pair([_c(0.47, 0.095)], total_frames=50) is None
    assert facecrop.wide_pair([_c(0.45, 0.045), _c(0.55, 0.045)], total_frames=50) is None
    assert facecrop.wide_pair([_c(0.25, 0.10), _c(0.8, 0.10)], total_frames=50) is None  # big faces: a crop fits one
    assert facecrop.wide_pair([_c(0.23, 0.045), _c(0.83, 0.047, frames=10)], total_frames=56) is None  # seen briefly


def test_split_screen_is_drawn_over_the_crop_only_inside_its_interval():
    cmd = render.build_ffmpeg_cmd("in.mp4", [(0.0, 6.0)], ass_name="c.ass", reframe="center", out_name="o.mp4",
                                  crop_x=[0.5], split=[[(1.0, 3.5, (0.23, 0.35), (0.83, 0.36))]])
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "vstack" in graph and "enable='between(t,1,3.5)'" in graph
    assert "scale=1080:960" in graph


def test_a_punch_in_also_tightens_the_split_screen_so_a_join_inside_it_is_not_a_jump():
    # seen in a real clip: two parts both in the wide shot met inside a split screen, and the
    # punch-in (applied to the hidden crop only) did not hide the people jumping in place
    cmd = render.build_ffmpeg_cmd("in.mp4", [(0.0, 3.0), (5.0, 8.0)], ass_name="c.ass", reframe="center",
                                  out_name="o.mp4", crop_x=[0.5, 0.5], zoom=[1.0, 1.08],
                                  split=[[(0.0, 3.0, (0.23, 0.35), (0.83, 0.36))], [(0.0, 3.0, (0.23, 0.35), (0.83, 0.36))]])
    graph = cmd[cmd.index("-filter_complex") + 1]
    first, second = graph.split("[s0]")[0], graph.split("[s1]")[0].split("[s0]")[-1]
    assert "h='ih*0.5'" in first and "h='ih*0.462963'" in second


@needs_ffmpeg
def test_a_split_render_shows_the_left_person_on_top_and_the_right_one_below(tmp_path):
    src = tmp_path / "src.mp4"
    # a wide picture: red on the left third, blue on the right third, grey between
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=25:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-vf", "drawbox=x=0:y=0:w=427:h=720:color=red:t=fill,drawbox=x=853:y=0:w=427:h=720:color=blue:t=fill",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    (tmp_path / "c.ass").write_text(captions.build_ass([], preset="social"), encoding="utf-8")
    (tmp_path / "fonts").mkdir()
    cmd = render.build_ffmpeg_cmd(str(src), [(0.0, 2.0)], ass_name="c.ass", reframe="center", out_name="o.mp4",
                                  crop_x=[0.5], split=[[(0.0, 2.0, (0.17, 0.4), (0.83, 0.4))]])
    subprocess.run(cmd, cwd=tmp_path, check=True)

    def colour(y):
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", str(tmp_path / "o.mp4"),
                              "-frames:v", "1", "-vf", f"crop=1080:200:0:{y},scale=1:1,format=rgb24", "-f", "rawvideo", "-"],
                             capture_output=True, check=True).stdout
        return raw[0], raw[2]

    top_r, top_b = colour(380)
    bottom_r, bottom_b = colour(1340)
    assert top_r > 150 > top_b and bottom_b > 150 > bottom_r


# --- captions during a split ---------------------------------------------------------------------

def test_captions_move_to_the_seam_between_the_two_halves_during_a_split():
    blocks = captions.make_blocks([Word("שלום", 0.5, 0.9), Word("לכולם.", 5.0, 5.4)], 2)
    ass = captions.build_ass(blocks, preset="social_bold", seam=[(0.0, 2.0)])
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert r"\an5\pos(540,960)" in lines[0] and r"\pos" not in lines[1]
