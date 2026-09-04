#!/usr/bin/env python3
"""Extract one version section from CHANGELOG.md for a GitHub release."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VERSION_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def extract_release_notes(changelog: str, tag: str) -> str:
    """Return the changelog body for an exact semantic-version tag."""
    match = VERSION_RE.fullmatch(tag)
    if match is None:
        msg = f"unsupported release tag: {tag}"
        raise ValueError(msg)

    version = ".".join(match.groups())
    heading = re.compile(rf"^## {re.escape(version)}(?:\s+-\s+.+)?$", re.MULTILINE)
    section = heading.search(changelog)
    if section is None:
        msg = f"CHANGELOG.md has no section for {version}"
        raise ValueError(msg)

    body_start = section.end()
    next_section = re.search(r"^## ", changelog[body_start:], re.MULTILINE)
    body_end = body_start + next_section.start() if next_section else len(changelog)
    body = changelog[body_start:body_end].strip()
    if not body:
        msg = f"CHANGELOG.md section for {version} is empty"
        raise ValueError(msg)
    return f"{body}\n"


def main() -> None:
    """Write release notes for the requested tag."""
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("output", type=Path)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args()

    notes = extract_release_notes(args.changelog.read_text(encoding="utf-8"), args.tag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes, encoding="utf-8")


if __name__ == "__main__":
    main()
