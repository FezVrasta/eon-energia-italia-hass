"""Config flow for EON Energia integration."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EONEnergiaApi, EONEnergiaApiError, EONEnergiaAuthError
from .const import (
    AUTH_AUDIENCE,
    AUTH_AUTHORIZE_URL,
    AUTH_CLIENT_ID,
    AUTH_SCOPE,
    AUTH_TOKEN_URL,
    CONF_ACCESS_TOKEN,
    CONF_POD,
    CONF_REFRESH_TOKEN,
    CONF_TARIFF_TYPE,
    DOMAIN,
    TARIFF_MONORARIA,
    TARIFF_MULTIORARIA,
)

_LOGGER = logging.getLogger(__name__)

# iOS app redirect URI - browser can't handle it but will show the code
REDIRECT_URI = "com.eon-energia.eon.auth0://auth.eon-energia.com/ios/com.eon-energia.eon/callback"


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge."""
    # Generate a random code verifier (43-128 chars, URL-safe)
    code_verifier = secrets.token_urlsafe(32)

    # Create code challenge using S256 method
    code_challenge_digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(code_challenge_digest).rstrip(b"=").decode()

    return code_verifier, code_challenge


def _generate_auth_url(code_challenge: str, state: str) -> str:
    """Generate the OAuth authorization URL."""
    return (
        f"{AUTH_AUTHORIZE_URL}"
        f"?client_id={AUTH_CLIENT_ID}"
        f"&audience={AUTH_AUDIENCE}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={AUTH_SCOPE.replace(' ', '%20')}"
        f"&response_type=code"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&state={state}"
        f"&prompt=login"
    )


def _extract_code_from_input(user_input: str) -> str | None:
    """Extract authorization code from user input (URL or just code)."""
    user_input = user_input.strip()

    # If it looks like a URL, try to extract the code parameter
    if user_input.startswith("com.eon-energia") or "code=" in user_input:
        # Try to parse as URL
        try:
            parsed = urlparse(user_input)
            query_params = parse_qs(parsed.query)
            if "code" in query_params:
                return query_params["code"][0]
        except Exception:
            pass

        # Try regex as fallback
        match = re.search(r"code=([^&]+)", user_input)
        if match:
            return match.group(1)

    # Return as-is if it doesn't look like a URL (assume it's just the code)
    return user_input if user_input else None


