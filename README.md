# EON Energia Italia - Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/fezvrasta/eon-energia-italia-hass.svg)](https://github.com/fezvrasta/eon-energia-italia-hass/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A custom Home Assistant integration for monitoring electricity consumption from EON Energia (Italy).

## Features

- **Automatic Token Refresh**: Uses OAuth refresh tokens - no need to manually update credentials
- **Automatic Statistics Import**: Hourly consumption data is automatically imported to HA statistics
- **Energy Dashboard Ready**: Works directly with the Home Assistant Energy Dashboard
- **Cumulative Energy Sensors**: Track total consumption with persistent state across restarts
- **Daily Energy Consumption Sensor**: Total daily kWh consumption
- **Last Hourly Reading Sensor**: Most recent hourly reading with timestamp
- **Hourly Breakdown**: All 24 hourly readings available as sensor attributes
- **Historical Data Import**: Import up to 365 days of historical consumption data via service
- **Tariff Support**: Choose between Monoraria (single rate) or Bioraria/Multioraria (F1, F2, F3)
- **Token Status Sensor**: Monitor the health of your API connection
- **Italian and English translations**

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots menu in the top right corner
4. Select "Custom repositories"
5. Add `https://github.com/fezvrasta/eon-energia-italia-hass` as a custom repository (Category: Integration)
6. Search for "EON Energia Italia" and install
7. Restart Home Assistant

### Manual Installation

1. Download the latest release from [GitHub Releases](https://github.com/fezvrasta/eon-energia-italia-hass/releases)
2. Copy the `custom_components/eon_energia` folder to your Home Assistant `custom_components` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "EON Energia"
3. Enter your refresh token (see below how to obtain it)
4. Select your electricity meter (POD) if you have multiple
5. Choose your tariff type (Monoraria or Bioraria/Multioraria)

## Obtaining the Refresh Token

The integration uses OAuth refresh tokens for authentication. The refresh token allows the integration to automatically renew access tokens, so you won't need to manually update credentials.

**How to obtain it:**

1. Open your browser and go to [myeon.eon-energia.com](https://myeon.eon-energia.com)
2. Log in with your credentials
3. Open Developer Tools (F12 or right-click → Inspect)
4. Go to the **Network** tab
5. Refresh the page
6. Look for a request to `https://auth.eon-energia.com/oauth/token`
7. Click on it and check the **Response** tab
8. Find the response with `scope: openid profile email offline_access`
9. Copy the `refresh_token` value

> **Note**: The refresh token is long-lived and the integration will automatically handle token renewal. You should only need to update it if you log out of your EON account or revoke the token.

## Sensors

### Cumulative Energy
- **Entity ID**: `sensor.eon_energia_PODID_cumulative_energy`
- **Unit**: kWh
- **State Class**: `total`
- **Description**: Running total of energy consumption since integration setup. Persists across restarts.
- **Attributes**:
  - `pod`: Your POD code
  - `last_processed_date`: Last date that was added to the total
  - `statistic_id`: The external statistic ID for Energy Dashboard

For Bioraria/Multioraria tariffs, additional cumulative sensors are created:
- `sensor.eon_energia_PODID_cumulative_energy_peak_f1` - Peak hours (F1)
- `sensor.eon_energia_PODID_cumulative_energy_mid_peak_f2` - Mid-peak hours (F2)
- `sensor.eon_energia_PODID_cumulative_energy_off_peak_f3` - Off-peak hours (F3)

### Daily Consumption
- **Entity ID**: `sensor.eon_energia_PODID_daily_consumption`
- **Unit**: kWh
- **State Class**: `total_increasing`
- **Description**: Total consumption for the most recent available day.
- **Attributes**:
  - `pod`: Your POD code
  - `data_date`: Date of the readings
  - `hourly_breakdown`: Dictionary with all 24 hourly readings

### Last Hourly Reading
- **Entity ID**: `sensor.eon_energia_PODID_last_reading`
- **Unit**: kWh
- **Description**: Most recent hourly reading value.
- **Attributes**:
  - `reading_hour`: Hour of the reading (e.g., "14:00")
  - `reading_date`: Date of the reading

### Token Status
- **Entity ID**: `sensor.eon_energia_PODID_token_status`
- **Category**: Diagnostic
- **Description**: Shows the current status of the API connection (`valid`, `invalid`, or `unknown`).
- **Attributes**:
  - `pod`: Your POD code
  - `last_error`: Error message if the last update failed

## Services

### Import Historical Statistics

Import historical energy consumption data into Home Assistant statistics for use in the Energy Dashboard.

> **Note**: New data is automatically imported every 6 hours. This service is only needed to backfill historical data when you first set up the integration.

**Service**: `eon_energia.import_statistics`

**Parameters**:
- `days` (optional): Number of days to import (1-365, default: 90)

**Example**:
```yaml
service: eon_energia.import_statistics
data:
  days: 90
```

**External Statistics** (for Energy Dashboard):
- **Total Consumption**: `eon_energia:{POD}_consumption`
- **F1 Peak Hours**: `eon_energia:{POD}_consumption_f1` (Bioraria/Multioraria only)
- **F2 Mid-Peak Hours**: `eon_energia:{POD}_consumption_f2` (Bioraria/Multioraria only)
- **F3 Off-Peak Hours**: `eon_energia:{POD}_consumption_f3` (Bioraria/Multioraria only)

## Tariff Types

### Monoraria
Single tariff rate - same price all day. Only total consumption statistics are created.

### Bioraria/Multioraria
Multiple tariff rates with different prices based on time of day:
- **F1 (Peak)**: Monday-Friday 8:00-19:00
- **F2 (Mid-Peak)**: Monday-Friday 7:00-8:00 and 19:00-23:00, Saturday 7:00-23:00
- **F3 (Off-Peak)**: Nights 23:00-7:00, Sundays, and holidays

## Energy Dashboard

To add EON Energia consumption to the Energy Dashboard:

1. Go to **Settings** → **Dashboards** → **Energy**
2. Click **Add Consumption**
3. Search for "eon" and select the consumption statistic you want to track
4. Save

## API Information

This integration uses the EON Energia API:
- **Base URL**: `https://api-mmi.eon.it`
- **Endpoint**: `/DeeperConsumption/v1.0/ExtDailyConsumption`
- **Update Interval**: Every 6 hours

## Troubleshooting

### Token expired / Authentication errors
The integration automatically refreshes access tokens using your refresh token. If you see persistent authentication errors:
1. Go to **Settings** → **Devices & Services** → **EON Energia**
2. Click **Configure** to update your refresh token
3. Obtain a new refresh token from the web interface (see instructions above)

### No data
Energy data is typically available with a 2-day delay. The integration automatically fetches the most recent available data (checking up to 7 days back).

### Empty response
If the API returns empty data, the readings for that date haven't been processed yet by EON Energia.

### Historical data not showing in Energy Dashboard
1. First, run the `eon_energia.import_statistics` service to backfill historical data
2. Go to **Developer Tools** → **Statistics** and search for "eon" to verify the data was imported
3. Add the external statistic (e.g., `eon_energia:{POD}_consumption`) to your Energy Dashboard

### Token Status shows "invalid"
Check the Token Status sensor's `last_error` attribute for details. Common causes:
- Network connectivity issues
- EON Energia API temporarily unavailable
- Refresh token has been revoked (re-login and get a new one)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Disclaimer

This is an unofficial integration and is not affiliated with EON Energia. Use at your own risk.

## Credits

Developed by [@fezvrasta](https://github.com/fezvrasta)
