"""
triageQ visual identity: palette, font loading, and bundled asset paths.

Brand colors were sampled directly from the reference brand sheet (hex values
below are exact, not estimated). Typography is Inter, per that brand guide.

On a Google Fonts CDN link: that's an HTML/CSS mechanism — <link> tags that
fetch a stylesheet, which points a browser at font files, at request time.
None of that exists here. triageQ is a Tkinter desktop app with no browser
and no live network font loading, so the equivalent is what this module
does: bundle the actual Inter font files (fonts/Inter.ttf and
fonts/Inter-Italic.ttf, OFL-licensed — see fonts/OFL.txt) and load them for
this process only, at startup, with no install step required.

Platform support for that private, no-install load:
  Windows       AddFontResourceExW with FR_PRIVATE. No admin rights, no
                system-wide install, unloaded automatically when the
                process exits. This is the primary supported path.
  macOS/Linux   Not implemented here — private in-process font loading on
                these platforms needs OS-specific APIs (Core Text on macOS,
                fontconfig on Linux) this reference build doesn't bundle
                dependencies for. FONT_FAMILY still resolves to "Inter" on
                these platforms if it happens to already be installed
                system-wide, and falls back cleanly otherwise — see below.

Either way, resolve_font_family() always returns something usable: "Inter"
if the private load (or an existing system install) succeeded, otherwise the
platform's closest system font. Nothing here ever hard-fails on a missing
font — a Tk app that raised on a missing font family would be a strange
trade for a branding pass to make.
"""

from __future__ import annotations

import platform
import tkinter.font as tkfont
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent / "fonts"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

INTER_REGULAR = FONTS_DIR / "Inter.ttf"          # variable font: weights 100-900
INTER_ITALIC = FONTS_DIR / "Inter-Italic.ttf"     # variable font, italic axis

ICON_ICO = ASSETS_DIR / "icon.ico"                # multi-res, for Windows iconbitmap
ICON_PNG_256 = ASSETS_DIR / "icon_256.png"        # for cross-platform iconphoto
ICON_PNG_32 = ASSETS_DIR / "icon_32.png"          # for the About card
LOGO_PNG = ASSETS_DIR / "logo.png"                # wordmark, transparent background

# Learning Data Insights attribution mark — the source file is a white LDI
# mark on a transparent background, meant to sit on a colored surface, not
# directly on the light header/card backgrounds this app mostly uses. The
# _badge_* variants are pre-composited onto a small Deep Blue circle for
# exactly that reason; the plain white mark stays available for anywhere
# already showing a dark/brand-colored surface.
LDI_LOGO_WHITE = ASSETS_DIR / "ldi_logo_white.png"
LDI_BADGE_28 = ASSETS_DIR / "ldi_badge_28.png"    # header credit line
LDI_BADGE_44 = ASSETS_DIR / "ldi_badge_44.png"    # About card


# ── Brand palette ───────────────────────────────────────────────────────────
# Sampled from the reference brand sheet's color swatches.

DEEP_BLUE = "#244467"      # Trust, stability, credibility
BRAND_BLUE = "#367EC5"     # Focus, clarity, approachability
TEAL = "#4B9F87"           # Balance, open-mindedness
AMBER = "#EFBC54"          # Attention, what needs a closer look
LAVENDER = "#9E92C7"       # Nuance, uncertainty, human judgment


# ── Font loading ────────────────────────────────────────────────────────────

_FALLBACKS = {
    "Windows": "Segoe UI",
    "Darwin": "Helvetica Neue",
}
_UNIVERSAL_FALLBACK = "Helvetica"


def _fallback_family() -> str:
    return _FALLBACKS.get(platform.system(), _UNIVERSAL_FALLBACK)


def _load_windows_private(path: Path) -> bool:
    """Load a font file for this process only. No install, no admin rights,
    no trace left after the process exits. Returns whether it worked."""
    if not path.exists():
        return False
    try:
        import ctypes
        FR_PRIVATE = 0x10
        n = ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0)
        return n > 0
    except Exception:
        return False


def resolve_font_family(root=None) -> str:
    """Make Inter available for this session if at all possible, and return
    the family name to actually use everywhere else in the app — "Inter" on
    success, a close system fallback otherwise.

    Call this once, early, right after the Tk root is constructed (pass it
    as `root`) and before any widgets are built — tkinter.font.families()
    needs a live default font system to query, and every font tuple built
    afterward should read the same resolved name.
    """
    if platform.system() == "Windows":
        ok_regular = _load_windows_private(INTER_REGULAR)
        _load_windows_private(INTER_ITALIC)  # best-effort; regular alone still works
        if ok_regular:
            return "Inter"

    # Either not Windows, or the private load failed (missing file, locked,
    # etc.) — check whether Inter is already installed system-wide before
    # giving up and falling back.
    try:
        families = set(tkfont.families(root)) if root is not None else set(tkfont.families())
    except Exception:
        families = set()
    if "Inter" in families:
        return "Inter"

    return _fallback_family()
