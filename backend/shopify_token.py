"""Shopify access token manager with automatic refresh.

Dev Dashboard apps use client_credentials grant which returns short-lived
tokens (~24h). This module manages token lifecycle transparently.
"""

import logging
import threading
import time
from typing import Optional

import requests

from backend.config import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_current_token: Optional[str] = None
_token_expires_at: float = 0  # Unix timestamp


def get_shopify_token() -> str:
    """Get a valid Shopify access token, refreshing if needed.

    Falls back to the static SHOPIFY_ACCESS_TOKEN env var if
    client credentials are not configured.
    """
    global _current_token, _token_expires_at

    # If no client credentials, use static token (legacy flow)
    if not config.SHOPIFY_CLIENT_ID or not config.SHOPIFY_CLIENT_SECRET:
        return config.SHOPIFY_ACCESS_TOKEN

    # Check if current token is still valid (with 5-minute buffer)
    now = time.time()
    if _current_token and now < (_token_expires_at - 300):
        return _current_token

    # Need to refresh
    with _lock:
        # Double-check after acquiring lock
        now = time.time()
        if _current_token and now < (_token_expires_at - 300):
            return _current_token

        return _refresh_token()


def _refresh_token() -> str:
    """Exchange client credentials for a new access token."""
    global _current_token, _token_expires_at

    store_url = config.SHOPIFY_STORE_URL
    if not store_url:
        logger.error("SHOPIFY_STORE_URL not configured")
        return config.SHOPIFY_ACCESS_TOKEN

    # Normalize store URL: extract domain for the token endpoint
    # e.g. "https://sake-company.myshopify.com" -> domain used in URL
    token_url = f"{store_url}/admin/oauth/access_token"

    try:
        resp = requests.post(
            token_url,
            json={
                "client_id": config.SHOPIFY_CLIENT_ID,
                "client_secret": config.SHOPIFY_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        _current_token = data["access_token"]
        expires_in = data.get("expires_in", 86400)
        _token_expires_at = time.time() + expires_in

        logger.info(
            f"Shopify token refreshed, expires in {expires_in}s "
            f"(scopes: {data.get('scope', 'unknown')})"
        )
        return _current_token

    except Exception as e:
        logger.error(f"Failed to refresh Shopify token: {e}", exc_info=True)
        # Fall back to static token if refresh fails
        if config.SHOPIFY_ACCESS_TOKEN:
            logger.warning("Falling back to static SHOPIFY_ACCESS_TOKEN")
            return config.SHOPIFY_ACCESS_TOKEN
        raise
