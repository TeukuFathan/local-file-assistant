"""Qt stylesheet and installed sans-serif font selection."""

from importlib.resources import files

from PySide6.QtGui import QFont, QFontDatabase


def choose_font(families, default):
    available = {family.casefold(): family for family in families}
    for family in ("Inter", "Inter Variable", "Segoe UI Variable Text", "Segoe UI", "Arial"):
        if family.casefold() in available:
            return available[family.casefold()]
    return default


def apply_theme(window):
    family = choose_font(QFontDatabase.families(), window.font().family())
    window.setFont(QFont(family, 11))
    window.setStyleSheet(files(__package__).joinpath("theme.qss").read_text(encoding="utf-8"))
    return family
