"""
autolens.theme: one palette and one stylesheet for the AutoLens workspace.

Same family as Match: dark, low-glare greys so the plate is the brightest thing
on screen, one cyan accent, and fixed status colours shared by the viewer chip,
the step pills and the timeline. Pure Python (no Qt) so it can be tested.
"""
from __future__ import annotations

ACCENT = "#27cfe8"
ACCENT_DIM = "#1f3a42"
ACCENT_HOVER = "#5bdcef"

BG_0 = "#0c0d0f"    # viewer / deepest
BG_1 = "#121417"    # timeline, inputs
BG_2 = "#16181b"    # window
BG_3 = "#1b1e22"    # panels
BG_4 = "#22262b"    # buttons
LINE = "#2b3036"
EDGE = "#262a2f"
TEXT = "#d7dbe0"
TEXT_HI = "#f1f3f5"
TEXT_DIM = "#8a919a"
TEXT_FAINT = "#6b727b"

# Timeline / overlay colours.
GUIDE = "#5b9bd5"        # surface guide-frame markers
CACHE = "#3fb37f"        # frames decoded in memory
CACHE_DISK = "#24533c"   # frames rendered to disk
PLAYHEAD = "#f1f3f5"
SURFACE_ON = "#69e178"   # a surface on its own guide frame
SURFACE_OFF = "#cdcdcd"  # a surface seen from another frame
MASK = "#e0605e"         # tracking-mask exclusion overlay

# Viewer chip states: (text colour, background, border).
CHIP = {
    "none": (TEXT_FAINT, "transparent", "#30353b"),
    "running": ("#cfeff5", "#1d2f35", "#3c5a63"),
    "solved": ("#bff0d2", "#173326", "#2f5a3d"),
    "failed": ("#ffd1c9", "#3a1f1f", "#7a3434"),
}

UI_FONT = "Segoe UI"
MONO_FONT = "Consolas"

