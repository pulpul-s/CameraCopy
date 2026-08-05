from __future__ import annotations

# ruff: noqa: E402

import os
from pathlib import Path
from threading import Event

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PySide6 = pytest.importorskip("PySide6")  # noqa: N816

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from cameracopy2 import __version__
from cameracopy2.config import load_config
from cameracopy2.models import CameraCopyConfig, CopyJob, VolumeInfo
from cameracopy2.ui.copy_progress_dialog import CopyProgressDialog
from cameracopy2.ui.copy_workers import CopyWorker
from cameracopy2.ui.main_window import MainWindow
from cameracopy2.ui.settings_dialog import SettingsDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_main_window_and_settings_dialog_smoke(tmp_path: Path) -> None:
    app = _app()
    config = CameraCopyConfig(source="", destination=str(tmp_path / "pictures"))

    window = MainWindow(config_path=tmp_path / "cameracopy.json", config=config)
    dialog = SettingsDialog(config, volumes=[])

    assert window.windowTitle() == "CameraCopy"
    assert dialog.windowTitle() == "CameraCopy Settings"
    dialog.close()
    window.close()
    app.processEvents()


def test_copy_worker_prompt_waits_observe_cancellation(tmp_path: Path) -> None:
    _app()
    cancel_event = Event()
    cancel_event.set()
    job = CopyJob(
        primary=VolumeInfo(id="card", display_name="Card", mount_path=tmp_path),
        secondary=None,
        config=CameraCopyConfig(source="", destination=str(tmp_path / "pictures")),
    )
    worker = CopyWorker(job, cancel_event)

    assert (
        worker.request_collision_decision(tmp_path / "source.JPG", tmp_path / "dest.JPG")
        == "cancel"
    )
    assert (
        worker.request_clone_mismatch_decision(
            tmp_path / "source.JPG", tmp_path / "dest.JPG", False
        ).decision
        == "cancel"
    )


