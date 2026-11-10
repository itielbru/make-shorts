"""Framing round 2: people centred and followed inside a shot (the user: "שיראו את האנשים טוב ושיהיו באמצע")."""
import facecrop
import render
from facecrop import FaceCluster


def test_lead_room_is_only_a_hint_and_never_pushes_the_face_out():
    assert facecrop.lead_for(1.0) <= 0.05
    # a huge close-up (almost a third of the source width) gets no lead at all: it would touch the edge
    centred = facecrop.crop_fraction(0.5, 16 / 9)
    assert facecrop.crop_fraction(0.5, 16 / 9, lead=0.3, face_width=0.3) == centred


def test_a_still_face_needs_no_glide():
    samples = [(i / 10, 0.45) for i in range(100)]
    crop = facecrop.crop_fraction(0.45, 16 / 9)
    assert facecrop.track_glides(samples, crop, 16 / 9) == []


def test_a_small_sway_stays_inside_the_deadband():
    samples = [(i / 10, 0.45 + (0.02 if i % 20 < 10 else -0.02)) for i in range(100)]
    crop = facecrop.crop_fraction(0.45, 16 / 9)
    assert facecrop.track_glides(samples, crop, 16 / 9) == []


def test_a_face_that_moves_across_the_frame_is_followed_by_a_glide():
    samples = [(i / 10, 0.35 if i < 50 else 0.54) for i in range(120)]
    crop = facecrop.crop_fraction(0.35, 16 / 9)
    glides = facecrop.track_glides(samples, crop, 16 / 9)
    assert len(glides) == 1
    t, x, g = glides[0]
    assert 4.5 <= t <= 6.0 and g == facecrop.GLIDE_S
    assert abs(x - facecrop.crop_fraction(0.54, 16 / 9)) < 1e-6


def test_follow_face_ignores_a_small_inset_face_and_jumps():
    subject = FaceCluster(0.4, 0.13, 3, 0.0)
    frames = [[(0.40, 0.13, 0), (0.09, 0.06, 0)], [(0.42, 0.13, 0), (0.09, 0.06, 0)], [(0.09, 0.06, 0)], [(0.45, 0.14, 0)]]
    assert [round(cx, 2) for _, cx in facecrop.follow_face(frames, subject)] == [0.40, 0.42, 0.45]


def test_crop_expression_glides_between_positions():
    expr = render.crop_x_expression([(0.0, 0.3), (5.0, 0.5, 0.8)])
    assert "min(1,(t-5)/0.8)" in expr and "if(lt(t,5),0.3," in expr


def test_group_centre_frames_a_wide_shot_on_the_people():
    people = [FaceCluster(0.70, 0.03, 3, 0.0), FaceCluster(0.80, 0.03, 3, 0.0)]
    assert abs(facecrop.group_centre(people, 3) - 0.75) < 1e-9
    studio = [FaceCluster(0.15, 0.03, 3, 0.0), FaceCluster(0.42, 0.025, 3, 0.0), FaceCluster(0.75, 0.03, 3, 0.0)]
    assert facecrop.group_centre(studio, 3) == 0.42  # the host between the panels
    assert facecrop.group_centre([], 3) is None


# ---- captions: the spoken word highlighted without colour tags inside the RTL text

def test_word_boxes_split_the_line_at_the_measured_gaps_and_reach_the_edges():
    import captions
    assert captions.word_boxes([500.0], 1080) == [(500.0, 1080.0), (0.0, 500.0)]


def test_highlight_events_are_clipped_copies_of_the_plain_line():
    import captions
    from transcript import Word
    block = captions.Block([Word("שלום,", 0.0, 0.5), Word("עולם.", 0.5, 1.0)], 0.0, 1.0)
    ass = captions.build_ass([block], preset="social_bold", word_edges=[[520.0]])
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert len(events) == 3  # the plain line, then one highlighted copy per word
    plain_text = events[0].split(",,", 1)[1].split("}")[-1]
    for e in events[1:]:
        assert r"\clip(" in e and r"\1c&H00E5FF&" in e
        assert e.endswith(plain_text)  # same text, so RTL order and punctuation are untouched
    assert r"\clip(520,0,1080,1920)" in events[1] and "0:00:00.50" in events[1]


def test_no_highlight_without_measurements_or_for_plain_presets():
    import captions
    from transcript import Word
    block = captions.Block([Word("שלום", 0.0, 0.5), Word("עולם", 0.5, 1.0)], 0.0, 1.0)
    assert r"\clip" not in captions.build_ass([block], preset="social_bold")
    assert r"\clip" not in captions.build_ass([block], preset="social", word_edges=[[520.0]])
