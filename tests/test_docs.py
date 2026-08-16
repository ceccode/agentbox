#!/usr/bin/env python3
"""Keeps the docs honest about the CLI.

SKILL.md is a contract with an agent: it names flags and sections and tells
the agent to use them without checking. A rename in agentbox.py would leave
that contract quietly false — the agent would run `agentbox --no-titles`,
get a non-zero exit, and have no idea why. These tests fail on the rename
instead, in both directions:

  * every flag and section the docs name must exist in the parser
  * every flag and section the parser accepts must appear in SKILL.md

README.md is checked one way only. It advertises a curated subset of the
section aliases on purpose, so requiring full coverage there would just
pressure it into being exhaustive rather than readable.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agentbox  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "agentbox", "SKILL.md")
README = os.path.join(ROOT, "README.md")

BACKTICKED = re.compile(r"`([^`\n]+)`")
FLAG = re.compile(r"^--[a-z][a-z-]*$")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parser_flags():
    """Long option strings the parser accepts, minus argparse's own --help."""
    return {opt
            for action in agentbox.build_parser()._actions
            for opt in action.option_strings
            if opt.startswith("--")} - {"--help"}


def documented_flags(text):
    """Flags named anywhere in the doc, as backticked tokens.

    Takes the first word of each token so `--days N` and `--watch N` match
    the flag rather than the flag-plus-metavar.
    """
    return {tok.split()[0]
            for tok in BACKTICKED.findall(text)
            if FLAG.match(tok.split()[0])}


def documented_sections(text):
    """Section names from the left column of SKILL.md's Sections table.

    Scoped to that table so prose mentions of ordinary backticked words
    (`--json`, `available`, `reason`) can't be mistaken for section names.
    """
    names = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1]
        names |= set(BACKTICKED.findall(first_cell))
    return names


class TestSkillMatchesCLI(unittest.TestCase):
    """SKILL.md and the parser must agree, both directions."""

    def setUp(self):
        self.text = read(SKILL)

    def test_documented_flags_exist(self):
        unknown = documented_flags(self.text) - parser_flags()
        self.assertFalse(
            unknown, f"SKILL.md documents flags the CLI rejects: {sorted(unknown)}")

    def test_every_flag_is_documented(self):
        missing = parser_flags() - documented_flags(self.text)
        self.assertFalse(
            missing, f"CLI flags missing from SKILL.md: {sorted(missing)}")

    def test_documented_sections_exist(self):
        unknown = documented_sections(self.text) - set(agentbox.SECTION_ALIASES)
        self.assertFalse(
            unknown, f"SKILL.md documents sections the CLI rejects: {sorted(unknown)}")

    def test_every_section_is_documented(self):
        missing = set(agentbox.SECTION_ALIASES) - documented_sections(self.text)
        self.assertFalse(
            missing, f"CLI sections missing from SKILL.md: {sorted(missing)}")

    def test_env_var_is_documented(self):
        self.assertIn("AGENTBOX_OPENCODE_DB", self.text)

    def test_frontmatter_name_matches_directory(self):
        """opencode and Claude Code both require this, and reject the skill
        outright when it drifts."""
        self.assertTrue(self.text.startswith("---\n"), "SKILL.md needs frontmatter")
        front = self.text.split("---", 2)[1]
        expected = os.path.basename(os.path.dirname(SKILL))
        self.assertRegex(front, rf"(?m)^name:\s*{re.escape(expected)}\s*$")
        self.assertRegex(front, r"(?m)^description:\s*\S")

    def test_documented_flags_actually_parse(self):
        """Cheapest possible end-to-end: argparse accepts each documented flag."""
        parser = agentbox.build_parser()
        for flag in sorted(documented_flags(self.text)):
            with self.subTest(flag=flag):
                argv = [flag] if flag in ("--json", "--no-titles") else [flag, "5"]
                parser.parse_args(argv)


class TestReadmeMatchesCLI(unittest.TestCase):
    """One direction only — the README shows a readable subset by design."""

    def setUp(self):
        self.text = read(README)

    def test_readme_flags_exist(self):
        unknown = documented_flags(self.text) - parser_flags()
        self.assertFalse(
            unknown, f"README documents flags the CLI rejects: {sorted(unknown)}")

    def test_readme_usage_sections_exist(self):
        """The usage block spells sections as `agentbox cpu | mem | gpu`."""
        block = re.search(r"agentbox (cpu(?: \| \w+)+)", self.text)
        self.assertIsNotNone(block, "README usage block for sections not found")
        named = {w.strip() for w in block.group(1).split("|")}
        unknown = named - set(agentbox.SECTION_ALIASES)
        self.assertFalse(
            unknown, f"README names sections the CLI rejects: {sorted(unknown)}")


class TestInstallerPointsAtTheSkill(unittest.TestCase):
    """The installer symlinks a path; a moved skill should fail here, not on
    a user's machine after the clone."""

    def test_skill_source_exists(self):
        self.assertTrue(os.path.isfile(SKILL), f"missing {SKILL}")

    def test_installer_references_that_path(self):
        script = read(os.path.join(ROOT, "scripts", "install-agent-skill.sh"))
        self.assertIn("skills/agentbox", script)
        self.assertIn("SKILL.md", script)


if __name__ == "__main__":
    unittest.main()
