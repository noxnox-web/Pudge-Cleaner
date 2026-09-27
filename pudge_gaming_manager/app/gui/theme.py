"""Dark theme for the administrator interface.

Design constraints (rule #69): dark, modern, compact, strong typography,
clear status indicators, no decorative colour. An administrator glancing at a
club PC should read its state in under a second, so status colour is reserved
exclusively for status — never for branding or emphasis.
"""

from __future__ import annotations

# -- palette ---------------------------------------------------------------

BACKGROUND = "#0e1116"
SURFACE = "#161b22"
SURFACE_RAISED = "#1c2230"
BORDER = "#2a313c"

TEXT = "#e6edf3"
TEXT_MUTED = "#9da7b3"
TEXT_FAINT = "#848d97"
"""The faintest text still reads at 4.5:1 on every surface it sits on
(SURFACE_RAISED is the darkest case, 4.7:1). Anything fainter is
decoration, and this interface has none."""

ACCENT = "#96c12a"
"""The lime of the club logo. It marks the one primary action, keyboard
focus and selection — never a status, which keeps GOOD green unambiguous."""
ACCENT_HOVER = "#a6d13a"
ACCENT_PRESSED = "#86ad22"
ACCENT_TEXT = "#0e1116"
"""Text on an accent fill: dark, as the logo's lettering is (9:1). White
on this lime would read at under 2:1."""

GOOD = "#3fb950"
WARNING = "#d29922"
CRITICAL = "#f85149"
UNAVAILABLE = "#6e7681"
"""Grey, deliberately. An unreadable value is not a fault in the PC and must
not look like one."""

STATUS_COLOURS: dict[str, str] = {
    "GOOD": GOOD,
    "WARNING": WARNING,
    "CRITICAL": CRITICAL,
    "UNAVAILABLE": UNAVAILABLE,
}

FONT_STACK = '"Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif'
MONO_STACK = '"Cascadia Mono", "Consolas", monospace'


