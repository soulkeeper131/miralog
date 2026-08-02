# -*- coding: utf-8 -*-
"""
SVG Natal Chart Generator
Draws a zodiac wheel with house cusps, planet positions and aspect lines
using pure Python math.
"""

import math

# Zodiac signs in order (0 = Aries)
ZODIAC = ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
          "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"]

ZODIAC_SYMBOLS = ["♈", "♉", "♊", "♋", "♌", "♍", "♎", "♏", "♐", "♑", "♒", "♓"]

ZODIAC_BG = ["Овен", "Телец", "Близнаци", "Рак", "Лъв", "Дева",
             "Везни", "Скорпион", "Стрелец", "Козирог", "Водолей", "Риби"]

# Alternating background bands per element (fire/earth/air/water) for readability
ZODIAC_BAND_COLORS = ["#241a3a", "#1a2438", "#1a2438", "#241a3a",
                       "#241a3a", "#1a2438", "#1a2438", "#241a3a",
                       "#241a3a", "#1a2438", "#1a2438", "#241a3a"]

PLANET_SYMBOLS = {
    "Sun": "☉", "Moon": "☽", "Mercury": "☿", "Venus": "♀", "Mars": "♂",
    "Jupiter": "♃", "Saturn": "♄", "Uranus": "♅", "Neptune": "♆", "Pluto": "♇",
    "Asc": "AS", "Desc": "DS", "MC": "MC", "IC": "IC",
    "True North Node": "☊", "True South Node": "☋", "North Node": "☊", "South Node": "☋",
    "Chiron": "⚷", "Part of Fortune": "⊕", "True Lilith": "⚸", "Lilith": "⚸",
    "Vertex": "Vx",
}

PLANET_COLORS = {
    "Sun": "#FFD700", "Moon": "#C0C0C0", "Mercury": "#FF8C00", "Venus": "#FF69B4",
    "Mars": "#FF4444", "Jupiter": "#FFA500", "Saturn": "#8B4513", "Uranus": "#00CED1",
    "Neptune": "#6A5ACD", "Pluto": "#800080", "Asc": "#f87171", "Desc": "#f87171",
    "MC": "#60a5fa", "IC": "#60a5fa", "Chiron": "#90EE90",
}

ASPECT_COLORS = {
    "Conjunction": "#FFD700", "Semisextile": "#3a3a5a", "Semisquare": "#3a3a5a",
    "Sextile": "#00BFFF", "Quintile": "#3a3a5a", "Square": "#FF4444",
    "Trine": "#00FF7F", "Sesquisquare": "#3a3a5a", "Quincunx": "#3a3a5a",
    "Biquintile": "#3a3a5a", "Opposition": "#FF6347",
}

MAJOR_ASPECTS = {"Conjunction", "Sextile", "Square", "Trine", "Opposition"}

# Planets/points to connect with aspect lines (skip minor points to avoid clutter)
ASPECT_BODIES = {
    "Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn",
    "Uranus", "Neptune", "Pluto", "Asc", "MC",
}


def sign_degree_to_absolute(sign_name: str, sign_degree: str) -> float:
    """Convert 'Capricorn 17°19'57\"' to absolute degrees (0-360)."""
    sign_index = ZODIAC.index(sign_name)
    parts = sign_degree.replace("°", " ").replace("'", " ").replace('"', " ").split()
    deg = float(parts[0]) + float(parts[1]) / 60 + float(parts[2]) / 3600
    return sign_index * 30 + deg


