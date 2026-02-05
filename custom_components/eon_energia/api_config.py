"""Dynamic API configuration fetcher for EON Energia.

This module fetches the API base URL and subscription key dynamically
from the EON website to avoid exposing sensitive values in source code.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import aiohttp

_LOGGER = logging.getLogger(__name__)

# EON website URLs
MYEON_BASE_URL = "https://myeon.eon-energia.com"
MYEON_LOGIN_URL = f"{MYEON_BASE_URL}/it/login.html"
# Direct config JS file - preferred source as it contains all config in one place
MYEON_CONFIG_JS_URL = f"{MYEON_BASE_URL}/content/eon-scsi/it/login.scsiconfig.js"

# Cache configuration (24 hours)
CONFIG_CACHE_TTL = 86400

# Module-level cache
_cached_config: dict[str, str] | None = None
_cache_timestamp: float = 0.0
_cache_lock: asyncio.Lock | None = None


class ApiConfigError(Exception):
    """Error fetching or extracting API configuration."""


def _get_cache_lock() -> asyncio.Lock:
    """Get or create the cache lock (must be called from async context)."""
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


async def get_api_config(
    session: aiohttp.ClientSession | None = None,
    force_refresh: bool = False,
) -> dict[str, str]:
    """Get the API configuration, using cache if available.

    Args:
        session: Optional aiohttp session to reuse. If None, creates a new one.
        force_refresh: If True, bypasses cache and fetches fresh config.

    Returns:
        Dict with "base_url" and "subscription_key".

    Raises:
        ApiConfigError: If the configuration cannot be fetched or extracted.
    """
    global _cached_config, _cache_timestamp

    lock = _get_cache_lock()
    async with lock:
        now = time.time()
        if (
            not force_refresh
            and _cached_config is not None
            and (now - _cache_timestamp) < CONFIG_CACHE_TTL
        ):
            _LOGGER.debug("Using cached API configuration")
            return _cached_config

        _LOGGER.info("Fetching API configuration from EON website")
        config = await _fetch_api_config(session)

        _cached_config = config
        _cache_timestamp = now

        return config


async def _fetch_api_config(
    session: aiohttp.ClientSession | None = None,
) -> dict[str, str]:
    """Fetch and extract API configuration from EON website."""
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        }

        # Try the direct config JS file first - it contains all config in one place
        config = await _try_fetch_from_config_js(session, headers)
        if config:
            return config

        _LOGGER.debug("Config JS not available, falling back to login page parsing")

        # Fallback: Fetch login page to get API base URL and find JS files
        async with session.get(
            MYEON_LOGIN_URL, headers=headers, allow_redirects=True
        ) as response:
            if response.status != 200:
                raise ApiConfigError(
                    f"Failed to fetch EON website: HTTP {response.status}"
                )
            html_content = await response.text()

        # Extract API base URL from HTML
        base_url = _extract_api_url_from_html(html_content)
        if not base_url:
            raise ApiConfigError("API base URL not found in login page")

        _LOGGER.debug("Extracted API base URL: %s", base_url)

        # Find JavaScript file URLs
        js_urls = _extract_js_urls(html_content)
        if not js_urls:
            raise ApiConfigError("No JavaScript files found on EON website")

        _LOGGER.debug("Found %d JavaScript files to search", len(js_urls))

        # Search JS files for subscription key
        subscription_key = None
        for js_url in js_urls:
            if not _is_likely_key_file(js_url):
                continue

            full_url = _build_full_url(js_url)
            _LOGGER.debug("Checking JS file: %s", js_url)

            try:
                async with session.get(full_url, headers=headers) as response:
                    if response.status != 200:
                        continue
                    js_content = await response.text()

                subscription_key = _extract_subscription_key_from_js(js_content)
                if subscription_key:
                    _LOGGER.info("Successfully extracted subscription key")
                    break

            except aiohttp.ClientError as err:
                _LOGGER.debug("Error fetching %s: %s", js_url, err)
                continue

        if not subscription_key:
            raise ApiConfigError(
                "Subscription key not found in any JavaScript file"
            )

        return {
            "base_url": base_url,
            "subscription_key": subscription_key,
        }

    finally:
        if close_session:
            await session.close()


async def _try_fetch_from_config_js(
    session: aiohttp.ClientSession,
    headers: dict[str, str],
) -> dict[str, str] | None:
    """Try to fetch config from the dedicated scsiconfig.js file.

    This file contains a clean JS object with all configuration values.
    Returns None if the file is not accessible.
    """
    try:
        async with session.get(MYEON_CONFIG_JS_URL, headers=headers) as response:
            if response.status != 200:
                _LOGGER.debug(
                    "Config JS file not accessible: HTTP %s", response.status
                )
                return None

            js_content = await response.text()

        # Extract from the env object in the config file
        # Format: "apicrm.url":"https://api-mmi.eon.it"
        base_url = None
        subscription_key = None

        # Look for apicrm.url or api.url for the base URL
        url_patterns = [
            r'"apicrm\.url"\s*:\s*"([^"]+)"',
            r'"api\.url"\s*:\s*"([^"]+)"',
        ]
        for pattern in url_patterns:
            match = re.search(pattern, js_content)
            if match:
                # Handle escaped characters like \u002D for hyphen
                url = match.group(1).encode().decode("unicode_escape")
                # api.url might have /scsi suffix, we want the base
                if url.endswith("/scsi"):
                    url = url[:-5]
                base_url = url
                break

        # Look for subscription.key or crm.apim.key
        key_patterns = [
            r'"subscription\.key"\s*:\s*"([a-f0-9]{32})"',
            r'"crm\.apim\.key"\s*:\s*"([a-f0-9]{32})"',
        ]
        for pattern in key_patterns:
            match = re.search(pattern, js_content)
            if match:
                subscription_key = match.group(1)
                break

        if base_url and subscription_key:
            _LOGGER.info("Successfully extracted config from scsiconfig.js")
            return {
                "base_url": base_url,
                "subscription_key": subscription_key,
            }

        _LOGGER.debug(
            "Config JS file found but missing values (base_url=%s, key=%s)",
            bool(base_url),
            bool(subscription_key),
        )
        return None

    except aiohttp.ClientError as err:
        _LOGGER.debug("Error fetching config JS: %s", err)
        return None


def _extract_api_url_from_html(html: str) -> str | None:
    """Extract API base URL from HTML content.

    Looks for patterns like:
    - "audience": "https://..."
    - api:{domain:"https://..."
    """
    patterns = [
        r'"audience"\s*:\s*"(https://[^"]+)"',
        r"api\s*:\s*\{\s*domain\s*:\s*\"(https://[^\"]+)\"",
        r'"api[_-]?base[_-]?url"\s*:\s*"(https://[^"]+)"',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            url = match.group(1)
            # Validate it looks like an API URL
            if "api" in url.lower() or "eon" in url.lower():
                return url

    return None


def _extract_js_urls(html: str) -> list[str]:
    """Extract JavaScript file URLs from HTML content."""
    script_pattern = r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']'
    urls = re.findall(script_pattern, html, re.IGNORECASE)
    return urls


def _is_likely_key_file(url: str) -> bool:
    """Check if a JS file URL is likely to contain the subscription key."""
    url_lower = url.lower()
    indicators = [
        "scsiconfig",
        "config",
        "clientlib",
        "main",
        "app",
        "vendor",
        "bundle",
    ]
    return any(indicator in url_lower for indicator in indicators)


def _build_full_url(url: str) -> str:
    """Build full URL from potentially relative path."""
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return f"{MYEON_BASE_URL}{url}"
    return f"{MYEON_BASE_URL}/{url}"


def _extract_subscription_key_from_js(js_content: str) -> str | None:
    """Extract subscription key from JavaScript content.

    Known patterns:
    - subscription.key: "..."
    - crm.apim.key: "..."
    - userSubscriptionKey: "..."
    """
    patterns = [
        # subscription.key or subscription["key"]
        r'subscription\.key\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        r'subscription\[[\'"]key[\'"]\]\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        # userSubscriptionKey
        r'userSubscriptionKey\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        # crm.apim.key
        r'crm\.apim\.key\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        # Generic patterns
        r'apim[._-]?subscription[._-]?key\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        r'ocp-apim-subscription-key\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        # Quoted key names
        r'"subscription\.key"\s*:\s*"([a-f0-9]{32})"',
        r'"crm\.apim\.key"\s*:\s*"([a-f0-9]{32})"',
    ]

    for pattern in patterns:
        match = re.search(pattern, js_content, re.IGNORECASE)
        if match:
            key = match.group(1)
            _LOGGER.debug("Found subscription key with pattern: %s", pattern[:40])
            return key

    return None


def clear_cache() -> None:
    """Clear the cached configuration (useful for testing or forced refresh)."""
    global _cached_config, _cache_timestamp
    _cached_config = None
    _cache_timestamp = 0.0
