"""Exceptions raised by the E.ON Energia client."""

from __future__ import annotations

from typing import Any


class EonEnergiaError(Exception):
    """Base class for every error this library raises."""


class EonEnergiaApiError(EonEnergiaError):
    """The API rejected a request, or answered something unusable.

    Note that E.ON's API reports a bad token as HTTP 500 rather than 401, so this
    is not a reliable signal that the server itself is unwell. The client retries
    once with a refreshed token before giving up and raising.
    """


class EonEnergiaAuthError(EonEnergiaError):
    """Authentication failed and cannot be recovered without the user."""


class EonEnergiaTokenRefreshError(EonEnergiaError):
    """A refresh token was rejected, usually because it has been rotated away."""


class EonEnergiaCaptchaError(EonEnergiaAuthError):
    """Auth0's bot detection is challenging the login.

    Risk-scored, so it comes and goes. Nothing headless can answer it; the caller
    has to fall back to a login a human completes in a browser.
    """


class EonEnergiaMfaRequiredError(EonEnergiaAuthError):
    """A one-time code is needed to finish signing in.

    Carries the session state the caller must hand back to
    :meth:`EonEnergiaAuth.submit_mfa_code` along with the code.
    """

    def __init__(self, message: str, session_data: dict[str, Any]) -> None:
        """Keep the partially completed login alongside the message."""
        super().__init__(message)
        self.session_data = session_data


class EonEnergiaConfigError(EonEnergiaError):
    """The API base URL and subscription key could not be resolved."""
