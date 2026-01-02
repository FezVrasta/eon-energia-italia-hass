"""Config flow for EON Energia integration."""

from __future__ import annotations

import logging
from typing import Any

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
    CONF_ACCESS_TOKEN,
    CONF_POD,
    CONF_REFRESH_TOKEN,
    CONF_TARIFF_TYPE,
    DOMAIN,
    TARIFF_MONORARIA,
    TARIFF_MULTIORARIA,
)

_LOGGER = logging.getLogger(__name__)


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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return EONEnergiaOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - enter refresh token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            refresh_token = user_input[CONF_REFRESH_TOKEN].strip()

            # Exchange refresh token for access token
            # We immediately rotate the token to ensure the stored token is unique
            # and won't conflict with any token still in use in the user's browser
            api = EONEnergiaApi(
                access_token="",  # Will be populated by refresh
                refresh_token=refresh_token,
            )
            try:
                # Try to refresh to get a valid access token and rotate the refresh token
                _LOGGER.debug("Attempting to exchange refresh token for new tokens")
                if not await api.refresh_access_token():
                    errors["base"] = "invalid_refresh_token"
                else:
                    # Verify we got a rotated refresh token
                    if api.refresh_token == refresh_token:
                        _LOGGER.warning(
                            "Auth server did not rotate refresh token - "
                            "this may cause issues if the same token is used elsewhere"
                        )
                    else:
                        _LOGGER.debug(
                            "Refresh token successfully rotated - storing new token"
                        )

                    # Now fetch PODs with the new access token
                    _LOGGER.debug("Fetching PODs with refreshed token")
                    pods = await api.get_points_of_delivery()
                    _LOGGER.debug("Received %d PODs", len(pods) if pods else 0)

                    if not pods:
                        errors["base"] = "no_pods"
                    else:
                        self._access_token = api.access_token
                        # Store the rotated refresh token (not the user-provided one)
                        self._refresh_token = api.refresh_token
                        self._pods = pods

                        # If only one POD, use it directly and go to tariff selection
                        if len(pods) == 1:
                            self._selected_pod = self._extract_pod_code(pods[0])
                            return await self.async_step_select_tariff()

                        # Multiple PODs - let user choose
                        return await self.async_step_select_pod()

            except EONEnergiaAuthError as err:
                _LOGGER.error("Authentication error: %s", err)
                errors["base"] = "invalid_auth"
            except EONEnergiaApiError as err:
                _LOGGER.error("API error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error during setup: %s", err)
                errors["base"] = "cannot_connect"
            finally:
                await api.close()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REFRESH_TOKEN): str,
                }
            ),
            errors=errors,
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
        """Handle reconfiguration confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            refresh_token = user_input[CONF_REFRESH_TOKEN].strip()
            tariff_type = user_input[CONF_TARIFF_TYPE]

            # Validate and rotate the refresh token
            # This ensures we store a unique token that won't conflict with the browser
            api = EONEnergiaApi(
                access_token="",
                refresh_token=refresh_token,
            )
            try:
                if not await api.refresh_access_token():
                    errors["base"] = "invalid_refresh_token"
                else:
                    # Log token rotation status
                    if api.refresh_token != refresh_token:
                        _LOGGER.debug("Refresh token successfully rotated during reconfigure")

                    pods = await api.get_points_of_delivery()

                    if not pods:
                        errors["base"] = "no_pods"
                    else:
                        # Verify the configured POD is still accessible
                        pod_codes = [self._extract_pod_code(pod) for pod in pods]
                        current_pod = self._reconfig_entry.data[CONF_POD]

                        if current_pod not in pod_codes:
                            errors["base"] = "pod_not_found"
                        else:
                            # Update the config entry with rotated token
                            return self.async_update_reload_and_abort(
                                self._reconfig_entry,
                                data={
                                    CONF_ACCESS_TOKEN: api.access_token,
                                    CONF_REFRESH_TOKEN: api.refresh_token,
                                    CONF_POD: current_pod,
                                    CONF_TARIFF_TYPE: tariff_type,
                                },
                            )

            except EONEnergiaAuthError:
                errors["base"] = "invalid_auth"
            except EONEnergiaApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reconfiguration")
                errors["base"] = "cannot_connect"
            finally:
                await api.close()

        # Get current values for defaults
        current_refresh_token = self._reconfig_entry.data.get(CONF_REFRESH_TOKEN, "")
        current_tariff = self._reconfig_entry.data.get(
            CONF_TARIFF_TYPE, TARIFF_MULTIORARIA
        )

        tariff_options = {
            TARIFF_MONORARIA: "Monoraria (tariffa unica)",
            TARIFF_MULTIORARIA: "Bioraria/Multioraria (F1, F2, F3)",
        }

        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REFRESH_TOKEN, default=current_refresh_token): str,
                    vol.Required(CONF_TARIFF_TYPE, default=current_tariff): vol.In(
                        tariff_options
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "pod": self._reconfig_entry.data.get(CONF_POD, "Unknown"),
            },
        )


class EONEnergiaOptionsFlow(OptionsFlow):
    """Handle EON Energia options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            refresh_token = user_input[CONF_REFRESH_TOKEN].strip()
            tariff_type = user_input[CONF_TARIFF_TYPE]

            # Validate the token if it changed
            current_refresh_token = self.config_entry.data.get(CONF_REFRESH_TOKEN, "")

            new_access_token = self.config_entry.data.get(CONF_ACCESS_TOKEN, "")
            new_refresh_token = refresh_token

            if refresh_token != current_refresh_token:
                # Validate and rotate the new refresh token
                api = EONEnergiaApi(
                    access_token="",
                    refresh_token=refresh_token,
                )
                try:
                    if not await api.refresh_access_token():
                        errors["base"] = "invalid_refresh_token"
                    else:
                        # Log token rotation status
                        if api.refresh_token != refresh_token:
                            _LOGGER.debug("Refresh token successfully rotated during options update")

                        pods = await api.get_points_of_delivery()

                        if not pods:
                            errors["base"] = "no_pods"
                        else:
                            # Verify the configured POD is still accessible
                            pod_codes = [self._extract_pod_code(pod) for pod in pods]
                            current_pod = self.config_entry.data[CONF_POD]

                            if current_pod not in pod_codes:
                                errors["base"] = "pod_not_found"
                            else:
                                new_access_token = api.access_token
                                # Store rotated token
                                new_refresh_token = api.refresh_token

                except EONEnergiaAuthError:
                    errors["base"] = "invalid_auth"
                except EONEnergiaApiError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during options update")
                    errors["base"] = "cannot_connect"
                finally:
                    await api.close()

            if not errors:
                # Update both data and options
                new_data = {
                    **self.config_entry.data,
                    CONF_ACCESS_TOKEN: new_access_token,
                    CONF_REFRESH_TOKEN: new_refresh_token,
                    CONF_TARIFF_TYPE: tariff_type,
                }
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )
                return self.async_create_entry(title="", data={})

        # Get current values
        current_refresh_token = self.config_entry.data.get(CONF_REFRESH_TOKEN, "")
        current_tariff = self.config_entry.data.get(
            CONF_TARIFF_TYPE, TARIFF_MULTIORARIA
        )

        tariff_options = {
            TARIFF_MONORARIA: "Monoraria (tariffa unica)",
            TARIFF_MULTIORARIA: "Bioraria/Multioraria (F1, F2, F3)",
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REFRESH_TOKEN, default=current_refresh_token): str,
                    vol.Required(CONF_TARIFF_TYPE, default=current_tariff): vol.In(
                        tariff_options
                    ),
                }
            ),
            errors=errors,
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
