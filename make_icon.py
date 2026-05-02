"""Generate compr.ico — a green C being cut by purple scissors. Theme-matched."""
from __future__ import annotations

import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "compr.ico"

BG     = (30, 30, 30, 255)      # #1e1e1e
GREEN  = (63, 185, 80, 255)     # #3fb950
PURPLE = (163, 113, 247, 255)   # #a371f7
WHITE  = (255, 255, 255, 255)

S = 512  # high-res master, downscaled into the ico


def find_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_c(d: ImageDraw.ImageDraw) -> None:
    font = find_font(int(S * 0.78))
    text = "C"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]; th = bbox[3] - bbox[1]
    x = (S - tw) // 2 - bbox[0]
    y = (S - th) // 2 - bbox[1] - int(S * 0.03)
    d.text((x, y), text, font=font, fill=GREEN)


def draw_scissors(img: Image.Image) -> None:
    """Draw scissors on a transparent layer, rotated, then paste."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    pivot = (S // 2, S // 2)
    blade_len = int(S * 0.50)
    blade_thick = int(S * 0.030)
    handle_r = int(S * 0.095)
    handle_thick = int(S * 0.028)
    handle_dist = int(S * 0.26)

    # Two blades extending right from pivot, fanned slightly.
    for spread in (-1, 1):
        ang = math.radians(spread * 6)
        tip = (pivot[0] + blade_len * math.cos(ang),
               pivot[1] + blade_len * math.sin(ang))
        d.line([pivot, tip], fill=PURPLE, width=blade_thick)
        # tiny pointed tip cap
        d.ellipse([(tip[0] - blade_thick // 2, tip[1] - blade_thick // 2),
                   (tip[0] + blade_thick // 2, tip[1] + blade_thick // 2)],
                  fill=PURPLE)

    # Two handle rings extending left from pivot.
    for spread in (-1, 1):
        ang = math.radians(180 + spread * 18)
        cx = pivot[0] + handle_dist * math.cos(ang)
        cy = pivot[1] + handle_dist * math.sin(ang)
        # connector from pivot to ring
        nearest = (cx - handle_r * math.cos(ang) * 0.6,
                   cy - handle_r * math.sin(ang) * 0.6)
        d.line([pivot, nearest], fill=PURPLE, width=blade_thick)
        d.ellipse([(cx - handle_r, cy - handle_r),
                   (cx + handle_r, cy + handle_r)],
                  outline=PURPLE, width=handle_thick)

    # Pivot rivet — drawn last so it sits on top
    rv = int(S * 0.022)
    d.ellipse([(pivot[0] - rv, pivot[1] - rv),
               (pivot[0] + rv, pivot[1] + rv)], fill=WHITE)

    # Rotate the whole scissors so they cut diagonally across the C
    layer = layer.rotate(-25, resample=Image.BICUBIC, center=pivot)
    img.alpha_composite(layer)


def build() -> None:
    base = Image.new("RGBA", (S, S), BG)
    # rounded-square mask for a softer icon corner
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([(0, 0), (S, S)], radius=int(S * 0.18), fill=255)
    bg_panel = Image.new("RGBA", (S, S), BG)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(bg_panel, mask=mask)

    d = ImageDraw.Draw(out)
    draw_c(d)
    draw_scissors(out)

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
             (128, 128), (256, 256)]
    out.save(OUT, sizes=sizes)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    build()
