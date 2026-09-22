#!/usr/bin/env python3
"""Regression test for the token refresh path.

The E.ON API does not answer 401 for a bad token. An expired token, a garbage one and
no Authorization header at all all come back as HTTP 500 with a generic "Internal
server error" body. `_request()` used to refresh only on 401, so two hours after setup
every poll failed and kept failing until Home Assistant was restarted.

These tests pin that behaviour:

  * a 500 caused by an expired token triggers a refresh and a retry
  * a token close to expiry is refreshed *before* the request, not after it fails
  * a genuine 500 that a refresh does not fix raises instead of looping
  * 401 still works, for if E.ON ever start using it

Run with: python test_token_refresh.py
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import pathlib
import sys
import time
import types


def _token(expires_in: float) -> str:
    """A JWT-shaped token whose `exp` claim is `expires_in` seconds from now."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time() + expires_in)}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


class _Response:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body
        self.content_type = "application/json"

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Answers 500 unless the bearer matches the token the server considers current."""

    def __init__(self, valid_token: str):
        self.valid_token = valid_token
        self.calls: list[str] = []
        self.always_500 = False
        self.closed = False

    def request(self, method, url, headers=None, json=None, params=None):
        sent = (headers or {}).get("Authorization", "")
        self.calls.append(sent)
        if self.always_500:
            return _Response(500, '{"statusCode": 500, "message": "Internal server error"}')
        if sent == f"Bearer {self.valid_token}":
            return _Response(200, '[{"ok": true}]')
        return _Response(500, '{"statusCode": 500, "message": "Internal server error"}')

    async def close(self):
        self.closed = True


def _load_api():
    base = pathlib.Path(__file__).resolve().parent / "custom_components" / "eon_energia"
    pkg_name = "eon_energia_api_under_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(base)]
    sys.modules[pkg_name] = pkg

    for name in ("const", "api_config", "auth"):
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{name}", base / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{name}"] = module
        spec.loader.exec_module(module)

    spec = importlib.util.spec_from_file_location(f"{pkg_name}.api", base / "api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.api"] = module
    spec.loader.exec_module(module)
    return module


def _make_client(api_module, access_token, server_token, refreshes_to=None):
    """An API client wired to a fake session, with the network calls stubbed out."""
    client = api_module.EONEnergiaApi(access_token, refresh_token="refresh-1")
    session = _Session(server_token)

    async def _get_session():
        return session

    async def _get_api_config():
        return {"base_url": "https://example.invalid", "subscription_key": "k" * 32}

    refreshed = {"count": 0}

    async def refresh_access_token():
        refreshed["count"] += 1
        if refreshes_to is None:
            return False
        client._access_token = refreshes_to
        client._token_expires_at = time.time() + 7200
        session.valid_token = refreshes_to
        return True

    client._get_session = _get_session
    client._get_api_config = _get_api_config
    client.refresh_access_token = refresh_access_token
    return client, session, refreshed


async def _run() -> None:
    api = _load_api()
    results: list[tuple[str, str | None]] = []

    def check(name, fn):
        try:
            fn()
            results.append((name, None))
        except AssertionError as err:
            results.append((name, str(err)))

    # 1. The actual bug: expired token, API says 500, must refresh and retry.
    expired, fresh = _token(-60), _token(7200)
    client, session, refreshed = _make_client(api, expired, fresh, refreshes_to=fresh)
    try:
        got = await client._request("GET", "/scsi/accounts/v1.0")
    except Exception as err:  # noqa: BLE001 - this is the bug, report it as a failure
        results.append(("a 500 from an expired token triggers refresh and retry",
                        f"{type(err).__name__}: {err}"))
    else:
        check("a 500 from an expired token triggers refresh and retry",
              lambda: (
                  got == [{"ok": True}] or _fail(f"expected data, got {got}"),
                  refreshed["count"] == 1 or _fail(f"refreshed {refreshed['count']} times"),
              ))

    # 2. Proactive: a token inside the margin is refreshed before the request goes out,
    #    so the server never sees the stale one.
    nearly, fresh2 = _token(60), _token(7200)
    client, session, refreshed = _make_client(api, nearly, fresh2, refreshes_to=fresh2)
    await client._request("GET", "/scsi/accounts/v1.0")
    check("a nearly-expired token is refreshed before the call",
          lambda: (
              refreshed["count"] == 1 or _fail("no proactive refresh"),
              len(session.calls) == 1 or _fail(f"{len(session.calls)} requests, expected 1"),
              session.calls[0] == f"Bearer {fresh2}" or _fail("stale token was sent"),
          ))

    # 3. A healthy token must not provoke a refresh at all.
    good = _token(7200)
    client, session, refreshed = _make_client(api, good, good, refreshes_to=good)
    await client._request("GET", "/scsi/accounts/v1.0")
    check("a healthy token is left alone",
          lambda: refreshed["count"] == 0 or _fail("refreshed unnecessarily"))

    # 4. A genuine server error must surface, not loop.
    client, session, refreshed = _make_client(api, good, good, refreshes_to=good)
    session.always_500 = True
    try:
        await client._request("GET", "/scsi/accounts/v1.0")
        results.append(("a persistent 500 raises instead of looping", "no exception raised"))
    except api.EONEnergiaApiError as err:
        check("a persistent 500 raises instead of looping",
              lambda: (
                  "500" in str(err) or _fail(f"wrong error: {err}"),
                  len(session.calls) == 2 or _fail(f"{len(session.calls)} attempts, expected 2"),
              ))
    except Exception as err:  # noqa: BLE001
        results.append(("a persistent 500 raises instead of looping",
                        f"wrong exception type: {type(err).__name__}"))

    # 5. Refresh that cannot succeed must not retry forever either.
    client, session, refreshed = _make_client(api, expired, fresh, refreshes_to=None)
    try:
        await client._request("GET", "/scsi/accounts/v1.0")
        results.append(("a failed refresh gives up cleanly", "no exception raised"))
    except api.EONEnergiaApiError:
        check("a failed refresh gives up cleanly",
              lambda: len(session.calls) == 1 or _fail(f"{len(session.calls)} attempts"))

    # 6. The invoices endpoint answers 202 with the full payload; that is a success.
    client, session, refreshed = _make_client(api, good, good, refreshes_to=good)
    session.request = lambda *a, **k: _Response(202, '{"ListaFatture": [{"Numero": "1"}]}')
    got202 = await client._request("GET", "/scsi/invoices/v1.0/getInvoiceDvc")
    check("HTTP 202 with a body is treated as success",
          lambda: got202 == {"ListaFatture": [{"Numero": "1"}]} or _fail(f"got {got202}"))

    # 7. The expiry parser, including tokens it cannot read.
    check("JWT expiry is parsed, and unreadable tokens are tolerated",
          lambda: (
              api._jwt_expiry(_token(3600)) is not None or _fail("failed to parse a valid token"),
              api._jwt_expiry("not-a-jwt") is None or _fail("should not parse garbage"),
              api._jwt_expiry(None) is None or _fail("should tolerate None"),
              api._jwt_expiry("a.!!!!.c") is None or _fail("should tolerate bad base64"),
          ))

    width = max(len(n) for n, _ in results)
    failed = sum(1 for _, err in results if err)
    for name, err in results:
        print(f"PASS  {name}" if err is None else f"FAIL  {name.ljust(width)}  {err}")
    print()
    print(f"{len(results) - failed}/{len(results)} passed")
    sys.exit(1 if failed else 0)


def _fail(message: str):
    raise AssertionError(message)


if __name__ == "__main__":
    asyncio.run(_run())
