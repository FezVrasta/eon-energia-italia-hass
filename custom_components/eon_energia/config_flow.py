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
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from .api import EONEnergiaApi, EONEnergiaApiError
from .auth import (
    EONAuth0Client,
    EONAuthError,
    EONMFARequiredError,
    build_authorization_url,
    extract_code_from_callback,
)
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
        self._mfa_session_data: dict[str, Any] | None = None
        self._tariff_type: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return EONEnergiaOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - username/password login."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "")

            if not username or not password:
                errors["base"] = "invalid_auth"
            else:
                try:
                    tokens = await EONAuth0Client.login(username, password)

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

                except EONMFARequiredError as err:
                    _LOGGER.info("MFA required for authentication")
                    self._mfa_session_data = err.session_data
                    return await self.async_step_mfa()
                except EONAuthError as err:
                    _LOGGER.error("Authentication failed: %s", err)
                    errors["base"] = "invalid_auth"
                    # Offer fallback for manual auth
                    return await self.async_step_auth_fallback()
                except EONEnergiaApiError as err:
                    _LOGGER.error("API error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception("Unexpected error: %s", err)
                    errors["base"] = "cannot_connect"
                    # Offer fallback for unexpected errors too
                    return await self.async_step_auth_fallback()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MFA code entry step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mfa_code = user_input.get("mfa_code", "").strip()

            if not mfa_code:
                errors["base"] = "invalid_mfa_code"
            else:
                try:
                    tokens = await EONAuth0Client.submit_mfa_code(
                        mfa_code, self._mfa_session_data
                    )

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

                except EONAuthError as err:
                    _LOGGER.error("MFA authentication failed: %s", err)
                    errors["base"] = "invalid_mfa_code"
                except EONEnergiaApiError as err:
                    _LOGGER.error("API error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception("Unexpected error: %s", err)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(
                {
                    vol.Required("mfa_code"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_auth_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle fallback manual authentication via callback URL."""
        errors: dict[str, str] = {}

        if user_input is not None:
            callback_url = user_input.get("callback_url", "").strip()

            if not callback_url:
                errors["base"] = "invalid_callback_url"
            else:
                # Extract code from callback URL
                code = extract_code_from_callback(callback_url)

                if not code:
                    errors["base"] = "invalid_callback_url"
                else:
                    try:
                        tokens = await EONAuth0Client.exchange_code_for_tokens(code)

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

                    except EONAuthError as err:
                        _LOGGER.error("Token exchange failed: %s", err)
                        errors["base"] = "invalid_callback_url"
                    except EONEnergiaApiError as err:
                        _LOGGER.error("API error: %s", err)
                        errors["base"] = "cannot_connect"
                    except Exception as err:
                        _LOGGER.exception("Unexpected error: %s", err)
                        errors["base"] = "cannot_connect"

        # Build authorization URL
        auth_url = build_authorization_url()

        return self.async_show_form(
            step_id="auth_fallback",
            data_schema=vol.Schema(
                {
                    vol.Required("callback_url"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "auth_url": auth_url,
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
        for field in ["PODID", "podid", "PR", "pr", "POD", "pod", "code", "Code", "pointOfDelivery"]:
            if field in pod:
                return str(pod[field])
        for value in pod.values():
            if isinstance(value, str) and len(value) > 5:
                return value
        return str(pod)

    def _format_pod_label(self, pod: dict[str, Any]) -> str:
        """Format POD for display in selection list."""
        code = self._extract_pod_code(pod)

        delivery_address = pod.get("DeliveryAddress", {})
        if delivery_address:
            street = delivery_address.get("Street", "")
            number = delivery_address.get("Number", "")
            city = delivery_address.get("City", "")
            if street and city:
                return f"{code} - {street} {number}, {city}"

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
        """Handle reconfiguration - login to update credentials."""
        errors: dict[str, str] = {}

        current_pod = self._reconfig_entry.data.get(CONF_POD, "Unknown")
        current_tariff = self._reconfig_entry.data.get(CONF_TARIFF_TYPE, TARIFF_MULTIORARIA)

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "")
            tariff_type = user_input.get(CONF_TARIFF_TYPE, current_tariff)

            if not username or not password:
                errors["base"] = "invalid_auth"
            else:
                try:
                    tokens = await EONAuth0Client.login(username, password)

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

                except EONMFARequiredError as err:
                    _LOGGER.info("MFA required for reconfiguration")
                    self._mfa_session_data = err.session_data
                    self._tariff_type = tariff_type
                    return await self.async_step_reconfigure_mfa()
                except EONAuthError as err:
                    _LOGGER.error("Authentication failed: %s", err)
                    errors["base"] = "invalid_auth"
                    # Offer fallback for manual auth
                    self._tariff_type = tariff_type
                    return await self.async_step_reconfigure_fallback()
                except EONEnergiaApiError as err:
                    _LOGGER.error("API error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception("Unexpected error: %s", err)
                    errors["base"] = "cannot_connect"
                    # Offer fallback for unexpected errors too
                    self._tariff_type = tariff_type
                    return await self.async_step_reconfigure_fallback()

        tariff_options = {
            TARIFF_MONORARIA: "Monoraria (tariffa unica)",
            TARIFF_MULTIORARIA: "Bioraria/Multioraria (F1, F2, F3)",
        }

        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_TARIFF_TYPE, default=current_tariff): vol.In(
                        tariff_options
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "pod": current_pod,
            },
        )

    async def async_step_reconfigure_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MFA code entry during reconfiguration."""
        errors: dict[str, str] = {}

        current_pod = self._reconfig_entry.data.get(CONF_POD, "Unknown")

        if user_input is not None:
            mfa_code = user_input.get("mfa_code", "").strip()

            if not mfa_code:
                errors["base"] = "invalid_mfa_code"
            else:
                try:
                    tokens = await EONAuth0Client.submit_mfa_code(
                        mfa_code, self._mfa_session_data
                    )

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
                                return self.async_update_reload_and_abort(
                                    self._reconfig_entry,
                                    data={
                                        CONF_ACCESS_TOKEN: access_token,
                                        CONF_REFRESH_TOKEN: refresh_token,
                                        CONF_POD: current_pod,
                                        CONF_TARIFF_TYPE: self._tariff_type,
                                    },
                                )
                    finally:
                        await api.close()

                except EONAuthError as err:
                    _LOGGER.error("MFA authentication failed: %s", err)
                    errors["base"] = "invalid_mfa_code"
                except EONEnergiaApiError as err:
                    _LOGGER.error("API error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception("Unexpected error: %s", err)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reconfigure_mfa",
            data_schema=vol.Schema(
                {
                    vol.Required("mfa_code"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle fallback manual authentication during reconfiguration."""
        errors: dict[str, str] = {}

        current_pod = self._reconfig_entry.data.get(CONF_POD, "Unknown")

        if user_input is not None:
            callback_url = user_input.get("callback_url", "").strip()

            if not callback_url:
                errors["base"] = "invalid_callback_url"
            else:
                # Extract code from callback URL
                code = extract_code_from_callback(callback_url)

                if not code:
                    errors["base"] = "invalid_callback_url"
                else:
                    try:
                        tokens = await EONAuth0Client.exchange_code_for_tokens(code)

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
                                    return self.async_update_reload_and_abort(
                                        self._reconfig_entry,
                                        data={
                                            CONF_ACCESS_TOKEN: access_token,
                                            CONF_REFRESH_TOKEN: refresh_token,
                                            CONF_POD: current_pod,
                                            CONF_TARIFF_TYPE: self._tariff_type,
                                        },
                                    )
                        finally:
                            await api.close()

                    except EONAuthError as err:
                        _LOGGER.error("Token exchange failed: %s", err)
                        errors["base"] = "invalid_callback_url"
                    except EONEnergiaApiError as err:
                        _LOGGER.error("API error: %s", err)
                        errors["base"] = "cannot_connect"
                    except Exception as err:
                        _LOGGER.exception("Unexpected error: %s", err)
                        errors["base"] = "cannot_connect"

        # Build authorization URL
        auth_url = build_authorization_url()

        return self.async_show_form(
            step_id="reconfigure_fallback",
            data_schema=vol.Schema(
                {
                    vol.Required("callback_url"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "auth_url": auth_url,
                "pod": current_pod,
            },
        )


class EONEnergiaOptionsFlow(OptionsFlow):
    """Handle EON Energia options."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._mfa_session_data: dict[str, Any] | None = None
        self._tariff_type: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options - update tariff or re-authenticate."""
        errors: dict[str, str] = {}

        current_pod = self.config_entry.data.get(CONF_POD, "Unknown")
        current_tariff = self.config_entry.data.get(CONF_TARIFF_TYPE, TARIFF_MULTIORARIA)

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "")
            tariff_type = user_input.get(CONF_TARIFF_TYPE, current_tariff)

            # If no credentials provided, just update tariff
            if not username and not password:
                new_data = {
                    **self.config_entry.data,
                    CONF_TARIFF_TYPE: tariff_type,
                }
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )
                return self.async_create_entry(title="", data={})

            # If credentials provided, re-authenticate
            if not username or not password:
                errors["base"] = "invalid_auth"
            else:
                try:
                    tokens = await EONAuth0Client.login(username, password)

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

                except EONMFARequiredError as err:
                    _LOGGER.info("MFA required for options re-authentication")
                    self._mfa_session_data = err.session_data
                    self._tariff_type = tariff_type
                    return await self.async_step_mfa()
                except EONAuthError as err:
                    _LOGGER.error("Authentication failed: %s", err)
                    errors["base"] = "invalid_auth"
                    # Offer fallback for manual auth
                    self._tariff_type = tariff_type
                    return await self.async_step_auth_fallback()
                except EONEnergiaApiError as err:
                    _LOGGER.error("API error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception("Unexpected error: %s", err)
                    errors["base"] = "cannot_connect"
                    # Offer fallback for unexpected errors too
                    self._tariff_type = tariff_type
                    return await self.async_step_auth_fallback()

        tariff_options = {
            TARIFF_MONORARIA: "Monoraria (tariffa unica)",
            TARIFF_MULTIORARIA: "Bioraria/Multioraria (F1, F2, F3)",
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_USERNAME): str,
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Required(CONF_TARIFF_TYPE, default=current_tariff): vol.In(
                        tariff_options
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "pod": current_pod,
            },
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MFA code entry in options flow."""
        errors: dict[str, str] = {}

        current_pod = self.config_entry.data.get(CONF_POD, "Unknown")

        if user_input is not None:
            mfa_code = user_input.get("mfa_code", "").strip()

            if not mfa_code:
                errors["base"] = "invalid_mfa_code"
            else:
                try:
                    tokens = await EONAuth0Client.submit_mfa_code(
                        mfa_code, self._mfa_session_data
                    )

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
                                new_data = {
                                    **self.config_entry.data,
                                    CONF_ACCESS_TOKEN: access_token,
                                    CONF_REFRESH_TOKEN: refresh_token,
                                    CONF_TARIFF_TYPE: self._tariff_type,
                                }
                                self.hass.config_entries.async_update_entry(
                                    self.config_entry,
                                    data=new_data,
                                )
                                return self.async_create_entry(title="", data={})
                    finally:
                        await api.close()

                except EONAuthError as err:
                    _LOGGER.error("MFA authentication failed: %s", err)
                    errors["base"] = "invalid_mfa_code"
                except EONEnergiaApiError as err:
                    _LOGGER.error("API error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception("Unexpected error: %s", err)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(
                {
                    vol.Required("mfa_code"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_auth_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle fallback manual authentication in options flow."""
        errors: dict[str, str] = {}

        current_pod = self.config_entry.data.get(CONF_POD, "Unknown")

        if user_input is not None:
            callback_url = user_input.get("callback_url", "").strip()

            if not callback_url:
                errors["base"] = "invalid_callback_url"
            else:
                # Extract code from callback URL
                code = extract_code_from_callback(callback_url)

                if not code:
                    errors["base"] = "invalid_callback_url"
                else:
                    try:
                        tokens = await EONAuth0Client.exchange_code_for_tokens(code)

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
                                    new_data = {
                                        **self.config_entry.data,
                                        CONF_ACCESS_TOKEN: access_token,
                                        CONF_REFRESH_TOKEN: refresh_token,
                                        CONF_TARIFF_TYPE: self._tariff_type,
                                    }
                                    self.hass.config_entries.async_update_entry(
                                        self.config_entry,
                                        data=new_data,
                                    )
                                    return self.async_create_entry(title="", data={})
                        finally:
                            await api.close()

                    except EONAuthError as err:
                        _LOGGER.error("Token exchange failed: %s", err)
                        errors["base"] = "invalid_callback_url"
                    except EONEnergiaApiError as err:
                        _LOGGER.error("API error: %s", err)
                        errors["base"] = "cannot_connect"
                    except Exception as err:
                        _LOGGER.exception("Unexpected error: %s", err)
                        errors["base"] = "cannot_connect"

        # Build authorization URL
        auth_url = build_authorization_url()

        return self.async_show_form(
            step_id="auth_fallback",
            data_schema=vol.Schema(
                {
                    vol.Required("callback_url"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "auth_url": auth_url,
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
