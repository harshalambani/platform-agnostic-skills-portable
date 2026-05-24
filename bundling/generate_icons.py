"""Generate PA Skills Portable icon artwork.

Produces a gear + sparkle motif inside a rounded-square container,
using the project palette: #0A0A0A primary, #3B82F6 accent.

Output files (in bundling/icons/):
  appicon_128.png, appicon_75.png, appicon_32.png, appicon_16.png, appicon.ico

Run:
    python bundling/generate_icons.py
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# --- Palette ---
BG_COLOR = (10, 10, 10, 255)         # #0A0A0A
ACCENT = (59, 130, 246, 255)         # #3B82F6
ACCENT_LIGHT = (96, 165, 250, 255)   # lighter blue for sparkle highlight
BORDER_COLOR = (40, 40, 40, 255)     # subtle edge on container

# --- Geometry (designed at 512px, scaled down) ---
MASTER_SIZE = 512
CORNER_RADIUS = 96  # rounded-square radius at 512px


def _rounded_rect_mask(size: int, radius: int) -> Image.Image:
    """Create an alpha mask for a rounded rectangle."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _draw_gear(draw: ImageDraw.Draw, cx: float, cy: float, outer_r: float,
               inner_r: float, teeth: int = 8, tooth_width_deg: float = 18.0,
               color=ACCENT):
    """Draw a gear/cog shape centered at (cx, cy)."""
    # Build gear polygon
    points = []
    for i in range(teeth):
        base_angle = (360 / teeth) * i
        # Tooth rise
        a1 = math.radians(base_angle - tooth_width_deg / 2)
        a2 = math.radians(base_angle + tooth_width_deg / 2)
        # Valley between teeth
        a3 = math.radians(base_angle + tooth_width_deg / 2)
        a4 = math.radians(base_angle + (360 / teeth) - tooth_width_deg / 2)

        # Outer edge of tooth
        points.append((cx + outer_r * math.cos(a1), cy + outer_r * math.sin(a1)))
        points.append((cx + outer_r * math.cos(a2), cy + outer_r * math.sin(a2)))
        # Inner edge (valley)
        points.append((cx + inner_r * math.cos(a3), cy + inner_r * math.sin(a3)))
        points.append((cx + inner_r * math.cos(a4), cy + inner_r * math.sin(a4)))

    draw.polygon(points, fill=color)

    # Center hole
    hole_r = inner_r * 0.38
    draw.ellipse(
        [cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r],
        fill=BG_COLOR,
    )


def _draw_sparkle(draw: ImageDraw.Draw, cx: float, cy: float, size: float,
                  color=ACCENT_LIGHT):
    """Draw a 4-point sparkle/star at (cx, cy)."""
    # 4-point star via two thin diamonds overlaid
    half = size / 2
    thin = size * 0.15  # arm thickness

    # Vertical diamond
    points_v = [
        (cx, cy - half),
        (cx + thin, cy),
        (cx, cy + half),
        (cx - thin, cy),
    ]
    # Horizontal diamond
    points_h = [
        (cx - half, cy),
        (cx, cy - thin),
        (cx + half, cy),
        (cx, cy + thin),
    ]
    draw.polygon(points_v, fill=color)
    draw.polygon(points_h, fill=color)


def generate_master() -> Image.Image:
    """Render the 512px master icon."""
    img = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. Rounded-square container
    # Draw filled bg with border
    draw.rounded_rectangle(
        [0, 0, MASTER_SIZE - 1, MASTER_SIZE - 1],
        radius=CORNER_RADIUS,
        fill=BG_COLOR,
        outline=BORDER_COLOR,
        width=4,
    )

    # 2. Gear — slightly offset up-left from center for visual balance with sparkle
    gear_cx = MASTER_SIZE * 0.46
    gear_cy = MASTER_SIZE * 0.52
    gear_outer = MASTER_SIZE * 0.30
    gear_inner = MASTER_SIZE * 0.22
    _draw_gear(draw, gear_cx, gear_cy, gear_outer, gear_inner, teeth=8, tooth_width_deg=18)

    # 3. Sparkle — upper-right, overlapping gear slightly
    sparkle_cx = MASTER_SIZE * 0.72
    sparkle_cy = MASTER_SIZE * 0.28
    sparkle_size = MASTER_SIZE * 0.22
    _draw_sparkle(draw, sparkle_cx, sparkle_cy, sparkle_size, color=ACCENT_LIGHT)

    # Small secondary sparkle for depth
    _draw_sparkle(draw, MASTER_SIZE * 0.82, MASTER_SIZE * 0.42, MASTER_SIZE * 0.09, color=ACCENT)

    # Apply rounded mask to clip any anti-aliasing overflow
    mask = _rounded_rect_mask(MASTER_SIZE, CORNER_RADIUS)
    img.putalpha(mask)

    return img


def main():
    project_root = Path(__file__).resolve().parent.parent
    icons_dir = project_root / "bundling" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    print("Generating PA Skills icon artwork...")
    master = generate_master()

    # Produce PNGs at required sizes
    sizes = {"128": 128, "75": 75, "32": 32, "16": 16}
    png_paths = {}
    for label, px in sizes.items():
        resized = master.resize((px, px), Image.LANCZOS)
        out_path = icons_dir / f"appicon_{label}.png"
        resized.save(out_path, "PNG")
        png_paths[label] = out_path
        print(f"  ok  {out_path.relative_to(project_root)} ({px}x{px})")

    # Produce ICO with multiple sizes embedded
    ico_path = icons_dir / "appicon.ico"
    ico_sizes = [16, 32, 48, 64, 128, 256]
    ico_images = [master.resize((s, s), Image.LANCZOS) for s in ico_sizes]
    ico_images[0].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=ico_images[1:],
    )
    print(f"  ok  {ico_path.relative_to(project_root)} ({len(ico_sizes)} sizes)")

    print("\nDone. Files in bundling/icons/:")
    for f in sorted(icons_dir.iterdir()):
        print(f"  {f.name}")


if __name__ == "__main__":
    main()
