"""Register the bundled Lovelace cards as a frontend module.

Serves the card JS from the integration directory and auto-loads it on every
dashboard via ``add_extra_js_url``, so users get the cards without a manual
resource entry. Registration is process-wide and idempotent across entries.
"""

from __future__ import annotations

import logging

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE = f"/{DOMAIN}_frontend"
CARDS_JS = "comed-ev-cards.js"
_REGISTERED = f"{DOMAIN}_frontend_registered"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve and auto-load the card bundle. Safe to call for every entry."""
    if hass.data.get(_REGISTERED):
        return
    if hass.http is None:
        # No HTTP component (e.g. a minimal test harness); cards are optional.
        _LOGGER.debug("HTTP not available; skipping frontend card registration")
        return
    hass.data[_REGISTERED] = True

    integration = await async_get_integration(hass, DOMAIN)
    source = integration.file_path / "frontend" / CARDS_JS
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"{URL_BASE}/{CARDS_JS}", str(source), False)]
    )
    # Version query busts the browser cache when the integration updates.
    add_extra_js_url(hass, f"{URL_BASE}/{CARDS_JS}?v={integration.version}")
