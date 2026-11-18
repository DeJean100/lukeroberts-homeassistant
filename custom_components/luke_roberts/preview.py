"""SVG preview of a scene, drawn from its scene record (top view).

The 16 uplight segments are a soft aura around the lamp (indirect light,
so not drawn exactly), the 84 downlight spots (rings of 3, 6, ... 21 spots,
inner ring first) blurred dots on the dark lamp disc at the scene's colour
temperature. Round, with transparent corners. The orientation
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
LAMP_RADIUS = 0.29  # dark lamp disc, the uplight aura stays outside it
SPOT_RADIUS = 0.23  # outer downlight ring


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


def _opacity(level: int, scale: float = 1.0) -> str:
    """Perceived brightness: dim levels look brighter than linear (sqrt)."""
    return f"{math.sqrt(level / 255) * scale:.2f}"


def _angle(index: float, count: int) -> float:
    """Counter-clockwise from the top, in screen coordinates."""
    return -2 * math.pi * index / count - math.pi / 2


def _frame(body: str) -> bytes:
    """A black disc with transparent corners; everything is clipped to it."""
    center = SIZE / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" '
        f'viewBox="0 0 {SIZE} {SIZE}">'
        "<defs>"
        f'<clipPath id="disc"><circle cx="{center}" cy="{center}" r="{center}"/>'
        "</clipPath>"
        '<filter id="aura" x="-50%" y="-50%" width="200%" height="200%">'
        f'<feGaussianBlur stdDeviation="{SIZE * 0.04:.1f}"/></filter>'
        '<filter id="glow" x="-50%" y="-50%" width="200%" height="200%">'
        f'<feGaussianBlur stdDeviation="{SIZE * 0.028:.1f}"/></filter>'
        '<radialGradient id="lamp"><stop offset="0" stop-color="#151515"/>'
        '<stop offset=".85" stop-color="#070707"/>'
        '<stop offset="1" stop-color="#000"/></radialGradient>'
        "</defs>"
        f'<g clip-path="url(#disc)"><circle cx="{center}" cy="{center}" '
        f'r="{center}" fill="#000"/>{body}</g></svg>'
    ).encode()


def _lamp_disc() -> str:
    center = SIZE / 2
    return (
        f'<circle cx="{center}" cy="{center}" r="{SIZE * LAMP_RADIUS:.1f}" '
        'fill="url(#lamp)"/>'
    )


def message_svg(text: str) -> bytes:
    """The lamp disc with a short text, for scenes without a picture."""
    center = SIZE / 2
    rows = text.split("\n")[:2]
    lines = "".join(
        f'<text x="{center}" y="{center + 6 + 22 * (i - (len(rows) - 1) / 2):.1f}" '
        'fill="#999" font-family="sans-serif" font-size="18" text-anchor="middle">'
        f"{escape(line)}</text>"
        for i, line in enumerate(rows)
    )
    rim = (
        f'<circle cx="{center}" cy="{center}" r="{SIZE * 0.35:.1f}" fill="none" '
        f'stroke="#2a2a2a" stroke-width="{SIZE * 0.05:.1f}"/>'
    )
    return _frame(rim + _lamp_disc() + lines)


def scene_svg(record: SceneRecord) -> bytes:
    """Picture of a scene record; a text tile for other record types.

    The uplight is indirect light, so it is drawn as a soft aura of 16
    blurred colour blobs around the lamp; the downlight spots are drawn on
    the dark lamp disc, which the aura does not reach.
    """
    if record.scene_type == SceneType.NAME_ONLY:
        return message_svg("Off")
    if record.scene_type == SceneType.DYNAMIC:
        return message_svg(f"{record.name}\nanimated scene")
    if record.scene_type != SceneType.STATIC:
        return message_svg("No scene")
    raw = record.raw
    center = SIZE / 2

    # Uplight: 16 soft colour blobs around the lamp.
    blobs: list[str] = []
    for segment in range(UPLIGHT_SEGMENTS):
        offset = _UPLIGHT_OFFSET + 4 * segment
        red, green, blue = _uplight_rgb(
            raw[offset], struct.unpack_from("<H", raw, offset + 1)[0]
        )
        angle = _angle(segment + 0.5, UPLIGHT_SEGMENTS)
        blobs.append(
            f'<circle cx="{center + SIZE * 0.35 * math.cos(angle):.1f}" '
            f'cy="{center + SIZE * 0.35 * math.sin(angle):.1f}" '
            f'r="{SIZE * 0.085:.1f}" fill="rgb({red},{green},{blue})" '
            f'fill-opacity="{_opacity(raw[offset + 3], 0.95)}"/>'
        )

    # Downlight: 84 spots in 7 rings at the scene's colour temperature.
    kelvin = struct.unpack_from("<H", raw, SCENE_HEADER_LENGTH)[0]
    red, green, blue = kelvin_to_rgb(kelvin)
    spots: list[str] = []
    index = _DOWNLIGHT_OFFSET
    for ring, count in enumerate(DOWNLIGHT_RINGS, start=1):
        ring_radius = SIZE * SPOT_RADIUS * ring / len(DOWNLIGHT_RINGS)
        for spot in range(count):
            level = raw[index]
            index += 1
            if not level:
                continue
            angle = _angle(spot, count)
            spots.append(
                f'<circle cx="{center + ring_radius * math.cos(angle):.1f}" '
                f'cy="{center + ring_radius * math.sin(angle):.1f}" '
                f'r="{SIZE * 0.042:.1f}" fill="rgb({red},{green},{blue})" '
                f'fill-opacity="{_opacity(level)}"/>'
            )
    return _frame(
        f'<g filter="url(#aura)">{"".join(blobs)}</g>'
        + _lamp_disc()
        + f'<g filter="url(#glow)">{"".join(spots)}</g>'
    )
