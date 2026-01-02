"""Constants for the EON Energia integration."""

DOMAIN = "eon_energia"

# API Configuration
API_BASE_URL = "https://api-mmi.eon.it"
API_SUBSCRIPTION_KEY = "3ab3ed651f7142a8a039dc61d081fbe5"

# OAuth Configuration (Auth0)
AUTH_DOMAIN = "auth.eon-energia.com"
AUTH_CLIENT_ID = "cGfV5ddN6z7ezg48gfmiRNagTPp6tsVy"
AUTH_AUDIENCE = "https://api-mmi.eon.it"
AUTH_SCOPE = "openid profile email offline_access"
AUTH_TOKEN_URL = f"https://{AUTH_DOMAIN}/oauth/token"
AUTH_AUTHORIZE_URL = f"https://{AUTH_DOMAIN}/authorize"

# API Endpoints
ENDPOINT_DAILY_CONSUMPTION = "/DeeperConsumption/v1.0/ExtDailyConsumption"
ENDPOINT_ACCOUNTS = "/scsi/accounts/v1.0"
ENDPOINT_POINT_OF_DELIVERIES = "/scsi/point-of-deliveries/v1.0"

# Config keys
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_POD = "pod"  # Point of Delivery (PR code)
CONF_TOKEN_EXPIRES = "token_expires"
CONF_TARIFF_TYPE = "tariff_type"

# Tariff types
TARIFF_MONORARIA = "monoraria"
TARIFF_MULTIORARIA = "multioraria"

# Measurement types
MEASURE_TYPE_EA = "Ea"  # Active Energy
MEASURE_TYPE_ER = "Er"  # Reactive Energy

# Data granularity
GRANULARITY_HOURLY = "H"
GRANULARITY_DAILY = "D"
GRANULARITY_MONTHLY = "M"

# Update interval (in hours)
DEFAULT_SCAN_INTERVAL = 6

# Sensor types
SENSOR_ENERGY_CONSUMPTION = "energy_consumption"
SENSOR_ENERGY_DAILY = "energy_daily"

# Tariff bands (Fasce)
# F1: Peak hours (Mon-Fri 8:00-19:00)
# F2: Mid-peak hours (Mon-Fri 7:00-8:00, 19:00-23:00, Sat 7:00-23:00)
# F3: Off-peak hours (nights 23:00-7:00, Sundays, holidays)
FASCIA_F1 = "F1"
FASCIA_F2 = "F2"
FASCIA_F3 = "F3"
