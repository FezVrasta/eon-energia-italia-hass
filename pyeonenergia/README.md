# pyeonenergia

Async client for the E.ON Energia (Italy) customer API.

E.ON publish no public API. This talks to the same endpoints their own app and web client use, with credentials you supply, to read your own consumption and billing data. It is the protocol layer behind the [E.ON Energia Home Assistant integration](https://github.com/fezvrasta/eon-energia-italia-hass), split out so it can be used, tested and released on its own.

```python
from pyeonenergia import EonEnergiaAuth, EonEnergiaClient, build_authorization_url

# Sign in. E.ON's Auth0 client forbids password grants, so the only way in is the
# browser flow: open this URL, log in, and pull the code out of the callback.
print(await build_authorization_url())
tokens = await EonEnergiaAuth.exchange_code_for_tokens(code)

client = EonEnergiaClient(tokens["access_token"], tokens["refresh_token"])
for pod in await client.get_points_of_delivery():
    print(pod.pod_id, pod.address, pod.is_active)
```

Install with `pip install pyeonenergia`. Python 3.12 or newer.

## What it gives you

`EonEnergiaClient` handles tokens on its own: it reads the expiry out of the access token and refreshes shortly before it lapses, so you hand it a token pair once and stop thinking about it. Pass `token_callback` to be told when they rotate, and persist what you are given — Auth0 rotates the refresh token on every use, so the old one stops working immediately.

| Method | Returns |
| --- | --- |
| `get_accounts()` | `list[Account]` |
| `get_points_of_delivery()` | `list[PointOfDelivery]` |
| `get_daily_consumption(pod, start, end)` | `list[HourlyConsumption]` |
| `get_invoices(start, end)` | `list[Invoice]` |
| `get_invoices_for_pod(pod, start, end)` | `list[Invoice]` |
| `get_monthly_consumption(...)` | raw `list[dict]` |
| `get_energy_wallet(pod, year)` | raw `dict` |

Every model keeps the untouched response in `.raw`, so a field with no typed home is still reachable without going round the library.

## Tariff bands and the calendar

`pyeonenergia.tariff` carries the Italian domain logic: which of the three ARERA bands an hour falls in, the national holidays including Easter Monday, and the mapping from E.ON's hourly fields onto real instants.

```python
from pyeonenergia import fascia_for_hour, local_hour_starts

fascia_for_hour(date(2026, 9, 22), 10)      # "F1" — Tuesday 09:00, peak
len(local_hour_starts(date(2026, 10, 25)))  # 25 — the clocks go back
```

That last one matters. E.ON send twenty-four `valore_hNN` fields every day of the year, including the two days when the Italian day is not twenty-four hours long. `HourlyConsumption.hours()` resolves them against the real calendar, so on the short day the field with nowhere to go is dropped and on the long day the final hour is simply unreported — rather than two readings landing on the same instant, which is a silent double count.

## Two things about this API

**It answers HTTP 500 for a bad token, never 401.** An expired token, a garbage bearer and no `Authorization` header at all all come back as `{"statusCode": 500, "message": "Internal server error"}`. A status code alone cannot tell an auth problem from a server fault, so the client treats a 500 as possibly-auth, refreshes once and retries, and only then reports it as a server error. Worth knowing before you debug anything here.

**`getInvoiceDvc` answers 202**, with the complete invoice list in the body. Anything checking for exactly 200 throws the data away.

## Errors

All descend from `EonEnergiaError`.

`EonEnergiaAuthError` for a failed login, with `EonEnergiaCaptchaError` and `EonEnergiaMfaRequiredError` as the two cases worth handling separately: the first means Auth0's bot detection has decided you look automated and nothing headless will get past it, the second carries the session state to hand back with the code. `EonEnergiaApiError` for a rejected request, `EonEnergiaConfigError` when the API base URL and subscription key cannot be resolved.

## Development

```sh
pip install -e ".[test]"
pytest
```

The API configuration is normally scraped from E.ON's public login page at runtime. That page sits behind Cloudflare and intermittently refuses non-browser clients, so there is a fallback; `tools/seal_fallback.py` regenerates it if the values ever change.

## Licence

MIT. Not affiliated with, authorised by or endorsed by E.ON Energia S.p.A. See the integration repository for the full legal notice.
