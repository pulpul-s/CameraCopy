from __future__ import annotations

import configparser
import os
import stat
from pathlib import Path

from cameracopy2 import __main__ as command_line
from cameracopy2 import __version__
from cameracopy2 import self_test
from scripts.stage_linux_package import stage

ROOT = Path(__file__).resolve().parents[1]


def test_version_command_does_not_start_the_gui(capsys) -> None:  # type: ignore[no-untyped-def]
    assert command_line.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_self_test_reports_all_failures(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    def fail() -> str:
        raise RuntimeError("missing component")

    monkeypatch.setattr(self_test, "_checks", lambda: [("working", lambda: "ok"), ("broken", fail)])

    assert self_test.run_self_test() == 1
    output = capsys.readouterr().out
    assert "OK   working: ok" in output
    assert "FAIL broken: RuntimeError: missing component" in output
    assert "Self-test failed: 1 check(s) failed." in output


def test_windows_self_test_requires_com_and_treats_wmi_as_optional(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(self_test.sys, "platform", "win32")

    required = {label for label, _check in self_test._checks()}
    optional = {label for label, _check in self_test._optional_checks()}

    assert "pythoncom" in required
    assert optional == {"WMI volume metadata"}


def test_optional_self_test_failure_is_a_warning(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    def fail() -> str:
        raise RuntimeError("optional component unavailable")

    monkeypatch.setattr(self_test, "_checks", lambda: [("working", lambda: "ok")])
    monkeypatch.setattr(self_test, "_optional_checks", lambda: [("optional", fail)])

    assert self_test.run_self_test() == 0
    output = capsys.readouterr().out
    assert "WARN optional: RuntimeError: optional component unavailable" in output
    assert "Self-test passed with 1 optional warning(s)." in output


def test_linux_staging_excludes_build_artifacts_and_normalizes_modes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "module.py").chmod(0o777)
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-313.pyc").write_bytes(b"compiled")
    (source / "stale.pyo").write_bytes(b"compiled")
    subdirectory = source / "resources"
    subdirectory.mkdir()
    (subdirectory / "icon.png").write_bytes(b"png")

    destination = tmp_path / "destination"
    stage(source, destination)

    assert (destination / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (destination / "__pycache__").exists()
    assert not (destination / "stale.pyo").exists()
    if os.name != "nt":
        assert stat.S_IMODE((destination / "module.py").stat().st_mode) == 0o644
        assert stat.S_IMODE((destination / "resources").stat().st_mode) == 0o755


def test_linux_staging_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("outside\n", encoding="utf-8")
    (source / "linked.txt").symlink_to(target)

    try:
        stage(source, tmp_path / "destination")
    except ValueError as exc:
        assert "must not contain symlinks" in str(exc)
    else:
        raise AssertionError("staging accepted a symlink")


def test_linux_launcher_uses_isolated_system_python() -> None:
    launcher = (ROOT / "packaging/linux/cameracopy").read_text(encoding="utf-8")

    assert "/usr/bin/python3 -I -B" in launcher
    assert 'sys.path.insert(0, "/usr/lib/cameracopy")' in launcher
    assert "export PYTHONPATH" not in launcher


def test_linux_desktop_entry_has_production_metadata() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(ROOT / "packaging/linux/cameracopy.desktop", encoding="utf-8")
    entry = parser["Desktop Entry"]

    assert entry["Exec"] == "cameracopy"
    assert entry["TryExec"] == "cameracopy"
    assert entry["Icon"] == "cameracopy"
    assert entry["GenericName"] == "Camera media copier"
    assert entry["Keywords"] == "camera;photo;media;copy;backup;verify;"
    assert entry.getboolean("StartupNotify")


def test_linux_package_manifest_uses_staged_sources_and_current_dependencies() -> None:
    config = (ROOT / "packaging/linux/nfpm.yaml").read_text(encoding="utf-8")

    assert "src: .build/linux-package/root/cameracopy2/" in config
    assert "src: cameracopy2/" not in config
    assert "type: tree" not in config
    assert config.count("hicolor-icon-theme") == 3
    assert "xdg-utils" not in config
    assert "pyside6>=6.7" in config
    assert "python3-pyside6 >= 6.7" in config


def test_release_installs_packages_on_supported_linux_targets() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    smoke_test = (ROOT / "scripts/test-linux-packages.sh").read_text(encoding="utf-8")

    assert "linux-install-test:" in workflow
    assert "target: [debian, ubuntu, fedora, arch]" in workflow
    assert 'needs: [linux-install-test, windows]' in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "publish:\n    needs:" in workflow
    assert "contents: write" in workflow
    assert "debian:13" in smoke_test
    assert "ubuntu:26.04" in smoke_test
    assert "fedora:44" in smoke_test
    assert "archlinux:base" in smoke_test
    assert 'cameracopy --self-test' in smoke_test
    assert '-f="\\${Status}\\n"' in smoke_test
    assert smoke_test.count('pacman -U --noconfirm "$package"') == 2
    assert 'directory permissions differ' in smoke_test
