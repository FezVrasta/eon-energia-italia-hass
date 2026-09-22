"""Dynamic API configuration fetcher for EON Energia.

This module fetches the API base URL and subscription key dynamically
from the EON website to avoid exposing sensitive values in source code.

The website is behind Cloudflare bot protection, so the fetch fails for
plain HTTP clients more often than not. When it does, we serve the values
the official Android app ships hardcoded rather than failing the setup.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time

import aiohttp

from .exceptions import EonEnergiaConfigError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_LOGGER = logging.getLogger(__name__)

# EON website URLs
MYEON_BASE_URL = "https://myeon.eon-energia.com"
MYEON_LOGIN_URL = f"{MYEON_BASE_URL}/it/login.html"
# Direct config JS file - preferred source as it contains all config in one place
MYEON_CONFIG_JS_URL = f"{MYEON_BASE_URL}/content/eon-scsi/it/login.scsiconfig.js"

# Cache configuration (24 hours)
CONFIG_CACHE_TTL = 86400

# Fallback configuration, sealed.
#
# myeon.eon-energia.com sits behind Cloudflare bot protection, which answers
# plain HTTP clients with a 403 challenge page. When that happens we cannot
# scrape the values, so we fall back to a known-good pair.
#
# Those values are not written here in the clear. The blob below is AES-256-GCM
# with an scrypt-derived key; _unseal() opens it when the scrape fails.
#
# To be clear about what that buys: the passphrase is a constant a few lines
# down, in the same repository, because the integration has to open this
# unattended on someone else's machine. Anyone who can read this file can
# recover the plaintext. It keeps the values from being greppable in the tree
# and from being indexed; it does not make them secret.
#
# Regenerate with tools/seal_fallback.py; do not hand-edit the blob.
SEALED_FALLBACK = (
    "DANZnie8zvh0hfs1bUNYuxwnnMj6bOt/Ia3+NCAxwSxnsypaxeYaKAU0TLINpJIPnlsN+UmI"
    "bRf22nIES6nytX2ay3M9eVyjpLq18KaZnoZp2WXJLBghrYaueIQFfAtFO+v/p0gnaFqqI1k4"
    "bPQivXfbmGzin95Q63aewW3XvYK7t9hN/6GV9MJG"
)

#: Shared with tools/seal_fallback.py. Changing either side invalidates the blob.
_PASSPHRASE = b"eon_energia:api-config-fallback:v1"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LENGTH = 32
_SALT_LENGTH = 16
_NONCE_LENGTH = 12

#: Opened at most once per process; scrypt is deliberately slow.
_unsealed_fallback: dict[str, str] | None = None

# Shorter TTL when we are serving the fallback, so a recovered website is
# picked up the same day rather than 24h later.
FALLBACK_CACHE_TTL = 3600

# Module-level cache
_cached_config: dict[str, str] | None = None
_cache_timestamp: float = 0.0
_cache_lock: asyncio.Lock | None = None


def _unseal() -> dict[str, str]:
    """Open the sealed fallback configuration.

    AES-256-GCM, so a tampered or truncated blob raises rather than returning
    something that looks plausible. The result is cached because scrypt is slow
    by design and the answer never changes within a process.
    """
    global _unsealed_fallback

    if _unsealed_fallback is not None:
        return _unsealed_fallback

    raw = base64.b64decode(SEALED_FALLBACK)
    salt = raw[:_SALT_LENGTH]
    nonce = raw[_SALT_LENGTH:_SALT_LENGTH + _NONCE_LENGTH]
    ciphertext = raw[_SALT_LENGTH + _NONCE_LENGTH:]

    key = Scrypt(
        salt=salt, length=_KEY_LENGTH, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    ).derive(_PASSPHRASE)

    _unsealed_fallback = json.loads(AESGCM(key).decrypt(nonce, ciphertext, None))
    return _unsealed_fallback


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
        Dict with "base_url" and "subscription_key". Never raises: if the
        website cannot be scraped, the built-in fallback values are returned.
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

        _LOGGER.debug("Fetching API configuration from EON website")
        try:
            config = await _fetch_api_config(session)
            ttl_from = now
        except (EonEnergiaConfigError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            # The website is unreachable (Cloudflare challenge, outage, DNS).
            # This must not take the integration down: the values change very
            # rarely and we know what they are.
            _LOGGER.warning(
                "Could not fetch API configuration from the EON website (%s); "
                "using built-in fallback values",
                err,
            )
            config = dict(_unseal())
            # Expire sooner so we retry the website rather than pinning the
            # fallback for a full day.
            ttl_from = now - (CONFIG_CACHE_TTL - FALLBACK_CACHE_TTL)

        _cached_config = config
        _cache_timestamp = ttl_from

        return config


async def _fetch_api_config(
    session: aiohttp.ClientSession | None = None,
) -> dict[str, str]:
    """Fetch API configuration from the EON website.

    Only the dedicated config JS file is trusted. See the comment below for why
    the older "search the bundles for a hex string" approach was dropped.
    """
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

        # Deliberately NOT falling through to _scrape_api_config_from_html() here.
        #
        # That path searches every plausible JS bundle for a 32-hex key and takes
        # the first hit. The site defines several (crm.apim.key, public.apim.key,
        # api.login.subscription.key, backoffice.key, ...) and picking the wrong
        # one is not a visible failure: the API accepts the request and answers
        # HTTP 500. A known-good constant beats a confident guess, so let the
        # caller fall back instead.
        raise EonEnergiaConfigError(
            "Config JS unavailable (site is behind Cloudflare or the path moved)"
        )

        # Fallback: Fetch login page to get API base URL and find JS files
        async with session.get(
            MYEON_LOGIN_URL, headers=headers, allow_redirects=True
        ) as response:
            if response.status != 200:
                raise EonEnergiaConfigError(
                    f"Failed to fetch EON website: HTTP {response.status}"
                )
            html_content = await response.text()

        # Extract API base URL from HTML
        base_url = _extract_api_url_from_html(html_content)
        if not base_url:
            raise EonEnergiaConfigError("API base URL not found in login page")

        _LOGGER.debug("Extracted API base URL: %s", base_url)

        # Find JavaScript file URLs
        js_urls = _extract_js_urls(html_content)
        if not js_urls:
            raise EonEnergiaConfigError("No JavaScript files found on EON website")

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
            raise EonEnergiaConfigError(
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
        # Format: "apicrm.url":"https://<host>"
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
                url = match.group(1)
                # Handle escaped forward slashes (common in JSON: \/)
                url = url.replace("\\/", "/")
                # Handle Unicode escapes like \u002D for hyphen
                url = url.encode().decode("unicode_escape")
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
    # NOTE: every pattern is anchored on the left with (?<![\w.]) so that
    # "api.login.subscription.key" cannot satisfy a "subscription.key" pattern.
    # That aliasing returned the login key for CRM calls, which the API answers
    # with a bare HTTP 500 rather than an auth error.
    patterns = [
        # crm.apim.key is the one the /scsi endpoints actually want, so try first
        r'(?<![\w.])crm\.apim\.key\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        # subscription.key or subscription["key"]
        r'(?<![\w.])subscription\.key\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        r'(?<![\w.])subscription\[[\'"]key[\'"]\]\s*[:=]\s*["\']([a-f0-9]{32})["\']',
        # userSubscriptionKey
        r'(?<![\w.])userSubscriptionKey\s*[:=]\s*["\']([a-f0-9]{32})["\']',
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