STYLESHEET = """
QDialog#AutoLensWorkspace {{ background: {bg2}; color: {text}; font-family: '{ui}'; font-size: 12px; }}
QDialog#AutoLensWorkspace QWidget {{ color: {text}; font-family: '{ui}'; font-size: 12px; }}
QToolTip {{ background: {bg1}; color: {texthi}; border: 1px solid {line}; padding: 6px; }}
QMessageBox, QProgressDialog, QInputDialog, QDialog {{ background: {bg3}; }}
QMessageBox QLabel {{ color: {texthi}; font-size: 12px; }}
QMessageBox QPushButton {{ min-width: 72px; }}
QProgressBar {{ background: {bg1}; border: 1px solid {line}; border-radius: 3px; text-align: center;
    color: {texthi}; max-height: 14px; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}
QMenu {{ background: {bg3}; border: 1px solid {line}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; color: {text}; }}
QMenu::item:selected {{ background: {accentdim}; color: {texthi}; }}
QMenu::item:disabled {{ color: #555b62; }}
QMenu::separator {{ height: 1px; background: {line}; margin: 4px 8px; }}
QListWidget {{ background: {bg1}; border: 1px solid {edge}; border-radius: 4px; color: {text}; outline: 0; }}
QListWidget::item {{ padding: 5px 6px; }}
QListWidget::item:selected {{ background: {accentdim}; color: {texthi}; }}
QScrollArea {{ background: transparent; border: 0; }}
QGroupBox {{ border: 1px solid {edge}; border-radius: 4px; margin-top: 14px; padding-top: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; color: {textfaint}; }}

QFrame#TopBar {{ background: #0f1113; border-bottom: 1px solid {edge}; }}
QFrame#SidePanel {{ background: {bg3}; border-left: 1px solid {edge}; }}
QFrame#BottomBar {{ background: {bg1}; border-top: 1px solid {edge}; }}
QFrame#StatusBar {{ background: #0f1113; border-top: 1px solid {edge}; }}
QFrame#Card {{ background: {bg1}; border: 1px solid {edge}; border-radius: 4px; }}
QFrame#Separator {{ background: #30353b; }}

QPushButton#Step {{ border-radius: 12px; padding: 3px 14px; background: transparent; border: 1px solid {line};
    color: {textfaint}; }}
QPushButton#Step:hover {{ border-color: #4a525b; color: {text}; }}
QPushButton#Step[state="done"] {{ color: #7bd88f; border-color: #2f5a3d; }}
QPushButton#Step[state="current"] {{ color: #071317; background: {accent}; border-color: {accent}; font-weight: 600; }}
QPushButton#Step[state="warn"] {{ color: #ffd1c9; background: #3a1f1f; border-color: #7a3434; font-weight: 600; }}
QLabel#StepArrow {{ color: {textfaint}; }}

QPushButton#MenuBarButton {{ background: transparent; border: 0; padding: 6px 10px; color: {text}; }}
QPushButton#MenuBarButton:hover, QPushButton#MenuBarButton:pressed {{ background: #23272c; border-radius: 4px; }}
QPushButton#MenuBarButton::menu-indicator {{ width: 0; image: none; }}

QLabel#Brand {{ color: {texthi}; font-weight: 600; font-size: 14px; letter-spacing: 3px; }}
QLabel#Faint {{ color: {textfaint}; }}
QLabel#Dim {{ color: {textdim}; }}
QLabel#Section {{ color: {textfaint}; font-size: 10px; letter-spacing: 1.5px; }}
QLabel#ShotInfo {{ color: {textdim}; }}
QLabel#Hint {{ color: {text}; }}
QLabel#Chip {{ border-radius: 9px; padding: 1px 9px; font-size: 10px; letter-spacing: 1px; font-weight: 600; }}

QPushButton {{ background: {bg4}; border: 1px solid {line}; border-radius: 4px; padding: 5px 10px; color: {text}; }}
QPushButton:hover {{ background: #2a2f35; border-color: #3a4048; }}
QPushButton:pressed {{ background: #1d2125; }}
QPushButton:disabled {{ color: #555b62; background: #1c1f23; border-color: #24282d; }}
QPushButton:checked {{ background: {accentdim}; border-color: {accent}; color: {texthi}; }}
QPushButton#Primary {{ background: {accent}; border: 0; color: #071317; font-weight: 600; padding: 7px 14px; }}
QPushButton#Primary:hover {{ background: {accenthover}; }}
QPushButton#Primary:disabled {{ background: #1f3a42; color: #4f6d75; }}
QPushButton#Tool {{ min-width: 30px; max-width: 34px; padding: 4px 0; }}
QPushButton#Transport {{ min-width: 30px; max-width: 34px; padding: 3px 0; }}
QPushButton#TrackButton {{ min-width: 38px; max-width: 42px; padding: 3px 0; background: #1d2f35; border-color: #3c5a63; }}
QPushButton#TrackButton:hover {{ background: #24434c; border-color: {accent}; }}
QPushButton#TrackButton:disabled {{ background: #1c1f23; border-color: #24282d; }}
QPushButton#Stop {{ min-width: 38px; max-width: 42px; padding: 3px 0; background: #3a2328; border-color: #5a2f37; }}
QPushButton#Stop:disabled {{ background: #1c1f23; border-color: #24282d; }}
QPushButton#Toggle {{ border-radius: 12px; padding: 3px 10px; background: transparent; color: {textfaint}; }}
QPushButton#Toggle:checked {{ background: #1d2f35; border-color: #3c5a63; color: #cfeff5; }}
QPushButton#Disclosure {{ background: transparent; border: 0; color: {textfaint}; text-align: left; padding: 4px 0; }}
QPushButton#Disclosure:hover {{ color: {text}; }}
QPushButton#MenuButton {{ padding-right: 20px; }}
QPushButton::menu-indicator {{ subcontrol-position: right center; right: 6px; }}
QSplitter::handle {{ background: #22262b; }}
QSplitter::handle:hover {{ background: {accent}; }}

QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox {{ background: {bg1}; border: 1px solid #30353b; border-radius: 3px;
    padding: 3px 6px; color: #e6e9ec; min-height: 20px; }}
QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {accent}; }}
QSpinBox#FrameField {{ font-family: '{mono}'; }}
QAbstractSpinBox[bare="true"]::up-button, QAbstractSpinBox[bare="true"]::down-button {{ width: 0px; border: 0; }}
QAbstractSpinBox[bare="true"]::up-arrow, QAbstractSpinBox[bare="true"]::down-arrow {{ width: 0px; height: 0px; }}
QComboBox QAbstractItemView {{ background: {bg3}; border: 1px solid {line}; selection-background-color: {accentdim}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 28px; height: 15px; border-radius: 7px; background: #30353b; }}
QCheckBox::indicator:checked {{ background: {accent}; }}
QCheckBox::indicator:disabled {{ background: #24282d; }}

QPlainTextEdit {{ background: {bg1}; border: 1px solid {edge}; border-radius: 4px; font-family: '{mono}';
    font-size: 11px; color: #b9c0c8; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: #30353b; border-radius: 4px; min-height: 20px; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; }}
QScrollBar::handle:horizontal {{ background: #30353b; border-radius: 4px; min-width: 20px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
""".format(
    bg1=BG_1, bg2=BG_2, bg3=BG_3, bg4=BG_4, line=LINE, edge=EDGE,
    text=TEXT, texthi=TEXT_HI, textdim=TEXT_DIM, textfaint=TEXT_FAINT,
    accent=ACCENT, accentdim=ACCENT_DIM, accenthover=ACCENT_HOVER,
    ui=UI_FONT, mono=MONO_FONT,
)


def chip_style(state):
    """Inline stylesheet for the viewer's status chip in `state`."""
    fg, bg, border = CHIP.get(state, CHIP["none"])
    return f"color: {fg}; background: {bg}; border: 1px solid {border};"


def rgb(hex_colour):
    """'#27cfe8' -> (39, 207, 232)."""
    text = str(hex_colour).lstrip("#")
    if len(text) != 6:
        raise ValueError(f"Expected #rrggbb, got {hex_colour!r}")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
