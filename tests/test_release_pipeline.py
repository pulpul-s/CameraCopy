from __future__ import annotations

from pathlib import Path
import tomllib

from cameracopy2 import __version__
from scripts.verify_release import verify

ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_sources_agree() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == __version__
    assert pyproject["project"]["version"] == __version__
    assert verify(f"v{__version__}") == __version__


def test_project_has_no_wheel_or_runtime_desktop_entry_point() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "build-system" not in pyproject
    assert "scripts" not in pyproject["project"]
    assert not (ROOT / "cameracopy2/desktop_integration.py").exists()


def test_windows_release_uses_standalone_nuitka_and_nsis() -> None:
    build_script = (ROOT / "scripts/build-windows.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "packaging/windows/CameraCopy.nsi").read_text(encoding="utf-8")
    entry_point = (ROOT / "packaging/windows/cameracopy_entry.py").read_text(
        encoding="utf-8"
    )

    assert "--mode=standalone" in build_script
    assert "--mode=onefile" not in build_script
    assert "--enable-plugin=pyside6" in build_script
    assert "--windows-console-mode=attach" in build_script
    assert "CameraCopy-$Version-windows-x86_64-portable.zip" in build_script
    assert 'from cameracopy2.__main__ import main' in entry_point

    assert 'InstallDir "$PROGRAMFILES64\\CameraCopy"' in installer
    assert 'File /r "${APP_DIST}\\*.*"' in installer
    assert "WriteUninstaller" in installer
    assert installer.count("SetRegView 64") == 2
    assert installer.count("SetShellVarContext all") == 2


def test_windows_release_smoke_tests_build_and_upgrade() -> None:
    build_script = (ROOT / "scripts/build-windows.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "packaging/windows/CameraCopy.nsi").read_text(encoding="utf-8")

    assert 'ArgumentList @("--self-test")' in build_script
    assert "Invoke-CheckedConsoleProcess" in build_script
    assert "Silent installer upgrade test" in build_script
    assert "Silent uninstall smoke test" in build_script
    assert "stale-runtime-file.txt" in build_script
    assert 'Rename "$INSTDIR" "$R0"' in installer
    assert 'RMDir /r /REBOOTOK "$R0"' in installer
    assert installer.count('FindWindow $0 "" "${APP_NAME}"') == 2


def test_release_workflow_builds_tests_and_uploads_all_assets() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "types: [published]" in workflow
    assert "scripts/package-linux.sh release-assets" in workflow
    assert ".\\scripts\\build-windows.ps1 -OutputDirectory release-assets" in workflow
    assert "scripts/generate_checksums.py" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "actions/download-artifact@v7" in workflow
    assert "scripts/test-linux-packages.sh" in workflow
    assert "gh release upload" in workflow


def test_readme_documents_installation_and_builds() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "cameracopy_<version>_amd64.deb" in readme
    assert "CameraCopy-Setup-<version>.exe" in readme
    assert "CameraCopy-<version>-windows-x86_64-portable.zip" in readme
    assert "./scripts/package-linux.sh" in readme
    assert ".\\scripts\\build-windows.ps1" in readme
    assert "pip install ./cameracopy-" not in readme
    assert "cameracopy-install-desktop" not in readme


def test_ci_covers_supported_python_and_operating_systems() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert 'python-version: ["3.11", "3.13"]' in workflow
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    assert "python -m ruff check cameracopy2 tests scripts" in workflow
    assert "python -W error -m pytest" in workflow
