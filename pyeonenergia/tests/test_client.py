"""Token handling and status-code tests for the client.

E.ON's API does not answer 401 for a bad token. An expired token, a garbage one
and no Authorization header at all all come back as HTTP 500 with a generic
"Internal server error" body. The client used to refresh only on 401, so two
hours after setup every request failed and kept failing until the process was
restarted. These tests pin the behaviour that fixed it.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from pyeonenergia import EonEnergiaApiError, EonEnergiaClient
from pyeonenergia.client import _jwt_expiry

SERVER_ERROR = '{"statusCode": 500, "message": "Internal server error"}'


def token(expires_in: float) -> str:
    """A JWT-shaped token whose `exp` claim is `expires_in` seconds from now."""
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(time.time() + expires_in)}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.signature"


class FakeResponse:
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


class FakeSession:
    """Answers 500 unless the bearer is the one the server currently accepts."""

    def __init__(self, valid_token: str):
        self.valid_token = valid_token
        self.sent: list[str] = []
        self.always_fail = False
        self.response_status = 200
        self.response_body = '[{"ok": true}]'

    def request(self, method, url, headers=None, json=None, params=None):
        self.sent.append((headers or {}).get("Authorization", ""))
        if self.always_fail:
            return FakeResponse(500, SERVER_ERROR)
        if self.sent[-1] == f"Bearer {self.valid_token}":
            return FakeResponse(self.response_status, self.response_body)
        return FakeResponse(500, SERVER_ERROR)

    async def close(self):
        return None


def make_client(access_token, server_token, refreshes_to=None):
    """A client wired to a fake session, with config lookup stubbed out."""
    client = EonEnergiaClient(access_token, refresh_token="refresh-1")
    session = FakeSession(server_token)
    refreshes = {"count": 0}

    async def _get_session():
        return session

    async def _get_api_config():
        return {"base_url": "https://example.invalid", "subscription_key": "k" * 32}

    async def refresh_access_token():
        refreshes["count"] += 1
        if refreshes_to is None:
            return False
        client._access_token = refreshes_to
        client._token_expires_at = time.time() + 7200
        session.valid_token = refreshes_to
        return True

    client._get_session = _get_session
    client._get_api_config = _get_api_config
    client.refresh_access_token = refresh_access_token
    return client, session, refreshes


class TestExpiredToken:
    async def test_an_expired_token_never_reaches_the_server(self):
        # Already past its expiry, so the proactive path catches it and only one
        # request goes out carrying the new token.
        fresh = token(7200)
        client, session, refreshes = make_client(token(-60), fresh, refreshes_to=fresh)
        assert await client._request("GET", "/scsi/accounts/v1.0") == [{"ok": True}]
        assert refreshes["count"] == 1
        assert len(session.sent) == 1

    async def test_a_500_triggers_a_refresh_and_a_retry(self):
        # The original bug. An opaque token means the expiry cannot be read, so
        # there is nothing to act on until the server rejects it - and it rejects
        # it with 500, not 401, which is what used to go unnoticed.
        fresh = token(7200)
        client, session, refreshes = make_client("opaque-token", fresh, refreshes_to=fresh)
        assert client._token_expires_at is None
        assert await client._request("GET", "/scsi/accounts/v1.0") == [{"ok": True}]
        assert refreshes["count"] == 1
        assert len(session.sent) == 2

    async def test_a_nearly_expired_token_is_refreshed_before_the_call(self):
        # Reacting to a response is the fallback. Knowing the expiry is better,
        # so the stale token should never reach the server at all.
        fresh = token(7200)
        client, session, refreshes = make_client(token(60), fresh, refreshes_to=fresh)
        await client._request("GET", "/scsi/accounts/v1.0")
        assert refreshes["count"] == 1
        assert len(session.sent) == 1
        assert session.sent[0] == f"Bearer {fresh}"

    async def test_a_healthy_token_is_left_alone(self):
        good = token(7200)
        client, _session, refreshes = make_client(good, good, refreshes_to=good)
        await client._request("GET", "/scsi/accounts/v1.0")
        assert refreshes["count"] == 0


class TestGivingUp:
    async def test_a_persistent_500_raises_rather_than_looping(self):
        good = token(7200)
        client, session, _ = make_client(good, good, refreshes_to=good)
        session.always_fail = True
        with pytest.raises(EonEnergiaApiError, match="500"):
            await client._request("GET", "/scsi/accounts/v1.0")
        assert len(session.sent) == 2  # the original, then one retry

    async def test_a_refresh_that_cannot_succeed_gives_up(self):
        client, session, _ = make_client(token(-60), token(7200), refreshes_to=None)
        with pytest.raises(EonEnergiaApiError):
            await client._request("GET", "/scsi/accounts/v1.0")
        assert len(session.sent) == 1


class TestStatusCodes:
    async def test_202_with_a_body_is_a_success(self):
        # getInvoiceDvc answers 202 with the whole invoice list in the body.
        good = token(7200)
        client, session, _ = make_client(good, good, refreshes_to=good)
        session.response_status = 202
        session.response_body = '{"ListaFatture": [{"Numero": "1"}]}'
        result = await client._request("GET", "/scsi/invoices/v1.0/getInvoiceDvc")
        assert result == {"ListaFatture": [{"Numero": "1"}]}


class TestJwtExpiry:
    def test_reads_a_real_claim(self):
        assert _jwt_expiry(token(3600)) == pytest.approx(time.time() + 3600, abs=5)

    @pytest.mark.parametrize("value", [None, "", "not-a-jwt", "a.!!!!.c", "only.two"])
    def test_tolerates_anything_unreadable(self, value):
        # Never raise here: an unparseable token just means falling back to the
        # reactive path, which still works.
        assert _jwt_expiry(value) is None
