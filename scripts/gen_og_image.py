#!/usr/bin/env python3
"""Generate a 1200x630 og:image for AstroKarta using brand colors."""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math

W, H = 1200, 630
BG = (8, 7, 26)          # #08071a
BG_RAISED = (15, 14, 38) # #0f0e26
ACCENT = (124, 58, 237)  # #7c3aed
GILT = (232, 197, 107)   # #e8c56b
TEXT_HEADING = (214, 198, 255)  # #d6c6ff
TEXT = (230, 226, 244)    # #e6e2f4
TEXT_MUTED = (154, 147, 184)  # #9a93b8

# --- Background: vertical gradient + radial accent glow at top-center ---
base = Image.new("RGB", (W, H))
px = base.load()

# vertical gradient BG -> BG_RAISED
for y in range(H):
    t = y / H
    r = int(BG[0] + (BG_RAISED[0] - BG[0]) * t)
    g = int(BG[1] + (BG_RAISED[1] - BG[1]) * t)
    b = int(BG[2] + (BG_RAISED[2] - BG[2]) * t)
    for x in range(W):
        px[x, y] = (r, g, b)

# radial glow at top-center (hero-glow)
cx, cy, radius = W // 2, -60, 620
for y in range(H):
    for x in range(W):
        d = math.hypot(x - cx, y - cy)
        if d < radius:
            k = (1 - d / radius) ** 2 * 0.30
            r = int(px[x, y][0] + (ACCENT[0] - px[x, y][0]) * k)
            g = int(px[x, y][1] + (ACCENT[1] - px[x, y][1]) * k)
            b = int(px[x, y][2] + (ACCENT[2] - px[x, y][2]) * k)
            px[x, y] = (r, g, b)

img = base.convert("RGBA")

# --- Logo on the left ---
logo = Image.open("static/logo-full.png").convert("RGBA")
logo_size = 360
logo = logo.resize((logo_size, logo_size), Image.LANCZOS)
logo_x = 70
logo_y = (H - logo_size) // 2 - 10
img.paste(logo, (logo_x, logo_y), logo)

# --- Text on the right ---
draw = ImageDraw.Draw(img)
try:
    serif = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
    sans = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    sans_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
except Exception:
    raise

def font(path, size):
    return ImageFont.truetype(path, size)

text_left = logo_x + logo_size + 60  # ~490
text_right = W - 70

title = "АстроКарта"
tagline = "Твоята натална карта, разчетена\nна разбираем език"

title_font = font(serif, 76)
tagline_font = font(sans, 30)
kicker_font = font(sans_bold, 22)

# Kicker (small gold uppercase-ish label)
kicker = "НАТАЛНА КАРТА · НУМЕРОЛОГИЯ · ПРОГНОЗИ"
draw.text((text_left, 150), kicker, font=kicker_font, fill=GILT)

# Title
draw.text((text_left, 195), title, font=title_font, fill=TEXT_HEADING)

# Tagline
draw.text((text_left, 330), tagline, font=tagline_font, fill=TEXT, spacing=8)

# subtle divider line above tagline
draw.line([(text_left, 300), (text_left + 380, 300)], fill=GILT, width=2)

out = "static/og-image.jpg"
img.convert("RGB").save(out, "JPEG", quality=88, optimize=True, progressive=True)
print("saved", out)
