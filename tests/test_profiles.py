import dataclasses

import pytest

import profiles
from messages import UserFacingError


def test_list_templates_has_the_four_spec_templates():
    assert set(profiles.list_templates()) >= {"therapist", "podcaster", "educator", "business"}


def test_every_template_loads_and_validates():
    for name in profiles.list_templates():
        p = profiles.load_template(name)
        assert p.name == name
        profiles.validate(p)


def test_therapist_template_has_hebrew_red_flags_for_client_mention_and_therapeutic_promises():
    p = profiles.load_template("therapist")
    assert p.language == "he"
    assert any("מטופל" in flag for flag in p.red_flags)
    assert any("הבטח" in flag for flag in p.red_flags)


def test_roundtrip(tmp_path):
    original = profiles.load_template("therapist")
    path = tmp_path / "profile.json"
    profiles.save_profile(original, path)
    assert profiles.load_profile(path) == original


def test_invalid_hook_default_rejected():
    p = dataclasses.replace(profiles.load_template("podcaster"), hook_default="explode")
    with pytest.raises(UserFacingError) as exc:
        profiles.validate(p)
    assert exc.value.key == "bad_profile"


def test_valid_hook_defaults_are_accepted():
    base = profiles.load_template("podcaster")
    for mode in ("none", "title", "teaser", "reorder"):
        profiles.validate(dataclasses.replace(base, hook_default=mode))


def test_cloud_allowed_defaults_false():
    for name in profiles.list_templates():
        assert profiles.load_template(name).cloud_allowed is False


def test_defaults_match_spec():
    p = profiles.load_template("podcaster")
    assert (p.min_s, p.max_s) == (20, 90)
    assert p.hook_default == "none"
    assert p.teaching_mode is True


def test_style_notes_default_empty_and_roundtrips(tmp_path):
    p = profiles.load_template("podcaster")
    assert p.style_notes == ""
    custom = dataclasses.replace(p, style_notes="הקהל שלי: נשים בגיל 30-45. טון חם ולא קליני.")
    path = tmp_path / "p.json"
    profiles.save_profile(custom, path)
    assert profiles.load_profile(path).style_notes == custom.style_notes


def test_max_below_min_rejected():
    p = dataclasses.replace(profiles.load_template("podcaster"), min_s=60, max_s=30)
    with pytest.raises(UserFacingError) as exc:
        profiles.validate(p)
    assert exc.value.key == "bad_profile"


def test_unknown_field_in_file_rejected(tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{"name": "x", "language": "he", "surprise": 1}', encoding="utf-8")
    with pytest.raises(UserFacingError) as exc:
        profiles.load_profile(path)
    assert exc.value.key == "bad_profile"


def test_plain_caption_preset_is_valid():
    profiles.validate(dataclasses.replace(profiles.load_template("podcaster"), caption_preset="plain"))


def test_unknown_font_name_is_rejected_when_the_profile_is_checked():
    base = profiles.load_template("podcaster")
    with pytest.raises(UserFacingError) as exc:
        profiles.validate(dataclasses.replace(base, font="Heebo"))
    assert exc.value.key == "bad_profile"
    profiles.validate(dataclasses.replace(base, font="rubik"))


def test_language_must_be_a_short_language_code():
    base = profiles.load_template("podcaster")
    for bad in ("other", "עברית", "Hebrew", ""):
        with pytest.raises(UserFacingError):
            profiles.validate(dataclasses.replace(base, language=bad))
    for good in ("he", "en", "ar"):
        profiles.validate(dataclasses.replace(base, language=good))
