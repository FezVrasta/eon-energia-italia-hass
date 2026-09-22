"""Constants for the E.ON Energia API."""

from __future__ import annotations

# Auth0 tenant behind E.ON's "Area Riservata".
AUTH_DOMAIN = "https://auth.eon-energia.com"

#: The native-app client. It allows custom redirect URIs, which is what makes the
#: manual authorisation flow possible. Password grants are explicitly disabled on it
#: (`unauthorized_client` for both `password` and `password-realm`), so the browser
#: flow is the only way in - do not try to reduce it to a single token POST.
AUTH_CLIENT_ID = "vEZ41cyr2pOHux9EKoN8dDgGb7UZc7EB"
AUTH_REDIRECT_URI = (
    "com.eon-energia.eon.auth0://auth.eon-energia.com/ios/com.eon-energia.eon/callback"
)
AUTH_SCOPE = "openid profile email offline_access"
AUTH_TOKEN_URL = f"{AUTH_DOMAIN}/oauth/token"
AUTH_AUTHORIZE_URL = f"{AUTH_DOMAIN}/authorize"

# API endpoints, relative to the base URL resolved at runtime.
ENDPOINT_DAILY_CONSUMPTION = "/DeeperConsumption/v1.0/ExtDailyConsumption"
ENDPOINT_MONTHLY_CONSUMPTION = "/DeeperConsumption/v1.0/ExtMonthlyConsumption"
ENDPOINT_ACCOUNTS = "/scsi/accounts/v1.0"
ENDPOINT_POINT_OF_DELIVERIES = "/scsi/point-of-deliveries/v1.0"
ENDPOINT_INVOICES = "/scsi/invoices/v1.0/getInvoiceDvc"
ENDPOINT_ENERGY_WALLET = "/energyWalletMyEon/v1.0/energyWallet"

# Measurement types.
MEASURE_TYPE_EA = "Ea"  # Active energy
MEASURE_TYPE_ER = "Er"  # Reactive energy

# Data granularity.
GRANULARITY_HOURLY = "H"
GRANULARITY_DAILY = "D"
GRANULARITY_MONTHLY = "M"

# Tariff types.
TARIFF_MONORARIA = "monoraria"
TARIFF_MULTIORARIA = "multioraria"

#: Tariff bands, as defined by ARERA.
#:
#: F1 peak      Mon-Fri 08:00-19:00
#: F2 mid-peak  Mon-Fri 07:00-08:00 and 19:00-23:00, Sat 07:00-23:00
#: F3 off-peak  nights 23:00-07:00, Sundays, and Italian national holidays
FASCIA_F1 = "F1"
FASCIA_F2 = "F2"
FASCIA_F3 = "F3"

#: Refresh this long before a token actually expires, to cover clock skew and the
#: round trip. Tokens live two hours, so the margin costs nothing.
TOKEN_REFRESH_MARGIN = 300