async def _exchange_code_for_tokens(code: str, code_verifier: str) -> dict[str, Any]:
    """Exchange authorization code for tokens."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(
            AUTH_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": AUTH_CLIENT_ID,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/json"},
        ) as response:
            if response.status != 200:
                text = await response.text()
                _LOGGER.error("Token exchange failed: %s - %s", response.status, text[:500])
                raise EONEnergiaAuthError(f"Token exchange failed: {text[:200]}")
            return await response.json()


class EONEnergiaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EON Energia."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._pods: list[dict[str, Any]] = []
        self._selected_pod: str | None = None
        self._reconfig_entry: ConfigEntry | None = None
        # OAuth PKCE state
        self._code_verifier: str | None = None
        self._state: str | None = None
        self._auth_url: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return EONEnergiaOptionsFlow()

    def _init_oauth(self) -> None:
        """Initialize OAuth parameters if not already done."""
        if self._code_verifier is None:
            self._code_verifier, code_challenge = _generate_pkce_pair()
            self._state = secrets.token_urlsafe(32)
            self._auth_url = _generate_auth_url(code_challenge, self._state)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - show login instructions and URL."""
        errors: dict[str, str] = {}

        # Generate PKCE parameters on first load
        self._init_oauth()

        if user_input is not None:
            callback_input = user_input.get("callback_url", "").strip()

            if not callback_input:
                errors["base"] = "no_code"
            else:
                # Extract code from URL or use as-is
                auth_code = _extract_code_from_input(callback_input)

                if not auth_code:
                    errors["base"] = "no_code"
                else:
                    # Exchange code for tokens
                    try:
                        tokens = await _exchange_code_for_tokens(auth_code, self._code_verifier)
                        self._access_token = tokens["access_token"]
                        self._refresh_token = tokens.get("refresh_token")

                        # Now fetch PODs
                        api = EONEnergiaApi(
                            access_token=self._access_token,
                            refresh_token=self._refresh_token,
                        )
                        try:
                            pods = await api.get_points_of_delivery()
                            if not pods:
                                errors["base"] = "no_pods"
                            else:
                                self._pods = pods
                                if len(pods) == 1:
                                    self._selected_pod = self._extract_pod_code(pods[0])
                                    return await self.async_step_select_tariff()
                                return await self.async_step_select_pod()
                        finally:
                            await api.close()

                    except EONEnergiaAuthError as err:
                        _LOGGER.error("Token exchange failed: %s", err)
                        errors["base"] = "invalid_auth"
                    except EONEnergiaApiError as err:
                        _LOGGER.error("API error: %s", err)
                        errors["base"] = "cannot_connect"
                    except Exception as err:
                        _LOGGER.exception("Unexpected error: %s", err)
                        errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("callback_url"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "auth_url": self._auth_url,
            },
        )

    async def async_step_select_pod(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle POD selection when multiple PODs exist."""
        if user_input is not None:
            self._selected_pod = user_input[CONF_POD]

            # Check if this POD is already configured
            await self.async_set_unique_id(self._selected_pod)
            self._abort_if_unique_id_configured()

            # Go to tariff selection
            return await self.async_step_select_tariff()

        # Build POD selection options
        pod_options = {
            self._extract_pod_code(pod): self._format_pod_label(pod)
            for pod in self._pods
        }

        return self.async_show_form(
            step_id="select_pod",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POD): vol.In(pod_options),
                }
            ),
        )

    async def async_step_select_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle tariff type selection."""
        if user_input is not None:
            tariff_type = user_input[CONF_TARIFF_TYPE]

            return self.async_create_entry(
                title=f"EON Energia ({self._selected_pod})",
                data={
                    CONF_ACCESS_TOKEN: self._access_token,
                    CONF_REFRESH_TOKEN: self._refresh_token,
                    CONF_POD: self._selected_pod,
                    CONF_TARIFF_TYPE: tariff_type,
                },
            )

        tariff_options = {
            TARIFF_MONORARIA: "Monoraria (tariffa unica)",
            TARIFF_MULTIORARIA: "Bioraria/Multioraria (F1, F2, F3)",
        }

        return self.async_show_form(
            step_id="select_tariff",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARIFF_TYPE, default=TARIFF_MULTIORARIA): vol.In(
                        tariff_options
                    ),
                }
            ),
        )

    def _extract_pod_code(self, pod: dict[str, Any]) -> str:
        """Extract POD code from API response."""
        # Try different possible field names (PODID is the main one from the API)
        for field in ["PODID", "podid", "PR", "pr", "POD", "pod", "code", "Code", "pointOfDelivery"]:
            if field in pod:
                return str(pod[field])
        # Fallback to first string value
        for value in pod.values():
            if isinstance(value, str) and len(value) > 5:
                return value
        return str(pod)

    def _format_pod_label(self, pod: dict[str, Any]) -> str:
        """Format POD for display in selection list."""
        code = self._extract_pod_code(pod)

        # Try to get address from DeliveryAddress object
        delivery_address = pod.get("DeliveryAddress", {})
        if delivery_address:
            street = delivery_address.get("Street", "")
            number = delivery_address.get("Number", "")
            city = delivery_address.get("City", "")
            if street and city:
                return f"{code} - {street} {number}, {city}"

        # Fallback to simple address fields
        address = pod.get("address", pod.get("Address", ""))
        if address:
            return f"{code} - {address}"
        return code

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration."""
        self._reconfig_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )

        if self._reconfig_entry is None:
            return self.async_abort(reason="reconfigure_failed")

        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration - OAuth login to update credentials."""
        errors: dict[str, str] = {}

        # Generate PKCE parameters on first load
        self._init_oauth()

        current_pod = self._reconfig_entry.data.get(CONF_POD, "Unknown")
        current_tariff = self._reconfig_entry.data.get(CONF_TARIFF_TYPE, TARIFF_MULTIORARIA)

        if user_input is not None:
            callback_input = user_input.get("callback_url", "").strip()
            tariff_type = user_input.get(CONF_TARIFF_TYPE, current_tariff)

            if not callback_input:
                errors["base"] = "no_code"
            else:
                # Extract code from URL or use as-is
                auth_code = _extract_code_from_input(callback_input)

                if not auth_code:
                    errors["base"] = "no_code"
                else:
                    # Exchange code for tokens
                    try:
                        tokens = await _exchange_code_for_tokens(auth_code, self._code_verifier)
                        access_token = tokens["access_token"]
                        refresh_token = tokens.get("refresh_token")

                        # Verify the POD is still accessible
                        api = EONEnergiaApi(
                            access_token=access_token,
                            refresh_token=refresh_token,
                        )
                        try:
                            pods = await api.get_points_of_delivery()
                            if not pods:
                                errors["base"] = "no_pods"
                            else:
                                pod_codes = [self._extract_pod_code(pod) for pod in pods]
                                if current_pod not in pod_codes:
                                    errors["base"] = "pod_not_found"
                                else:
                                    # Update the config entry
                                    return self.async_update_reload_and_abort(
                                        self._reconfig_entry,
                                        data={
                                            CONF_ACCESS_TOKEN: access_token,
                                            CONF_REFRESH_TOKEN: refresh_token,
                                            CONF_POD: current_pod,
                                            CONF_TARIFF_TYPE: tariff_type,
                                        },
                                    )
                        finally:
                            await api.close()

                    except EONEnergiaAuthError as err:
                        _LOGGER.error("Token exchange failed: %s", err)
                        errors["base"] = "invalid_auth"
                    except EONEnergiaApiError as err:
                        _LOGGER.error("API error: %s", err)
                        errors["base"] = "cannot_connect"
                    except Exception as err:
                        _LOGGER.exception("Unexpected error: %s", err)
                        errors["base"] = "cannot_connect"

        tariff_options = {
            TARIFF_MONORARIA: "Monoraria (tariffa unica)",
            TARIFF_MULTIORARIA: "Bioraria/Multioraria (F1, F2, F3)",
        }

        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("callback_url"): str,
                    vol.Required(CONF_TARIFF_TYPE, default=current_tariff): vol.In(
                        tariff_options
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "auth_url": self._auth_url,
                "pod": current_pod,
            },
        )