def _polar(cx, cy, r, angle_deg):
    a = math.radians(angle_deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def generate_chart_svg(chart_data: dict, size: int = 640) -> str:
    """Generate an SVG natal chart wheel with houses, signs and aspects."""
    cx = cy = size / 2
    zodiac_outer_r = size * 0.47
    zodiac_inner_r = size * 0.42
    house_r = size * 0.40
    planet_r = size * 0.34
    aspect_r = size * 0.30
    center_r = size * 0.14

    objects = chart_data.get("objects", {})

    # Ascendant absolute degree defines the wheel's rotation (Asc always at 9 o'clock / left)
    asc_abs = 0.0
    for obj in objects.values():
        if obj.get("name") == "Asc":
            asc_abs = sign_degree_to_absolute(obj["sign"], obj["sign_longitude"])
            break

    def to_svg_angle(abs_deg: float) -> float:
        # 0 deg (Asc) sits at 180 (left/9 o'clock); degrees increase counter-clockwise
        return 180 - (abs_deg - asc_abs)

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
           f'width="{size}" height="{size}" font-family="system-ui, sans-serif">']
    svg.append(f'<rect width="{size}" height="{size}" fill="#0a0a1a"/>')

    # Zodiac ring background bands (one per sign, 30° each)
    for i in range(12):
        start_abs = i * 30
        a1 = to_svg_angle(start_abs)
        a2 = to_svg_angle(start_abs + 30)
        x1o, y1o = _polar(cx, cy, zodiac_outer_r, a1)
        x2o, y2o = _polar(cx, cy, zodiac_outer_r, a2)
        x1i, y1i = _polar(cx, cy, zodiac_inner_r, a1)
        x2i, y2i = _polar(cx, cy, zodiac_inner_r, a2)
        large_arc = 0
        svg.append(
            f'<path d="M {x1i} {y1i} L {x1o} {y1o} '
            f'A {zodiac_outer_r} {zodiac_outer_r} 0 {large_arc} 0 {x2o} {y2o} '
            f'L {x2i} {y2i} A {zodiac_inner_r} {zodiac_inner_r} 0 {large_arc} 1 {x1i} {y1i} Z" '
            f'fill="{ZODIAC_BAND_COLORS[i]}" stroke="#3a3a5a" stroke-width="0.5"/>'
        )
        # Sign symbol + BG name, centered in the band
        mid_a = to_svg_angle(start_abs + 15)
        sx, sy = _polar(cx, cy, (zodiac_outer_r + zodiac_inner_r) / 2, mid_a)
        svg.append(f'<text x="{sx}" y="{sy - 6}" fill="#c9b1ff" font-size="16" '
                    f'text-anchor="middle" dominant-baseline="central">{ZODIAC_SYMBOLS[i]}</text>')
        svg.append(f'<text x="{sx}" y="{sy + 10}" fill="#8b7fb8" font-size="8" '
                    f'text-anchor="middle" dominant-baseline="central">{ZODIAC_BG[i]}</text>')

    # Outer/inner boundary circles
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{zodiac_outer_r}" fill="none" stroke="#4a4a7a" stroke-width="1.5"/>')
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{zodiac_inner_r}" fill="none" stroke="#3a3a5a" stroke-width="1"/>')
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{aspect_r}" fill="none" stroke="#22223a" stroke-width="1"/>')
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="{center_r}" fill="#12121f" stroke="#2a2a4a" stroke-width="1"/>')

    # House cusps: use the real (e.g. Placidus) cusp longitudes when available,
    # otherwise fall back to equal 30° divisions from the ascendant.
    houses = chart_data.get("houses") or []
    if houses:
        cusp_longitudes = [h["longitude"] for h in houses]
    else:
        cusp_longitudes = [asc_abs + i * 30 for i in range(12)]

    for i in range(12):
        cusp_abs = cusp_longitudes[i]
        next_abs = cusp_longitudes[(i + 1) % 12]
        angle = to_svg_angle(cusp_abs)
        x1, y1 = _polar(cx, cy, center_r, angle)
        x2, y2 = _polar(cx, cy, zodiac_inner_r, angle)
        is_angle_cusp = i in (0, 3, 6, 9)  # Asc/IC/Desc/MC
        stroke = "#6a6a9a" if is_angle_cusp else "#2a2a4a"
        width = 1.5 if is_angle_cusp else 0.75
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{width}"/>')

        # Midpoint of this house's arc (handles wraparound past 360°)
        span = (next_abs - cusp_abs) % 360
        label_angle = to_svg_angle(cusp_abs + span / 2)
        lx, ly = _polar(cx, cy, house_r, label_angle)
        svg.append(f'<text x="{lx}" y="{ly}" fill="#555577" font-size="9" '
                    f'text-anchor="middle" dominant-baseline="central">{i + 1}</text>')

    # Aspect lines between major bodies
    aspects = chart_data.get("aspects", [])
    positions = {}
    for obj in objects.values():
        name = obj.get("name")
        sign = obj.get("sign")
        sign_lon = obj.get("sign_longitude")
        if name and sign and sign_lon:
            positions[name] = sign_degree_to_absolute(sign, sign_lon)

    seen_pairs = set()
    for a in aspects:
        active, passive, a_type = a.get("active"), a.get("passive"), a.get("type")
        if active not in ASPECT_BODIES or passive not in ASPECT_BODIES:
            continue
        if a_type not in MAJOR_ASPECTS:
            continue
        pair = tuple(sorted((active, passive)))
        if pair in seen_pairs or active not in positions or passive not in positions:
            continue
        seen_pairs.add(pair)
        angle1 = to_svg_angle(positions[active])
        angle2 = to_svg_angle(positions[passive])
        x1, y1 = _polar(cx, cy, aspect_r, angle1)
        x2, y2 = _polar(cx, cy, aspect_r, angle2)
        color = ASPECT_COLORS.get(a_type, "#333355")
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="0.75" opacity="0.7"/>')

    # Planet positions
    for oid, obj in objects.items():
        name = obj.get("name", "")
        sign = obj.get("sign", "")
        sign_lon = obj.get("sign_longitude", "")
        if not sign or not sign_lon:
            continue

        abs_deg = sign_degree_to_absolute(sign, sign_lon)
        angle = to_svg_angle(abs_deg)

        symbol = PLANET_SYMBOLS.get(name, (obj.get("name_bg") or name)[:2])
        color = PLANET_COLORS.get(name, "#c9b1ff")

        px, py = _polar(cx, cy, planet_r, angle)

        size_px = 9 if name in ("Sun", "Moon") else 7 if name in ("Asc", "MC", "Desc", "IC") else 5.5
        retro = obj.get("movement") == "Retrograde"
        svg.append(f'<circle cx="{px}" cy="{py}" r="{size_px}" fill="{color}" stroke="#0a0a1a" stroke-width="1.2"/>')
        svg.append(f'<text x="{px}" y="{py - size_px - 4}" fill="{color}" font-size="11" '
                    f'text-anchor="middle" font-weight="600">{symbol}{"℞" if retro else ""}</text>')

    # Center info
    native = chart_data.get("native", {})
    svg.append(f'<text x="{cx}" y="{cy - 8}" fill="#c9b1ff" font-size="12" text-anchor="middle" '
                f'font-weight="bold">{native.get("name", "")}</text>')
    svg.append(f'<text x="{cx}" y="{cy + 10}" fill="#888" font-size="9" text-anchor="middle">'
                f'{native.get("datetime", "")}</text>')

    svg.append('</svg>')
    return '\n'.join(svg)