def stylesheet() -> str:
    """Return the application-wide Qt stylesheet."""
    return f"""
    QWidget {{
        background-color: {BACKGROUND};
        color: {TEXT};
        font-family: {FONT_STACK};
        font-size: 14px;
    }}

    /* Labels sit inside cards, which are lighter than the window. Without
       this they inherit the window background and render as dark boxes. */
    QLabel {{
        background: transparent;
    }}

    QLabel#Title {{
        font-size: 22px;
        font-weight: 700;
        letter-spacing: 0.5px;
    }}
    QLabel#Subtitle {{
        color: {TEXT_MUTED};
        font-size: 13px;
    }}
    /* The author credit, set on the title's baseline. Deliberately small
       and faint: it is a signature, not a second heading. */
    QLabel#Byline {{
        color: {TEXT_FAINT};
        font-size: 11px;
        font-style: italic;
        padding-bottom: 3px;
    }}
    QLabel#SectionHeading {{
        color: {TEXT_MUTED};
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 1.4px;
    }}

    QFrame#Card {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    QFrame#Divider {{
        background-color: {BORDER};
        max-height: 1px;
        border: none;
    }}

    QLabel#MetricName   {{ color: {TEXT_MUTED}; font-size: 13px; }}
    QLabel#MetricValue  {{ font-size: 15px; font-weight: 600; }}
    QLabel#MetricDetail {{ color: {TEXT_FAINT}; font-size: 12px; }}

    QLabel#ScoreValue {{
        font-size: 56px;
        font-weight: 800;
        letter-spacing: -1px;
    }}
    QLabel#ScoreCaption {{
        color: {TEXT_MUTED};
        font-size: 12px;
        letter-spacing: 1.2px;
        font-weight: 700;
    }}
    QLabel#ScoreNote {{
        color: {TEXT_FAINT};
        font-size: 11px;
    }}

    QPushButton#Primary {{
        background-color: {ACCENT};
        color: {ACCENT_TEXT};
        border: 2px solid {ACCENT};
        border-radius: 8px;
        padding: 12px 26px;
        font-size: 15px;
        font-weight: 700;
    }}
    QPushButton#Primary:hover    {{ background-color: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
    QPushButton#Primary:pressed  {{ background-color: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}
    QPushButton#Primary:disabled {{
        background-color: {SURFACE_RAISED};
        border-color: {SURFACE_RAISED};
        color: {TEXT_FAINT};
    }}

    QPushButton#Secondary {{
        background-color: transparent;
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 8px;
        padding: 7px 14px;
        font-size: 13px;
    }}
    QPushButton#Secondary:hover    {{ background-color: {SURFACE_RAISED}; border-color: {TEXT_FAINT}; }}
    QPushButton#Secondary:pressed  {{ background-color: {BORDER}; }}
    QPushButton#Secondary:disabled {{ color: {TEXT_FAINT}; border-color: {BORDER}; }}

    /* One choice of several (widgets.Segmented): the chosen one is raised
       and bold, never accent-filled — the window's one filled button is
       OPTIMIZE. Middle segments drop their side borders so neighbours share
       a single divider. */
    QPushButton#Segment {{
        background-color: transparent;
        color: {TEXT_MUTED};
        border: 1px solid {BORDER};
        padding: 7px 12px;
        font-size: 13px;
    }}
    QPushButton#Segment[edge="first"] {{
        border-top-left-radius: 8px;
        border-bottom-left-radius: 8px;
    }}
    QPushButton#Segment[edge="last"] {{
        border-top-right-radius: 8px;
        border-bottom-right-radius: 8px;
    }}
    QPushButton#Segment[edge="middle"] {{ border-left: none; border-right: none; }}
    QPushButton#Segment:hover   {{ color: {TEXT}; }}
    QPushButton#Segment:checked {{
        background-color: {SURFACE_RAISED};
        color: {TEXT};
        font-weight: 700;
    }}
    QPushButton#Segment:disabled {{ color: {TEXT_FAINT}; }}

    /* Keyboard focus. Qt stops drawing its own focus rectangle once a
       button's border is styled, so without these a Tab press moves focus
       somewhere invisible. The property is set only for focus that arrived
       from the keyboard (see widgets.KeyboardFocusRing), so a mouse click
       does not leave a ring behind. Border width is traded against padding
       so the ring never shifts the layout. */
    QPushButton#Primary[kbfocus="true"] {{ border-color: {TEXT}; }}
    QPushButton#Secondary[kbfocus="true"] {{
        border: 2px solid {ACCENT};
        padding: 6px 13px;
    }}
    QPushButton#Segment[kbfocus="true"] {{ border-color: {ACCENT}; }}
    QCheckBox {{ border: 1px solid transparent; border-radius: 4px; padding: 2px; }}
    QCheckBox[kbfocus="true"],
    QToolButton#Disclosure[kbfocus="true"] {{ border: 1px solid {ACCENT}; }}
    QListWidget[kbfocus="true"],
    QScrollArea[kbfocus="true"] {{ border: 1px solid {ACCENT}; border-radius: 8px; }}
    QComboBox[kbfocus="true"] {{ border: 1px solid {ACCENT}; }}

    /* A collapsible section header: reads as a link-weight control, not as
       a third button style. */
    QToolButton#Disclosure {{
        background: transparent;
        color: {TEXT_MUTED};
        border: 1px solid transparent;
        border-radius: 4px;
        padding: 2px 4px;
        font-size: 12px;
    }}
    QToolButton#Disclosure:hover {{ color: {TEXT}; }}

    /* One finding: the title carries the weight, the explanation sits
       under it in the muted tone. */
    QFrame#Finding {{
        background-color: {SURFACE_RAISED};
        border: 1px solid {BORDER};
        border-radius: 8px;
    }}
    QLabel#FindingTitle  {{ font-size: 14px; font-weight: 600; }}
    QLabel#FindingDetail {{ color: {TEXT_MUTED}; font-size: 13px; }}
    QLabel#FindingEmpty  {{ color: {TEXT_MUTED}; font-size: 13px; }}
    QScrollArea#Findings, QWidget#FindingsBody {{
        background: transparent;
        border: 1px solid transparent;
    }}

    QListWidget {{
        background-color: transparent;
        border: none;
        outline: none;
    }}
    QListWidget::item {{
        background-color: {SURFACE_RAISED};
        border: 1px solid {BORDER};
        border-radius: 8px;
        padding: 10px 12px;
        margin-bottom: 6px;
    }}
    QListWidget::item:selected {{
        border-color: {ACCENT};
        color: {TEXT};
    }}

    QProgressBar {{
        background-color: {SURFACE_RAISED};
        border: none;
        border-radius: 3px;
        height: 6px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background-color: {ACCENT};
        border-radius: 3px;
    }}

    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER};
        border-radius: 5px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {TEXT_FAINT}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: {BORDER};
        border-radius: 5px;
        min-width: 30px;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    /* The settings dialog puts these inside cards, where the window
       background would show as a dark box — same reason as QLabel. */
    QCheckBox {{ background: transparent; spacing: 8px; }}
    QComboBox {{
        background-color: {SURFACE_RAISED};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 6px 10px;
        min-width: 260px;
    }}
    QComboBox:hover {{ border-color: {ACCENT}; }}
    QComboBox:disabled {{ color: {TEXT_FAINT}; }}
    QComboBox QAbstractItemView {{
        background-color: {SURFACE_RAISED};
        border: 1px solid {BORDER};
        selection-background-color: {ACCENT};
        selection-color: {ACCENT_TEXT};
    }}

    QToolTip {{
        background-color: {SURFACE_RAISED};
        color: {TEXT};
        border: 1px solid {BORDER};
        padding: 6px 8px;
        border-radius: 6px;
    }}
    """


def status_colour(status: str) -> str:
    return STATUS_COLOURS.get(status, UNAVAILABLE)


def score_colour(value: float | None) -> str:
    """Colour for the headline score. Grey when there is no score."""
    if value is None:
        return UNAVAILABLE
    if value >= 80:
        return GOOD
    if value >= 55:
        return WARNING
    return CRITICAL
