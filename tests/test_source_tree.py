from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_source_tree_has_explicit_subpackages_and_no_stale_artifacts() -> None:
    for package in ("core", "services", "ui"):
        assert (ROOT / "cameracopy2" / package / "__init__.py").is_file()

    assert not (ROOT / "IMPLEMENTATION_PLAN.md").exists()
    assert not (ROOT / "cameracopy.json").exists()
    assert not (ROOT / "cameracopy2/resources/images/cameracopy.png").exists()
