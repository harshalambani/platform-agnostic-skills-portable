"""
bundling/icons/make_splash.py -- generate the PortableApps.com Launcher splash.

The launcher shows App\\AppInfo\\Launcher\\splash.jpg for [Launch]:SplashTime
milliseconds while the app starts (see the launcher's SplashScreen.nsh). It is a
plain JPEG -- no text overlay, no progress bar: whatever the user reads during
startup has to be baked into the image, so the version line is drawn here.

Colours track ui/_theme.py (black + electric blue) so the splash and the window
it precedes look like the same product.

Regenerate after an icon or theme change:

    cd "C:\\Users\\inabm\\Documents\\Cowork Playground\\platform-agnostic-skills-portable"
    python bundling/icons/make_splash.py

The output is committed, but the build does not ship that copy: build.py runs
this script under the build venv's interpreter to redraw the splash into the
staging tree with the version being built, and falls back to the committed image
only if that fails. So the committed copy is the safety net, not the artifact --
keep it regenerated, since a fallback build ships whatever version line it holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE / "splash.jpg"
ICON = HERE / "appicon_1024.png"

# PortableApps splashes are small; this matches the common 500x400 shape so the
# launcher's centred, non-scaled blit looks right on any DPI.
W, H = 500, 400

BG_TOP = (10, 10, 10)        # #0A0A0A  primary surface
BG_BOTTOM = (23, 23, 23)     # #171717  secondary surface
BAR = (17, 24, 39)           # footer strip, a touch bluer than the field
ACCENT = (59, 130, 246)      # #3B82F6  accent
ACCENT_DIM = (37, 99, 235)   # #2563EB
TEXT = (245, 245, 245)       # #F5F5F5  body
MUTED = (163, 163, 163)      # #A3A3A3  muted

BAR_H = 54


def _font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    """First installed font that loads, else Pillow's bitmap default."""
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _vertical_gradient(img: Image.Image) -> None:
    """Paint the field: a subtle top-to-bottom lift, dark enough that the white
    icon and text stay high-contrast at any brightness."""
    d = ImageDraw.Draw(img)
    span = H - BAR_H
    for y in range(span):
        t = y / max(span - 1, 1)
        d.line(
            [(0, y), (W, y)],
            fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)),
        )


def build(version: str, out: Path | None = None) -> Path:
    """Draw the splash for *version* and write it to *out* (default: the
    committed bundling/icons/splash.jpg).

    build.py passes an explicit *out* inside the staging tree: a release build
    must not rewrite a tracked file just to stamp a version into it, or every
    CI build would finish with a dirty working tree.
    """
    out = Path(out) if out is not None else OUT
    img = Image.new("RGB", (W, H), BG_TOP)
    _vertical_gradient(img)
    d = ImageDraw.Draw(img)

    # -- icon -----------------------------------------------------------------
    if ICON.exists():
        icon = Image.open(ICON).convert("RGBA").resize((128, 128), Image.LANCZOS)
        img.paste(icon, ((W - 128) // 2, 54), icon)

    # -- wordmark -------------------------------------------------------------
    bold = _font(["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"], 34)
    reg = _font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], 15)
    small = _font(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"], 12)

    def centred(y: int, text: str, font, fill) -> None:
        w = d.textbbox((0, 0), text, font=font)[2]
        d.text(((W - w) // 2, y), text, font=font, fill=fill)

    centred(206, "PA Skills Portable", bold, TEXT)
    # Expands the acronym, which is the one thing the title cannot do for a
    # first-time user, and stays true as the skill set broadens -- unlike a
    # domain claim ("accounting and tax") or a deployment claim ("offline",
    # which this shipped with briefly and which is simply false whenever the
    # active endpoint is a cloud one). What is in the box TODAY is said in
    # appinfo.ini's Description instead, where being specific is the job.
    centred(250, "Platform Agnostic Skills — LLM powered", reg, MUTED)

    # A thin accent rule -- the one piece of electric blue in the field, so the
    # splash reads as the same product as the window behind it.
    d.rectangle([(W // 2 - 90, 284), (W // 2 + 90, 286)], fill=ACCENT)

    # -- footer ---------------------------------------------------------------
    d.rectangle([(0, H - BAR_H), (W, H)], fill=BAR)
    d.rectangle([(0, H - BAR_H), (W, H - BAR_H + 2)], fill=ACCENT_DIM)

    ver = f"Version {version}" if version else "Version -"
    d.text((18, H - BAR_H + 13), ver, font=reg, fill=TEXT)
    d.text((18, H - BAR_H + 33), "Starting up...", font=small, fill=MUTED)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=92, optimize=True)
    return out


def _version_from_changelog() -> str:
    """Read the newest released version out of CHANGELOG.md, so the splash never
    disagrees with the release it ships in."""
    import re
    cl = HERE.parents[1] / "CHANGELOG.md"
    try:
        for line in cl.read_text(encoding="utf-8").splitlines():
            m = re.match(r"##\s*\[(\d+\.\d+\.\d+)\]", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ""


if __name__ == "__main__":
    # Usage: make_splash.py [VERSION] [OUT_PATH]
    #
    # build.py invokes this as a subprocess with BOTH arguments, run by the build
    # venv's interpreter -- that venv has Pillow, whereas whatever interpreter is
    # running build.py may not (a CI runner's bare setup-python does not). Run by
    # hand with no arguments it redraws the committed image at the changelog
    # version, which is what the module docstring describes.
    v = sys.argv[1] if len(sys.argv) > 1 else _version_from_changelog()
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    print(f"wrote {build(v, out)} (version {v!r})")