class EONEnergiaOptionsFlow(OptionsFlow):
    """Handle EON Energia options."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._code_verifier: str | None = None
        self._state: str | None = None
        self._auth_url: str | None = None

    def _init_oauth(self) -> None:
        """Initialize OAuth parameters if not already done."""
        if self._code_verifier is None:
            self._code_verifier, code_challenge = _generate_pkce_pair()
            self._state = secrets.token_urlsafe(32)
            self._auth_url = _generate_auth_url(code_challenge, self._state)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options - OAuth login to update credentials."""
        errors: dict[str, str] = {}

        # Generate PKCE parameters on first load
        self._init_oauth()

        current_pod = self.config_entry.data.get(CONF_POD, "Unknown")
        current_tariff = self.config_entry.data.get(CONF_TARIFF_TYPE, TARIFF_MULTIORARIA)

        if user_input is not None:
            callback_input = user_input.get("callback_url", "").strip()
            tariff_type = user_input.get(CONF_TARIFF_TYPE, current_tariff)

            # If no callback URL provided, just update tariff
            if not callback_input:
                # Only update tariff without re-authenticating
                new_data = {
                    **self.config_entry.data,
                    CONF_TARIFF_TYPE: tariff_type,
                }
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )
                return self.async_create_entry(title="", data={})

            # Extract code from URL or use as-is
            auth_code = _extract_code_from_input(callback_input)

            if not auth_code:
                errors["base"] = "no_code"
            else:
                # Exchange code for tokens
                try:
                    tokens = await _exchange_code_for_tokens(auth_code, self._code_verifier)
                    access_token = tokens["access_token"]
                    refresh_token = tokens.get("refresh_token")

                    # Verify the POD is still accessible
                    api = EONEnergiaApi(
                        access_token=access_token,
                        refresh_token=refresh_token,
                    )
                    try:
                        pods = await api.get_points_of_delivery()
                        if not pods:
                            errors["base"] = "no_pods"
                        else:
                            pod_codes = [self._extract_pod_code(pod) for pod in pods]
                            if current_pod not in pod_codes:
                                errors["base"] = "pod_not_found"
                            else:
                                # Update the config entry
                                new_data = {
                                    **self.config_entry.data,
                                    CONF_ACCESS_TOKEN: access_token,
                                    CONF_REFRESH_TOKEN: refresh_token,
                                    CONF_TARIFF_TYPE: tariff_type,
                                }
                                self.hass.config_entries.async_update_entry(
                                    self.config_entry,
                                    data=new_data,
                                )
                                return self.async_create_entry(title="", data={})
                    finally:
                        await api.close()

                except EONEnergiaAuthError as err:
                    _LOGGER.error("Token exchange failed: %s", err)
                    errors["base"] = "invalid_auth"
                except EONEnergiaApiError as err:
                    _LOGGER.error("API error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception("Unexpected error: %s", err)
                    errors["base"] = "cannot_connect"

        tariff_options = {
            TARIFF_MONORARIA: "Monoraria (tariffa unica)",
            TARIFF_MULTIORARIA: "Bioraria/Multioraria (F1, F2, F3)",
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("callback_url"): str,
                    vol.Required(CONF_TARIFF_TYPE, default=current_tariff): vol.In(
                        tariff_options
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "auth_url": self._auth_url,
                "pod": current_pod,
            },
        )

    def _extract_pod_code(self, pod: dict[str, Any]) -> str:
        """Extract POD code from API response."""
        for field in [
            "PODID",
            "podid",
            "PR",
            "pr",
            "POD",
            "pod",
            "code",
            "Code",
            "pointOfDelivery",
        ]:
            if field in pod:
                return str(pod[field])
        for value in pod.values():
            if isinstance(value, str) and len(value) > 5:
                return value
        return str(pod)
