from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "extract-release-notes.py"
SPEC = importlib.util.spec_from_file_location("extract_release_notes", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_extracts_only_requested_release() -> None:
    changelog = """# Changelog

## 0.8.0 - 2026-09-04

- New runtime.

## 0.7.0 - 2026-08-29

- Old runtime.
"""

    assert MODULE.extract_release_notes(changelog, "v0.8.0") == "- New runtime.\n"


@pytest.mark.parametrize("tag", ["0.8", "release-0.8.0", "v01.8.0"])
def test_rejects_invalid_tag(tag: str) -> None:
    with pytest.raises(ValueError, match="unsupported release tag"):
        MODULE.extract_release_notes("", tag)


def test_requires_matching_changelog_section() -> None:
    with pytest.raises(ValueError, match="has no section"):
        MODULE.extract_release_notes("# Changelog\n", "v0.8.0")
