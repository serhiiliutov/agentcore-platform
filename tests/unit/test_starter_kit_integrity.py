"""Proves the ticketing fixture has not been edited.

The assignment is explicit: *"Don't change the ticketing API itself; if you have
to, say what you changed and why in your README."* The mock is what we are
measured against — surviving its 429s, its 500s and its lost responses is the
exercise. A quiet edit to it (a raised timeout, a removed failure mode, a fixed
bug) would make every failure-handling result meaningless while still passing.

So the fixture's checksums are recorded in ``docs/starter-kit/checksums.sha256``
and recomputed here on every run. The baseline was taken at Stage 0 from the files
exactly as the starter kit shipped them; nothing in the mock has been touched.

Every protected file is LF-only on disk (verified) and ``.gitattributes`` pins
``eol=lf`` for these file types, so hashing raw bytes is stable across a Windows
checkout and a Linux CI runner alike.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs" / "starter-kit" / "checksums.sha256"

#: The files that must never change. Anything else in the kit is ours to extend.
PROTECTED_FILES = frozenset(
    {
        "mock-ticketing/app.py",
        "mock-ticketing/seed.py",
        "mock-ticketing/Dockerfile",
        "mock-ticketing/requirements.txt",
        "mock-ticketing/openapi.json",
        "CONTRACT.md",
    }
)

_HEX_LENGTH = 64


def parse_manifest() -> list[tuple[str, str]]:
    """Return ``(relative_path, sha256)`` pairs from the manifest.

    Blank lines and ``#`` comments are skipped, matching the ``sha256sum`` format
    so the file can also be checked with standard tooling.
    """
    entries: list[tuple[str, str]] = []
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        digest, separator, relative_path = stripped.partition("  ")
        if not separator:
            raise AssertionError(f"malformed manifest line (expected two spaces): {line!r}")
        entries.append((relative_path.strip(), digest.strip()))
    return entries


MANIFEST_ENTRIES = parse_manifest()


def test_manifest_exists() -> None:
    assert MANIFEST_PATH.is_file(), f"missing manifest: {MANIFEST_PATH}"


def test_manifest_lists_exactly_the_protected_files() -> None:
    """Guards against a file being silently dropped from the manifest."""
    listed = {path for path, _ in MANIFEST_ENTRIES}
    assert listed == PROTECTED_FILES, (
        f"manifest does not match the protected set.\n"
        f"missing: {sorted(PROTECTED_FILES - listed)}\n"
        f"unexpected: {sorted(listed - PROTECTED_FILES)}"
    )


def test_manifest_hashes_are_well_formed() -> None:
    """Catches a hand-edit that records something that is not a SHA-256 digest."""
    for relative_path, digest in MANIFEST_ENTRIES:
        assert len(digest) == _HEX_LENGTH, f"{relative_path}: digest is not 64 characters"
        assert all(character in "0123456789abcdef" for character in digest), (
            f"{relative_path}: digest is not lowercase hexadecimal"
        )


@pytest.mark.parametrize(("relative_path", "expected_digest"), MANIFEST_ENTRIES)
def test_protected_file_is_unmodified(relative_path: str, expected_digest: str) -> None:
    """The fixture must be byte-for-byte what the starter kit shipped."""
    target = REPO_ROOT / relative_path
    assert target.is_file(), f"{relative_path} is missing from the repository"

    actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()

    assert actual_digest == expected_digest, (
        f"{relative_path} has been modified.\n"
        f"  recorded: {expected_digest}\n"
        f"  actual:   {actual_digest}\n"
        "The ticketing mock is the fixture for this assignment and must not be "
        "edited. If a change to it is genuinely unavoidable, say so in the README "
        "and update docs/starter-kit/checksums.sha256 deliberately, in its own commit."
    )
