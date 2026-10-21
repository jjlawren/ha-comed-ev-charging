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
LOADER_JS = "comed-ev-loader.js"
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
    source = integration.file_path / "frontend"
    # Cache the files; the version query busts the cache when the integration
    # updates. The loader retries the card bundle if its fetch fails.
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(source), True)]
    )
    add_extra_js_url(hass, f"{URL_BASE}/{LOADER_JS}?v={integration.version}")
