import json
import re
from pathlib import Path

import pytest

import captions
import make_shorts
from hooks import HookMode

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "make-shorts"
ALLOWED_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}


def skill_text():
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


def frontmatter():
    match = re.match(r"^---\n(.*?)\n---\n", skill_text(), re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    keys = {}
    for line in match.group(1).splitlines():
        if re.match(r"^[A-Za-z-]+:", line):
            key, _, value = line.partition(":")
            keys[key] = value.strip()
    return keys


def wizard():
    return json.loads((SKILL / "references" / "wizard.json").read_text(encoding="utf-8"))


def test_frontmatter_only_uses_allowed_keys_and_has_name_and_description():
    keys = frontmatter()
    assert set(keys) <= ALLOWED_KEYS
    assert keys["name"] == "make-shorts"
    assert len(keys["description"]) > 80
    tools = keys["allowed-tools"]
    # The skill tells Claude to write profile.json and picks.json, so Write and Edit must be allowed.
    assert all(tool in tools for tool in ("Bash", "Read", "Write", "Edit", "AskUserQuestion"))


def test_every_script_path_in_skill_md_exists():
    paths = set(re.findall(r"(?:scripts|references|profiles)/[A-Za-z0-9_./-]+\.(?:py|md|json)", skill_text()))
    assert paths, "SKILL.md should point at its scripts and references"
    for rel in paths:
        assert (SKILL / rel).exists(), rel


def test_skill_md_under_400_lines():
    assert len(skill_text().splitlines()) < 400


def test_skill_md_mentions_every_cli_subcommand():
    parser = make_shorts.build_parser()
    commands = next(a for a in parser._actions if a.dest == "command").choices
    text = skill_text()
    for name in commands:
        assert name in text, name


def test_wizard_batches_have_at_most_4_questions_2_to_4_options_and_headers_up_to_12_chars():
    for batch in wizard()["batches"]:
        assert 1 <= len(batch["questions"]) <= 4
        for q in batch["questions"]:
            assert len(q["header"]) <= 12, q["header"]
            assert 2 <= len(q["options"]) <= 4, q["header"]


def test_every_wizard_option_has_description_and_defaults_are_marked():
    for batch in wizard()["batches"]:
        for q in batch["questions"]:
            assert all(o["label"] and o["description"] for o in q["options"]), q["header"]
            recommended = [o for o in q["options"] if o.get("recommended")]
            if q["multiSelect"]:
                assert recommended, q["header"]
            else:
                assert len(recommended) == 1, q["header"]


def test_wizard_values_map_to_real_profile_choices():
    valid = {
        "caption_preset": set(captions.PRESETS),
        "font": set(captions.FONTS),
        "hook_default": {m.value for m in HookMode},
    }
    seen = set()
    for batch in wizard()["batches"]:
        for q in batch["questions"]:
            field = q["field"]
            seen.add(field)
            if field in valid:
                assert {o["value"] for o in q["options"]} <= valid[field], field
    assert {"caption_preset", "font", "hook_default", "cloud_allowed", "language", "domain"} <= seen


def test_glossary_defines_the_terms_a_beginner_meets():
    glossary = (SKILL / "references" / "glossary.md").read_text(encoding="utf-8")
    for term in ("תמלול", "חותמות זמן", "פתיח", "מפתח API", "9:16"):
        assert term in glossary, term


def test_marketplace_lists_plugin_make_shorts():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert [p["name"] for p in marketplace["plugins"]] == ["make-shorts"]
    assert marketplace["plugins"][0]["source"] in (".", "./")
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "make-shorts"
    assert (SKILL / "SKILL.md").exists()


def test_skill_md_explains_the_json_fields_and_commands_claude_depends_on():
    text = skill_text()
    for needed in ("url_input_ready", "profile_exists", "correct", "winget upgrade --id yt-dlp.yt-dlp", "--force"):
        assert needed in text, needed
