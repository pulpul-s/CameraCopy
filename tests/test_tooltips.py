from __future__ import annotations

from cameracopy2.ui.tooltip_text import help_tooltip_text


def test_tooltips_use_plain_text_with_deliberate_paragraph_breaks() -> None:
    tooltip = help_tooltip_text(
        "Root folder where CameraCopy creates dated folders.",
        "Copied files are written there.",
    )

    assert tooltip == (
        "Root folder where CameraCopy creates dated folders.\n\n"
        "Copied files are written there."
    )
    assert "<qt>" not in tooltip
    assert "width" not in tooltip


def test_plain_tooltip_text_preserves_literal_characters() -> None:
    assert help_tooltip_text("Use <name> & value") == "Use <name> & value"
