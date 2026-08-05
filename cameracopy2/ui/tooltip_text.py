from __future__ import annotations


def help_tooltip_text(*paragraphs: str) -> str:
    """Return plain Qt tooltip text with deliberate paragraph breaks."""
    return "\n\n".join(paragraph.strip() for paragraph in paragraphs if paragraph.strip())
