"""SVG preview of a scene, drawn from its scene record (top view).

The outer ring shows the 16 uplight segments in their colour and brightness,
the blurred dots inside the 84 downlight spots (rings of 3, 6, ... 21 spots,
inner ring first) at the scene's colour temperature. The orientation
(counter-clockwise from the top) matches the Luke Roberts app for a captured
scene; colours are an approximation of the real light.

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

import colorsys
from html import escape
import math
import struct

from .protocol import SCENE_HEADER_LENGTH, SceneRecord, SceneType

SIZE = 300
UPLIGHT_SEGMENTS = 16
DOWNLIGHT_RINGS = (3, 6, 9, 12, 15, 18, 21)
_UPLIGHT_OFFSET = SCENE_HEADER_LENGTH + 2
_DOWNLIGHT_OFFSET = _UPLIGHT_OFFSET + 4 * UPLIGHT_SEGMENTS


def kelvin_to_rgb(kelvin: int) -> tuple[int, int, int]:
    """Approximate RGB of a black body (Tanner Helland's fit)."""
    temp = max(1000, min(40000, kelvin)) / 100
    if temp <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(temp) - 161.1195681661
        blue = (
            0.0 if temp <= 19 else 138.5177312231 * math.log(temp - 10) - 305.0447927307
        )
    else:
        red = 329.698727446 * (temp - 60) ** -0.1332047592
        green = 288.1221695283 * (temp - 60) ** -0.0755148492
        blue = 255.0
    return tuple(max(0, min(255, round(v))) for v in (red, green, blue))  # type: ignore[return-value]


def _uplight_rgb(saturation: int, value: int) -> tuple[int, int, int]:
    if saturation == 0:
        return kelvin_to_rgb(value)  # white uplight: the value is Kelvin
    red, green, blue = colorsys.hsv_to_rgb(value / 65535, saturation / 255, 1.0)
    return round(red * 255), round(green * 255), round(blue * 255)


def _opacity(level: int) -> str:
    """Perceived brightness: dim levels look brighter than linear (sqrt)."""
    return f"{math.sqrt(level / 255):.2f}"


def _angle(index: int, count: int) -> float:
    """Counter-clockwise from the top, in screen coordinates."""
    return -2 * math.pi * index / count - math.pi / 2


def _frame(body: str) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" '
        f'viewBox="0 0 {SIZE} {SIZE}">'
        '<defs><filter id="glow" x="-50%" y="-50%" width="200%" height="200%">'
        '<feGaussianBlur stdDeviation="9"/></filter></defs>'
        f'<rect width="{SIZE}" height="{SIZE}" fill="#000"/>{body}</svg>'
    ).encode()


def message_svg(text: str) -> bytes:
    """A dark tile with a short text, for scenes without a picture."""
    center = SIZE / 2
    lines = "".join(
        f'<text x="{center}" y="{center + 22 * (i - 0.5)}" fill="#aaa" '
        f'font-family="sans-serif" font-size="17" text-anchor="middle">'
        f"{escape(line)}</text>"
        for i, line in enumerate(text.split("\n")[:2])
    )
    ring = (
        f'<circle cx="{center}" cy="{center}" r="{SIZE * 0.44}" fill="none" '
        'stroke="#333" stroke-width="3"/>'
    )
    return _frame(ring + lines)


def scene_svg(record: SceneRecord) -> bytes:
    """Picture of a scene record; a text tile for other record types."""
    if record.scene_type == SceneType.NAME_ONLY:
        return message_svg("Off")
    if record.scene_type == SceneType.DYNAMIC:
        return message_svg(f"{record.name}\nanimated scene")
    if record.scene_type != SceneType.STATIC:
        return message_svg("No scene")
    raw = record.raw
    center = SIZE / 2
    parts: list[str] = []

    # Uplight: 16 arcs on the outer ring.
    radius = SIZE * 0.44
    for segment in range(UPLIGHT_SEGMENTS):
        offset = _UPLIGHT_OFFSET + 4 * segment
        saturation = raw[offset]
        value = struct.unpack_from("<H", raw, offset + 1)[0]
        brightness = raw[offset + 3]
        start = _angle(segment, UPLIGHT_SEGMENTS)
        end = _angle(segment + 1, UPLIGHT_SEGMENTS)
        x0, y0 = center + radius * math.cos(start), center + radius * math.sin(start)
        x1, y1 = center + radius * math.cos(end), center + radius * math.sin(end)
        red, green, blue = _uplight_rgb(saturation, value)
        parts.append(
            f'<path d="M{x0:.1f},{y0:.1f} A{radius:.1f},{radius:.1f} 0 0 0 '
            f'{x1:.1f},{y1:.1f}" fill="none" stroke="rgb({red},{green},{blue})" '
            f'stroke-opacity="{_opacity(brightness)}" stroke-width="{SIZE * 0.07:.1f}"/>'
        )

    # Downlight: 84 spots in 7 rings at the scene's colour temperature.
    kelvin = struct.unpack_from("<H", raw, SCENE_HEADER_LENGTH)[0]
    red, green, blue = kelvin_to_rgb(kelvin)
    spots: list[str] = []
    index = _DOWNLIGHT_OFFSET
    for ring, count in enumerate(DOWNLIGHT_RINGS, start=1):
        ring_radius = SIZE * 0.34 * ring / len(DOWNLIGHT_RINGS)
        for spot in range(count):
            level = raw[index]
            index += 1
            if not level:
                continue
            angle = _angle(spot, count)
            spots.append(
                f'<circle cx="{center + ring_radius * math.cos(angle):.1f}" '
                f'cy="{center + ring_radius * math.sin(angle):.1f}" '
                f'r="{SIZE * 0.05:.1f}" fill="rgb({red},{green},{blue})" '
                f'fill-opacity="{_opacity(level)}"/>'
            )
    parts.append(f'<g filter="url(#glow)">{"".join(spots)}</g>')
    return _frame("".join(parts))
