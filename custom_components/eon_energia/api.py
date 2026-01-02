"""EON Energia API Client."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import aiohttp

from .const import (
    API_BASE_URL,
    API_SUBSCRIPTION_KEY,
    AUTH_CLIENT_ID,
    AUTH_TOKEN_URL,
    ENDPOINT_DAILY_CONSUMPTION,
    ENDPOINT_ACCOUNTS,
    ENDPOINT_POINT_OF_DELIVERIES,
    GRANULARITY_HOURLY,
    MEASURE_TYPE_EA,
)

_LOGGER = logging.getLogger(__name__)


class EONEnergiaApiError(Exception):
    """Base exception for EON Energia API errors."""


class EONEnergiaAuthError(EONEnergiaApiError):
    """Authentication error."""


class EONEnergiaTokenRefreshError(EONEnergiaApiError):
    """Token refresh error."""


class EONEnergiaApi:
    """EON Energia API client with OAuth token refresh support."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str | None = None,
        token_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        """Initialize the API client.

        Args:
            access_token: The current access token.
            refresh_token: The refresh token for automatic renewal.
            token_callback: Callback to notify when tokens are refreshed.
                           Called with (new_access_token, new_refresh_token).
        """
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_callback = token_callback
        self._session: aiohttp.ClientSession | None = None

    @property
    def access_token(self) -> str:
        """Return the current access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    @property
    def _headers(self) -> dict[str, str]:
        """Return headers for API requests."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "ocp-apim-subscription-key": API_SUBSCRIPTION_KEY,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the API session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token.

        Returns:
            True if refresh was successful, False otherwise.
        """
        if not self._refresh_token:
            _LOGGER.warning("No refresh token available")
            return False

        session = await self._get_session()

        try:
            async with session.post(
                AUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": AUTH_CLIENT_ID,
                    "refresh_token": self._refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(
                        "Token refresh failed with status %s: %s",
                        response.status,
                        text[:200],
                    )
                    return False

                data = await response.json()

                new_access_token = data.get("access_token")
                new_refresh_token = data.get("refresh_token")

                if not new_access_token:
                    _LOGGER.error("No access token in refresh response")
                    return False

                self._access_token = new_access_token
                # Auth0 uses refresh token rotation - always update with the new one
                if new_refresh_token:
                    self._refresh_token = new_refresh_token
                    _LOGGER.debug("Refresh token rotated")

                _LOGGER.info("Successfully refreshed access token")

                # Notify callback about new tokens to persist them
                if self._token_callback:
                    self._token_callback(
                        new_access_token,
                        new_refresh_token or self._refresh_token,
                    )

                return True

        except aiohttp.ClientError as err:
            _LOGGER.error("Token refresh connection error: %s", err)
            return False

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        retry_on_auth_error: bool = True,
    ) -> dict[str, Any]:
        """Make an API request with automatic token refresh."""
        session = await self._get_session()
        url = f"{API_BASE_URL}{endpoint}"

        try:
            async with session.request(
                method,
                url,
                headers=self._headers,
                json=data if method == "POST" else None,
            ) as response:
                if response.status == 401:
                    # Token expired - try to refresh
                    if retry_on_auth_error and self._refresh_token:
                        _LOGGER.info("Access token expired, attempting refresh")
                        if await self.refresh_access_token():
                            # Retry the request with the new token
                            return await self._request(
                                method, endpoint, data, retry_on_auth_error=False
                            )
                    raise EONEnergiaAuthError("Invalid or expired access token")

                # Get response text first for debugging
                text = await response.text()
                _LOGGER.debug(
                    "API response status: %s, content-type: %s",
                    response.status,
                    response.content_type,
                )

                if response.status != 200:
                    raise EONEnergiaApiError(
                        f"API request failed with status {response.status}: {text[:500]}"
                    )

                # Try to parse as JSON
                try:
                    import json

                    return json.loads(text)
                except json.JSONDecodeError as err:
                    _LOGGER.error("Failed to parse JSON response: %s", text[:500])
                    raise EONEnergiaApiError(f"Invalid JSON response: {err}") from err

        except aiohttp.ClientError as err:
            raise EONEnergiaApiError(f"Connection error: {err}") from err

    async def get_accounts(self) -> list[dict[str, Any]]:
        """Get user accounts."""
        return await self._request("GET", ENDPOINT_ACCOUNTS)

    async def get_points_of_delivery(self) -> list[dict[str, Any]]:
        """Get points of delivery (PODs)."""
        return await self._request("GET", ENDPOINT_POINT_OF_DELIVERIES)

    async def get_daily_consumption(
        self,
        pod: str,
        start_date: datetime,
        end_date: datetime,
        granularity: str = GRANULARITY_HOURLY,
        measure_type: str = MEASURE_TYPE_EA,
    ) -> dict[str, Any]:
        """
        Get energy consumption data.

        Args:
            pod: Point of Delivery code (PR number)
            start_date: Start date for the data
            end_date: End date for the data
            granularity: H (hourly), D (daily), or M (monthly)
            measure_type: Ea (active energy) or Er (reactive energy)

        Returns:
            Dictionary with consumption data
        """
        data = {
            "DataInizio": start_date.strftime("%Y-%m-%d"),
            "DataFine": end_date.strftime("%Y-%m-%d"),
            "PR": pod,
            "Type": granularity,
            "Misura": measure_type,
        }

        return await self._request("POST", ENDPOINT_DAILY_CONSUMPTION, data)

    async def get_today_consumption(
        self,
        pod: str,
        measure_type: str = MEASURE_TYPE_EA,
    ) -> dict[str, Any]:
        """Get today's energy consumption."""
        today = datetime.now()
        return await self.get_daily_consumption(
            pod=pod,
            start_date=today,
            end_date=today,
            granularity=GRANULARITY_HOURLY,
            measure_type=measure_type,
        )

    async def get_yesterday_consumption(
        self,
        pod: str,
        measure_type: str = MEASURE_TYPE_EA,
    ) -> dict[str, Any]:
        """Get yesterday's energy consumption (usually more complete data)."""
        yesterday = datetime.now() - timedelta(days=1)
        return await self.get_daily_consumption(
            pod=pod,
            start_date=yesterday,
            end_date=yesterday,
            granularity=GRANULARITY_HOURLY,
            measure_type=measure_type,
        )

    async def validate_token(self) -> bool:
        """Validate the access token by making a test request."""
        try:
            # Use point-of-deliveries as it's known to work
            await self.get_points_of_delivery()
            return True
        except EONEnergiaAuthError:
            _LOGGER.debug("Token validation failed: authentication error")
            return False
        except EONEnergiaApiError as err:
            _LOGGER.debug("Token validation error: %s", err)
            # Other errors might mean the token is valid but there's another issue
            return True

    def update_token(self, access_token: str, refresh_token: str | None = None) -> None:
        """Update the access token and optionally refresh token."""
        self._access_token = access_token
        if refresh_token:
            self._refresh_token = refresh_token

    def set_token_callback(
        self, callback: Callable[[str, str], None] | None
    ) -> None:
        """Set the callback for token refresh notifications."""
        self._token_callback = callback


async def exchange_code_for_tokens(
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Exchange authorization code for tokens.

    Args:
        code: The authorization code from the OAuth callback.
        code_verifier: The PKCE code verifier.
        redirect_uri: The redirect URI used in the authorization request.

    Returns:
        Dictionary containing access_token, refresh_token, etc.

    Raises:
        EONEnergiaAuthError: If the token exchange fails.
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                AUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": AUTH_CLIENT_ID,
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(
                        "Token exchange failed with status %s: %s",
                        response.status,
                        text[:200],
                    )
                    raise EONEnergiaAuthError(f"Token exchange failed: {text[:200]}")

                return await response.json()

        except aiohttp.ClientError as err:
            raise EONEnergiaAuthError(f"Token exchange connection error: {err}") from err
