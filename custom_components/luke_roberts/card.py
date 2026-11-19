"""Serve the scene editor card and register its websocket commands.

The card ships with the integration: Home Assistant loads it on every
dashboard, so no separate download or resource entry is needed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from . import websocket_api
from .const import DOMAIN

CARD_FILE = Path(__file__).parent / "frontend" / "luke-roberts-scene-card.js"
CARD_URL = f"/{DOMAIN}/luke-roberts-scene-card.js"
_REGISTERED = f"{DOMAIN}_frontend_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Register once per Home Assistant run (not per lamp)."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    websocket_api.async_register(hass)
    if hass.http is None:
        return  # no web server (tests)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=False)]
    )
    if "frontend" not in hass.config.components:
        return
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

    # The content hash makes browsers load a changed card after an update.
    digest = await hass.async_add_executor_job(_digest)
    add_extra_js_url(hass, f"{CARD_URL}?v={digest}")


def _digest() -> str:
    return hashlib.sha256(CARD_FILE.read_bytes()).hexdigest()[:12]
