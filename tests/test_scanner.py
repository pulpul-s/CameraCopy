from __future__ import annotations

from pathlib import Path

from cameracopy2.core.scanner import find_candidate_files


def test_natural_sort_handles_numeric_and_text_leading_components(tmp_path: Path) -> None:
    for directory, filename in [
        ("DCIM", "A.JPG"),
        ("DCIM", "1.JPG"),
        ("100CANON", "image10.JPG"),
        ("100CANON", "image2.JPG"),
    ]:
        path = tmp_path / directory
        path.mkdir(exist_ok=True)
        (path / filename).write_bytes(b"x")

    files = find_candidate_files(tmp_path, ["*.JPG"])

    assert [path.relative_to(tmp_path).as_posix() for path in files] == [
        "100CANON/image2.JPG",
        "100CANON/image10.JPG",
        "DCIM/1.JPG",
        "DCIM/A.JPG",
    ]