def test_copy_worker_throttles_chunk_progress_but_keeps_boundaries(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _app()
    job = CopyJob(
        primary=VolumeInfo(id="card", display_name="Card", mount_path=tmp_path),
        secondary=None,
        config=CameraCopyConfig(source="", destination=str(tmp_path / "pictures")),
    )
    worker = CopyWorker(job, Event())
    clock = [1.0]
    source_updates: list[tuple[int, int, bool]] = []
    byte_updates: list[tuple[int, int, int]] = []
    monkeypatch.setattr("cameracopy2.ui.copy_workers.monotonic", lambda: clock[0])
    worker.source_progress.connect(
        lambda done, total, metered: source_updates.append((done, total, metered))
    )
    worker.byte_progress.connect(
        lambda done, total, index: byte_updates.append((done, total, index))
    )

    worker._emit_source_progress(0, 100, False)  # noqa: SLF001
    worker._emit_source_progress(10, 100, True)  # noqa: SLF001
    clock[0] += 0.1
    worker._emit_source_progress(20, 100, True)  # noqa: SLF001
    worker._emit_source_progress(100, 100, True)  # noqa: SLF001

    worker._emit_byte_progress(0, 100, 1)  # noqa: SLF001
    worker._emit_byte_progress(10, 100, 1)  # noqa: SLF001
    clock[0] += 0.1
    worker._emit_byte_progress(20, 100, 1)  # noqa: SLF001
    worker._emit_byte_progress(100, 100, 1)  # noqa: SLF001
    worker._emit_byte_progress(0, 100, 2)  # noqa: SLF001

    assert source_updates == [
        (0, 100, False),
        (20, 100, True),
        (100, 100, True),
    ]
    assert byte_updates == [
        (0, 100, 1),
        (20, 100, 1),
        (100, 100, 1),
        (0, 100, 2),
    ]


def test_format_confirmation_shows_device_model_before_mount(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _app()
    target = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=tmp_path / "card",
        device_path="/dev/sdh1",
        label="Transcend_32GB",
        model="Transcend 32GB",
        size_bytes=29_400_000_000,
    )
    job = CopyJob(
        primary=target,
        secondary=None,
        config=CameraCopyConfig(source="", destination=str(tmp_path / "pictures")),
    )
    dialog = CopyProgressDialog(job)
    captured: list[str] = []

    def capture_warning(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        captured.append(args[2])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "warning", capture_warning)

    assert dialog._confirm_format(target, "exFAT")  # noqa: SLF001
    text = captured[0]
    assert "Model: Transcend 32GB" in text
    assert text.index("Model: Transcend 32GB") < text.index(
        f"Mount: {target.mount_path}"
    )

    dialog.close()
    _app().processEvents()

def test_main_window_requests_async_refresh_after_format_signal(tmp_path: Path) -> None:
    _app()
    config = CameraCopyConfig(source="", destination=str(tmp_path / "pictures"))
    window = MainWindow(config_path=tmp_path / "cameracopy.json", config=config)

    requests: list[str] = []
    window._request_volume_scan = requests.append  # type: ignore[method-assign]  # noqa: SLF001
    window._handle_volumes_changed_after_format()  # noqa: SLF001

    assert requests == ["post_format"]
    window.close()
    _app().processEvents()


def test_copy_progress_dialog_preserves_large_source_byte_counts(tmp_path: Path) -> None:
    app = _app()
    total_bytes = 24_322_785_280
    job = CopyJob(
        primary=VolumeInfo(id="card", display_name="Card", mount_path=tmp_path),
        secondary=None,
        config=CameraCopyConfig(source="", destination=str(tmp_path / "pictures")),
    )
    dialog = CopyProgressDialog(job)
    worker = CopyWorker(job, Event())
    worker.source_started.connect(dialog.handle_source_started)
    worker.source_progress.connect(dialog.update_source_progress)

    worker.source_started.emit(1, 1, "Copying", total_bytes)
    worker.source_progress.emit(12_000_000_000, total_bytes, True)
    app.processEvents()

    label = dialog.source_status_label.text()
    assert "Source: Copying" in label
    assert "GB" in label
    assert "calculating speed" in label or "/s" in label
    assert "ETA" in label
    dialog.close()
    app.processEvents()


def test_copy_progress_dialog_preserves_large_file_byte_counts(tmp_path: Path) -> None:
    app = _app()
    file_size = 5 * 1024 * 1024 * 1024
    job = CopyJob(
        primary=VolumeInfo(id="card", display_name="Card", mount_path=tmp_path),
        secondary=None,
        config=CameraCopyConfig(source="", destination=str(tmp_path / "pictures")),
    )
    dialog = CopyProgressDialog(job)
    worker = CopyWorker(job, Event())
    worker.file_started.connect(dialog.handle_file_started)
    worker.byte_progress.connect(dialog.update_byte_progress)

    worker.file_started.emit(str(tmp_path / "video.MP4"), 1, 1, file_size)
    worker.byte_progress.emit(3 * 1024 * 1024 * 1024, file_size, 1)
    app.processEvents()

    assert "5.00 GB" in dialog.current_file_label.text()
    assert "3.00 GB / 5.00 GB" in dialog.status_label.text()
    assert dialog.byte_progress_bar.value() == 60
    dialog.close()
    app.processEvents()


def test_copy_worker_converts_unexpected_exception_to_failed_report(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _app()
    job = CopyJob(
        primary=VolumeInfo(id="card", display_name="Card", mount_path=tmp_path),
        secondary=None,
        config=CameraCopyConfig(source="", destination=str(tmp_path / "pictures")),
    )
    worker = CopyWorker(job, Event())
    reports = []
    worker.finished.connect(reports.append)
    monkeypatch.setattr(
        "cameracopy2.ui.copy_workers.CopyEngine.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    worker.run()

    assert len(reports) == 1
    assert reports[0].has_failures
    assert reports[0].failures[0].reason == "internal worker error"


def test_volume_scan_worker_reuses_service_and_reports_results(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from contextlib import nullcontext

    from cameracopy2.ui import volume_scanner as worker_module
    from cameracopy2.ui.volume_scanner import _VolumeScanWorker

    class FakeService:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def filtered_volumes(self, keywords: list[str]) -> list[VolumeInfo]:
            self.calls.append(keywords)
            return []

    service = FakeService()
    monkeypatch.setattr(worker_module, "create_volume_service", lambda: service)
    monkeypatch.setattr(worker_module, "windows_com_initialized", nullcontext)
    worker = _VolumeScanWorker()
    results: list[tuple[int, object]] = []
    worker.succeeded.connect(lambda request_id, volumes: results.append((request_id, volumes)))

    worker.scan(1, ["camera"])
    worker.scan(2, ["reader"])

    assert service.calls == [["camera"], ["reader"]]
    assert results == [(1, []), (2, [])]


def test_main_and_settings_help_text(tmp_path: Path) -> None:
    app = _app()
    config = CameraCopyConfig(
        source="",
        destination=str(tmp_path / "pictures"),
        copysidecars=True,
    )
    window = MainWindow(config_path=tmp_path / "cameracopy.json", config=config)
    dialog = SettingsDialog(config, volumes=[])

    assert window.settings_button.text() == "Settings"
    assert "primary source volume" in window.primary_combo.toolTip()
    assert "duplicate of the first" in window.clone_checkbox.toolTip()
    assert window.autoremove_checkbox.toolTip() == (
        "Remove each source file only after its destination copy has been written "
        "and successfully verified."
    )
    assert dialog.copy_sidecars_checkbox.isChecked()
    assert dialog.height() == 680
    assert ".rrdata" in dialog.rating_help_label.text()
    assert dialog.metadata_diagnostics_button.text() == "Run metadata diagnostics"
    diagnostics_tooltip = dialog.metadata_diagnostics_button.toolTip()
    assert "ExifTool" in diagnostics_tooltip
    assert "Files and settings are not changed" in diagnostics_tooltip
    assert "width:" not in diagnostics_tooltip
    assert dialog.version_label.text() == __version__
    assert not dialog.version_label.isEnabled()

    dialog.close()
    window.close()
    app.processEvents()


def test_main_window_remembers_clone_mode(tmp_path: Path) -> None:
    app = _app()
    config_path = tmp_path / "cameracopy.json"
    window = MainWindow(
        config_path=config_path,
        config=CameraCopyConfig(
            source="",
            destination=str(tmp_path / "pictures"),
            clonemode=True,
        ),
    )

    assert window.clone_checkbox.isChecked()
    window.clone_checkbox.setChecked(False)
    assert not load_config(config_path).clonemode
    window.clone_checkbox.setChecked(True)
    assert load_config(config_path).clonemode

    window.close()
    app.processEvents()


def test_main_window_resets_source_cleanup_to_saved_default_after_copy(
    tmp_path: Path,
) -> None:
    app = _app()
    window = MainWindow(
        config_path=tmp_path / "cameracopy.json",
        config=CameraCopyConfig(
            source="",
            destination=str(tmp_path / "pictures"),
            autoremove=False,
        ),
    )

    window.autoremove_checkbox.setChecked(True)
    window._copy_dialog_finished(0)  # noqa: SLF001

    assert not window.autoremove_checkbox.isChecked()

    window.close()
    app.processEvents()


def test_existing_file_policy_tooltip_updates(tmp_path: Path) -> None:
    app = _app()
    dialog = SettingsDialog(
        CameraCopyConfig(source="", destination=str(tmp_path / "pictures")),
        volumes=[],
    )

    dialog.collision_combo.setCurrentIndex(0)
    assert "do not copy" in dialog.collision_combo.toolTip()
    dialog.collision_combo.setCurrentIndex(2)
    assert "Replace existing destination files" in dialog.collision_combo.toolTip()

    dialog.close()
    app.processEvents()


def test_settings_help_rows_stay_compact_in_field_column(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services.metadata_service import MetadataCapability
    from cameracopy2.ui import settings_dialog as settings_module

    app = _app()
    monkeypatch.setattr(
        settings_module.ExifToolService,
        "capability",
        lambda self: MetadataCapability("exiftool", False, None),
    )
    dialog = SettingsDialog(
        CameraCopyConfig(
            source="",
            destination=str(tmp_path / "pictures"),
            minrating=1,
            useembeddedmetadata=True,
            checkhash=False,
            durablewrites=False,
        ),
        volumes=[],
    )
    dialog.show()

    from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy

    checked_labels = 0
    for tab_index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(tab_index)
        dialog.resize(860, 680)
        app.processEvents()

        labels = [
            label
            for label in dialog.tabs.currentWidget().findChildren(QLabel)
            if bool(label.property("settingsHelpText")) and label.isVisible()
        ]
        compact_heights = {label: label.height() for label in labels}

        for label in labels:
            checked_labels += 1
            assert label.wordWrap()
            assert label.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding
            assert label.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum

            grid = label.parentWidget().layout()
            assert isinstance(grid, QGridLayout)
            item_index = grid.indexOf(label)
            assert item_index >= 0
            _row, column, row_span, column_span = grid.getItemPosition(item_index)
            assert (column, row_span, column_span) == (1, 1, 1)
            assert grid.rowStretch(grid.rowCount() - 1) == 1

        dialog.resize(860, 980)
        app.processEvents()
        for label in labels:
            assert label.height() <= compact_heights[label] + 2

    assert checked_labels > 0
    dialog.close()
    app.processEvents()


def test_tooltip_palette_follows_selected_theme() -> None:
    app = _app()
    from PySide6.QtGui import QPalette

    from cameracopy2.ui.theme import apply_theme

    apply_theme(app, "dark")
    dark = app.palette()
    assert dark.color(QPalette.ColorRole.ToolTipBase).lightness() < 128
    assert dark.color(QPalette.ColorRole.ToolTipText).lightness() > 128

    apply_theme(app, "light")
    light = app.palette()
    assert light.color(QPalette.ColorRole.ToolTipBase).lightness() > 128
    assert light.color(QPalette.ColorRole.ToolTipText).lightness() < 128


def test_settings_inline_safety_warnings_follow_controls(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services.metadata_service import MetadataCapability
    from cameracopy2.ui import settings_dialog as settings_module

    app = _app()
    monkeypatch.setattr(
        settings_module.ExifToolService,
        "capability",
        lambda self: MetadataCapability("exiftool", False, None),
    )
    dialog = SettingsDialog(
        CameraCopyConfig(
            source="",
            destination=str(tmp_path / "pictures"),
            minrating=1,
            checkhash=True,
            durablewrites=True,
            useembeddedmetadata=True,
        ),
        volumes=[],
    )
    dialog.show()
    app.processEvents()

    assert dialog.hash_warning_label.isHidden()
    assert dialog.durable_writes_warning_label.isHidden()
    assert not dialog.rating_warning_label.isHidden()
    assert "ExifTool was not found" in dialog.rating_warning_label.text()

    dialog.embedded_metadata_checkbox.setChecked(False)
    app.processEvents()
    assert dialog.rating_warning_label.isHidden()

    dialog.embedded_metadata_checkbox.setChecked(True)
    app.processEvents()
    assert not dialog.rating_warning_label.isHidden()

    dialog.hash_checkbox.setChecked(False)
    dialog.durable_writes_checkbox.setChecked(False)
    app.processEvents()

    assert not dialog.hash_warning_label.isHidden()
    assert not dialog.durable_writes_warning_label.isHidden()
    assert dialog.hash_warning_label.styleSheet()
    assert dialog.durable_writes_warning_label.styleSheet()

    dialog.close()
    app.processEvents()


def test_formatting_destination_volume_is_a_hard_validation_error(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app = _app()
    mount = tmp_path / "card"
    destination = mount / "pictures"
    mount.mkdir()
    window = MainWindow(
        config_path=tmp_path / "cameracopy.json",
        config=CameraCopyConfig(source="", destination=str(destination)),
    )
    volume = VolumeInfo(id="card", display_name="Card", mount_path=mount)
    messages: list[tuple[str, str]] = []

    def capture_critical(_parent, title, text, *_args):  # noqa: ANN001, ANN202
        messages.append((title, text))
        return None

    monkeypatch.setattr("cameracopy2.ui.main_window.QMessageBox.critical", capture_critical)

    assert not window._confirm_destination_on_formatted_volume([volume])  # noqa: SLF001
    assert messages
    assert "Choose a destination on another volume" in messages[0][1]

    window.close()
    app.processEvents()


def test_settings_and_progress_dialog_expose_current_ui_contract(tmp_path: Path) -> None:
    app = _app()
    config = CameraCopyConfig(
        source="",
        destination=str(tmp_path / "pictures"),
        applicationname="RapidRaw",
        applicationpath=str(tmp_path / "RapidRaw.exe"),
        autoremove=True,
    )
    settings = SettingsDialog(config, volumes=[])
    progress = CopyProgressDialog(
        CopyJob(
            primary=VolumeInfo(
                id="card",
                display_name="Card",
                mount_path=tmp_path,
            ),
            secondary=None,
            config=config,
        )
    )

    tab_names = [settings.tabs.tabText(index) for index in range(settings.tabs.count())]
    assert "Appearance" in tab_names
    files_tab = settings.tabs.widget(tab_names.index("Files"))
    advanced_tab = settings.tabs.widget(tab_names.index("Advanced"))
    assert settings.autoremove_checkbox.isChecked()
    assert files_tab.isAncestorOf(settings.autoremove_checkbox)
    assert not advanced_tab.isAncestorOf(settings.autoremove_checkbox)
    assert progress.width() == 1000
    assert not progress.open_application_button.isHidden()
    assert "Open RapidRaw" in progress.open_application_button.text()

    help_texts = [
        label.text()
        for label in settings.findChildren(QLabel)
        if bool(label.property("settingsHelpText"))
    ]
    assert any("Leave the arguments empty, or omit %d" in text for text in help_texts)

    progress.close()
    settings.close()
    app.processEvents()


def test_settings_volume_refresh_uses_supplied_async_results(tmp_path: Path) -> None:
    app = _app()
    first = VolumeInfo(
        id="first",
        display_name="First",
        mount_path=tmp_path / "first",
        device_path="E:\\",
        device_serial="SER-1",
    )
    second = VolumeInfo(
        id="second",
        display_name="Second",
        mount_path=tmp_path / "second",
        device_path="F:\\",
        device_serial="SER-2",
    )
    dialog = SettingsDialog(CameraCopyConfig(), volumes=[first])

    initial_ids = {
        dialog.primary_default_combo.itemData(index)
        for index in range(dialog.primary_default_combo.count())
    }
    assert "first" in initial_ids
    assert "second" not in initial_ids

    dialog.update_volumes([first, second])

    refreshed_ids = {
        dialog.primary_default_combo.itemData(index)
        for index in range(dialog.primary_default_combo.count())
    }
    assert {"first", "second"}.issubset(refreshed_ids)

    dialog.close()
    app.processEvents()


def test_copy_dialog_shows_source_validation_before_copy_starts(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app = _app()
    destination = tmp_path / "pictures"
    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=tmp_path / "card",
        device_path="E:\\",
    )
    volume.mount_path.mkdir()
    requests: list[str] = []
    monkeypatch.setattr(
        MainWindow,
        "_request_volume_scan",
        lambda _self, kind, included_devices=None: requests.append(kind),
    )
    window = MainWindow(
        config_path=tmp_path / "cameracopy.json",
        config=CameraCopyConfig(source="", destination=str(destination)),
    )
    window._apply_volume_list([volume])  # noqa: SLF001
    window.primary_combo.setCurrentIndex(1)
    requests.clear()

    window.start_copy()
    app.processEvents()

    pending = window._pending_copy_start  # noqa: SLF001
    assert pending is not None
    assert requests == ["pre_copy"]
    assert pending.dialog.isVisible()
    assert pending.dialog.validation_pending
    assert pending.dialog.source_status_label.text() == "Validating source volumes…"
    assert pending.dialog.status_label.isHidden()
    assert pending.dialog.file_progress_bar.minimum() == 0
    assert pending.dialog.file_progress_bar.maximum() == 0
    assert not window.start_button.isEnabled()

    started_jobs: list[CopyJob] = []

    def capture_start(job: CopyJob) -> None:
        pending.dialog.validation_pending = False
        started_jobs.append(job)

    monkeypatch.setattr(pending.dialog, "start", capture_start)
    window._complete_pending_copy_start([volume])  # noqa: SLF001

    assert window._pending_copy_start is None  # noqa: SLF001
    assert started_jobs
    assert started_jobs[0].primary is volume
    assert pending.dialog.isVisible()
    assert not window.start_button.isEnabled()

    pending.dialog.reject()
    app.processEvents()
    assert window.start_button.isEnabled()
    window.close()
    app.processEvents()


def test_copy_dialog_cancel_during_source_validation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app = _app()
    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=tmp_path / "card",
        device_path="E:\\",
    )
    volume.mount_path.mkdir()
    monkeypatch.setattr(
        MainWindow,
        "_request_volume_scan",
        lambda _self, kind, included_devices=None: None,
    )
    window = MainWindow(
        config_path=tmp_path / "cameracopy.json",
        config=CameraCopyConfig(
            source="",
            destination=str(tmp_path / "pictures"),
        ),
    )
    window._apply_volume_list([volume])  # noqa: SLF001
    window.primary_combo.setCurrentIndex(1)

    window.start_copy()
    pending = window._pending_copy_start  # noqa: SLF001
    assert pending is not None

    pending.dialog.request_cancel()
    app.processEvents()

    assert window._pending_copy_start is None  # noqa: SLF001
    assert not pending.dialog.isVisible()
    assert window.start_button.isEnabled()
    window.close()
    app.processEvents()
