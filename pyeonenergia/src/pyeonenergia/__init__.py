"""Async client for the E.ON Energia (Italy) customer API.

E.ON publish no public API. This talks to the same endpoints their own app and web
client use, with credentials the user supplies, to read that user's own consumption
and billing data.

    from pyeonenergia import EonEnergiaAuth, EonEnergiaClient

    tokens = await EonEnergiaAuth.exchange_code_for_tokens(code)
    client = EonEnergiaClient(tokens["access_token"], tokens["refresh_token"])
    pods = await client.get_points_of_delivery()

Two things about this API are worth knowing before debugging anything:

* it answers **HTTP 500** for an expired or missing token, never 401, so a status
  code alone cannot tell an auth problem from a server fault
* `getInvoiceDvc` answers **HTTP 202** with the full payload in the body

The client handles both. See `client.py` for the detail.
"""

from __future__ import annotations

from .auth import EonEnergiaAuth, build_authorization_url, extract_code_from_callback
from .client import EonEnergiaClient
from .config import clear_cache, get_api_config
from .const import (
    AUTH_AUTHORIZE_URL,
    AUTH_CLIENT_ID,
    AUTH_DOMAIN,
    AUTH_REDIRECT_URI,
    AUTH_SCOPE,
    AUTH_TOKEN_URL,
    FASCIA_F1,
    FASCIA_F2,
    FASCIA_F3,
    GRANULARITY_DAILY,
    GRANULARITY_HOURLY,
    GRANULARITY_MONTHLY,
    MEASURE_TYPE_EA,
    MEASURE_TYPE_ER,
    TARIFF_MONORARIA,
    TARIFF_MULTIORARIA,
)
from .exceptions import (
    EonEnergiaApiError,
    EonEnergiaAuthError,
    EonEnergiaCaptchaError,
    EonEnergiaConfigError,
    EonEnergiaError,
    EonEnergiaMfaRequiredError,
    EonEnergiaTokenRefreshError,
)
from .models import (
    Account,
    BillingProfile,
    HourlyConsumption,
    Invoice,
    PointOfDelivery,
)
from .tariff import (
    ITALY,
    easter_sunday,
    fascia_for_hour,
    hour_start_for_field,
    is_italian_holiday,
    local_hour_starts,
    parse_italian_date,
)

__version__ = "0.1.0"

__all__ = [
    "AUTH_AUTHORIZE_URL",
    "AUTH_CLIENT_ID",
    "AUTH_DOMAIN",
    "AUTH_REDIRECT_URI",
    "AUTH_SCOPE",
    "AUTH_TOKEN_URL",
    "FASCIA_F1",
    "FASCIA_F2",
    "FASCIA_F3",
    "GRANULARITY_DAILY",
    "GRANULARITY_HOURLY",
    "GRANULARITY_MONTHLY",
    "ITALY",
    "MEASURE_TYPE_EA",
    "MEASURE_TYPE_ER",
    "TARIFF_MONORARIA",
    "TARIFF_MULTIORARIA",
    "Account",
    "BillingProfile",
    "EonEnergiaApiError",
    "EonEnergiaAuth",
    "EonEnergiaAuthError",
    "EonEnergiaCaptchaError",
    "EonEnergiaClient",
    "EonEnergiaConfigError",
    "EonEnergiaError",
    "EonEnergiaMfaRequiredError",
    "EonEnergiaTokenRefreshError",
    "HourlyConsumption",
    "Invoice",
    "PointOfDelivery",
    "__version__",
    "build_authorization_url",
    "clear_cache",
    "easter_sunday",
    "extract_code_from_callback",
    "fascia_for_hour",
    "get_api_config",
    "hour_start_for_field",
    "is_italian_holiday",
    "local_hour_starts",
    "parse_italian_date",
]
