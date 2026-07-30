"""
SVG Natal Chart Generator
Draws a zodiac wheel with planet positions using pure Python math.
"""

import math
from typing import Dict, List, Tuple

# Zodiac signs in order (0 = Aries)
ZODIAC = ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
          "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"]

ZODIAC_SYMBOLS = ["♈", "♉", "♊", "♋", "♌", "♍", "♎", "♏", "♐", "♑", "♒", "♓"]

PLANET_SYMBOLS = {
    "Sun": "☉", "Moon": "☽", "Mercury": "☿", "Venus": "♀", "Mars": "♂",
    "Jupiter": "♃", "Saturn": "♄", "Uranus": "♅", "Neptune": "♆", "Pluto": "♇",
    "Asc": "AS", "MC": "MC", "True North Node": "☊", "True South Node": "☋",
    "Chiron": "⚷", "Part of Fortune": "⊕", "True Lilith": "⚸"
}

PLANET_COLORS = {
    "Sun": "#FFD700", "Moon": "#C0C0C0", "Mercury": "#FF8C00", "Venus": "#FF69B4",
    "Mars": "#FF4444", "Jupiter": "#FFA500", "Saturn": "#8B4513", "Uranus": "#00CED1",
    "Neptune": "#6A5ACD", "Pluto": "#800080", "Asc": "#FF0000", "MC": "#FF0000",
    "Chiron": "#90EE90"
}

ASPECT_COLORS = {
    "Conjunction": "#FFD700", "Semi-Sextile": "#AAA", "Semi-Square": "#AAA",
    "Sextile": "#00BFFF", "Quintile": "#AAA", "Square": "#FF4444",
    "Trine": "#00FF7F", "Sesquiquadrate": "#AAA", "Quincunx": "#AAA",
    "Opposition": "#FF6347"
}

def sign_degree_to_absolute(sign_name: str, sign_degree: str) -> float:
    """Convert 'Capricorn 17°19'57\"' to absolute degrees (0-360)."""
    sign_index = ZODIAC.index(sign_name)
    # Parse degree string (e.g., "17°19'57\"")
    parts = sign_degree.replace("°", " ").replace("'", " ").replace('"', " ").split()
    deg = float(parts[0]) + float(parts[1]) / 60 + float(parts[2]) / 3600
    return sign_index * 30 + deg

def generate_chart_svg(chart_data: dict, size: int = 500) -> str:
    """Generate an SVG natal chart wheel."""
    cx = cy = size / 2
    outer_r = size * 0.45
    inner_r = size * 0.32
    planet_r = size * 0.38
    center_r = size * 0.15

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">']
    svg.append(f'<rect width="{size}" height="{size}" fill="#0a0a1a"/>')

    # Background circle
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{outer_r}" fill="none" stroke="#2a2a4a" stroke-width="2"/>')
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{inner_r}" fill="none" stroke="#2a2a4a" stroke-width="1"/>')
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{center_r}" fill="#1a1a2e" stroke="#2a2a4a" stroke-width="1"/>')

    # House cusps (if available) or zodiac divisions
    for i in range(12):
        angle_deg = i * 30 - 90  # Start from ascendant (left side)
        angle = math.radians(angle_deg)

        # Sector lines
        x1 = cx + center_r * math.cos(angle)
        y1 = cy + center_r * math.sin(angle)
        x2 = cx + outer_r * math.cos(angle)
        y2 = cy + outer_r * math.sin(angle)
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#2a2a4a" stroke-width="1"/>')

        # Sign labels
        label_r = outer_r - 20
        lx = cx + label_r * math.cos(angle + math.radians(15))
        ly = cy + label_r * math.sin(angle + math.radians(15))
        svg.append(f'<text x="{lx}" y="{ly}" fill="#c9b1ff" font-size="10" text-anchor="middle" dominant-baseline="central">{ZODIAC_SYMBOLS[i]}</text>')

    # Planet positions
    objects = chart_data.get("objects", {})
    for oid, obj in objects.items():
        name = obj.get("name", "")
        sign = obj.get("sign", "")
        sign_lon = obj.get("sign_longitude", "")

        if not sign or not sign_lon:
            continue

        # Calculate absolute angle
        abs_deg = sign_degree_to_absolute(sign, sign_lon)

        # Convert to SVG angle (0 = 3 o'clock = right side in SVG, 0° Aries = left side/ascendant)
        # Aries 0° = 0° absolute = left side (-180° in SVG coordinates)
        svg_angle_deg = abs_deg - 90  # Rotate so Aries is at ascendant position (left)
        angle = math.radians(svg_angle_deg)

        symbol = PLANET_SYMBOLS.get(name, name[:2])
        color = PLANET_COLORS.get(name, "#c9b1ff")

        px = cx + planet_r * math.cos(angle)
        py = cy + planet_r * math.sin(angle)

        # Draw planet
        size_px = 8 if name in ["Sun", "Moon"] else 6 if name in ["Asc", "MC"] else 5
        svg.append(f'<circle cx="{px}" cy="{py}" r="{size_px}" fill="{color}" stroke="#0a0a1a" stroke-width="1"/>')
        svg.append(f'<text x="{px}" y="{py - 10}" fill="{color}" font-size="8" text-anchor="middle">{symbol}</text>')

    # Center info
    native = chart_data.get("native", {})
    svg.append(f'<text x="{cx}" y="{cy - 8}" fill="#c9b1ff" font-size="10" text-anchor="middle" font-weight="bold">{native.get("name", "")}</text>')
    svg.append(f'<text x="{cx}" y="{cy + 8}" fill="#888" font-size="8" text-anchor="middle">{native.get("datetime", "")}</text>')

    svg.append('</svg>')
    return '\n'.join(svg)
