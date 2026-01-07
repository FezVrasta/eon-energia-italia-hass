"""Auth0 authentication module for EON Energia."""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


def _extract_hidden_fields(html: str) -> dict[str, str]:
    """Extract hidden form fields from HTML."""
    hidden_fields = {}
    hidden_pattern = r'<input[^>]+type=["\']hidden["\'][^>]*>'
    for match in re.finditer(hidden_pattern, html, re.IGNORECASE):
        field_html = match.group(0)
        name_match = re.search(r'name=["\']([^"\']+)["\']', field_html)
        value_match = re.search(r'value=["\']([^"\']*)["\']', field_html)
        if name_match:
            field_name = name_match.group(1)
            field_value = value_match.group(1) if value_match else ""
            hidden_fields[field_name] = field_value
    return hidden_fields


def _extract_form_action(html: str, default_url: str) -> str:
    """Extract form action URL from HTML."""
    action_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html)
    if action_match:
        form_action = action_match.group(1)
        if form_action.startswith("/"):
            return AUTH_DOMAIN + form_action
        return form_action
    return default_url


# Auth0 Configuration
AUTH_DOMAIN = "https://auth.eon-energia.com"
AUTH_CLIENT_ID = "vEZ41cyr2pOHux9EKoN8dDgGb7UZc7EB"  # iOS app client_id
AUTH_REDIRECT_URI = "com.eon-energia.eon.auth0://auth.eon-energia.com/ios/com.eon-energia.eon/callback"
AUTH_AUDIENCE = "https://api-mmi.eon.it"
AUTH_SCOPE = "openid profile email offline_access"


def build_authorization_url() -> str:
    """Build the authorization URL for manual OAuth flow."""
    params = {
        "os": "ios",
        "response_type": "code",
        "client_id": AUTH_CLIENT_ID,
        "redirect_uri": AUTH_REDIRECT_URI,
        "scope": AUTH_SCOPE,
        "audience": AUTH_AUDIENCE,
    }
    return f"{AUTH_DOMAIN}/authorize?" + urllib.parse.urlencode(params)


def extract_code_from_callback(callback_url: str) -> str | None:
    """Extract authorization code from callback URL."""
    try:
        parsed = urllib.parse.urlparse(callback_url)
        query_params = urllib.parse.parse_qs(parsed.query)
        return query_params.get("code", [None])[0]
    except Exception:
        return None


class EONAuthError(Exception):
    """Exception for EON authentication errors."""

    pass


class EONMFARequiredError(EONAuthError):
    """Exception raised when MFA code is required."""

    def __init__(self, message: str, session_data: dict[str, Any]):
        super().__init__(message)
        self.session_data = session_data


class EONAuth0Client:
    """Auth0 client for EON Energia authentication."""

    @staticmethod
    async def login(username: str, password: str) -> dict[str, Any]:
        """
        Perform Auth0 login and return tokens.

        Args:
            username: EON Energia username (email)
            password: EON Energia password

        Returns:
            dict with access_token, refresh_token, etc.

        Raises:
            EONAuthError: If authentication fails
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; K) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Mobile Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": AUTH_DOMAIN,
            "Referer": AUTH_DOMAIN + "/",
        }

        # Create session with cookie jar to maintain cookies across requests
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            # Step 1: GET /authorize to initiate flow and get state
            authorize_params = {
                "os": "ios",
                "response_type": "code",
                "client_id": AUTH_CLIENT_ID,
                "redirect_uri": AUTH_REDIRECT_URI,
                "scope": AUTH_SCOPE,
                "audience": AUTH_AUDIENCE,
            }
            authorize_url = f"{AUTH_DOMAIN}/authorize?" + urllib.parse.urlencode(authorize_params)

            _LOGGER.debug("Starting Auth0 authorization flow")

            async with session.get(
                authorize_url, headers=headers, allow_redirects=True
            ) as resp:
                # After redirects, we should be at the login page with state in URL
                parsed = urllib.parse.urlparse(str(resp.url))
                query_params = urllib.parse.parse_qs(parsed.query)
                auth_state = query_params.get("state", [None])[0]

                # Get the login page HTML to check for hidden fields
                login_page_html = await resp.text()
                _LOGGER.debug("Login page URL: %s", resp.url)

                if not auth_state:
                    _LOGGER.error("No state returned from /authorize, URL: %s", resp.url)
                    raise EONAuthError("No state returned from authorization endpoint")

            # EON uses identifier-first login flow (two steps)
            # Step 2a: POST username to /u/login/identifier
            identifier_url = f"{AUTH_DOMAIN}/u/login/identifier?state={auth_state}"
            hidden_fields = _extract_hidden_fields(login_page_html)
            _LOGGER.debug("Hidden fields found: %s", list(hidden_fields.keys()))

            identifier_data = {
                **hidden_fields,
                "state": auth_state,
                "username": username,
                "action": "default",
            }

            _LOGGER.debug("Step 2a: Submitting username to %s", identifier_url)

            async with session.post(
                identifier_url,
                headers=headers,
                data=identifier_data,
                allow_redirects=True,
            ) as resp:
                password_page_url = str(resp.url)
                password_page_html = await resp.text()
                _LOGGER.debug("After identifier submit, URL: %s", password_page_url)

            # Check if we're on the password page
            if "/u/login/password" not in password_page_url:
                _LOGGER.error("Not redirected to password page, URL: %s", password_page_url)
                # Check for error messages in the page
                if "user not found" in password_page_html.lower() or "no account" in password_page_html.lower():
                    raise EONAuthError("User not found")
                raise EONAuthError("Invalid username or authentication flow error")

            # Step 2b: POST password to /u/login/password
            password_hidden_fields = _extract_hidden_fields(password_page_html)
            _LOGGER.debug("Password page hidden fields: %s", list(password_hidden_fields.keys()))

            password_url = f"{AUTH_DOMAIN}/u/login/password?state={auth_state}"
            password_data = {
                **password_hidden_fields,
                "state": auth_state,
                "username": username,
                "password": password,
                "action": "default",
            }

            _LOGGER.debug("Step 2b: Submitting password to %s", password_url)

            async with session.post(
                password_url,
                headers=headers,
                data=password_data,
                allow_redirects=False,
            ) as resp:
                redirect_url = resp.headers.get("Location")
                _LOGGER.debug("Password submit response status: %s", resp.status)
                _LOGGER.debug("Password submit redirect location: %s", redirect_url)

            # Check if redirect points back to login page (auth failed)
            if not redirect_url:
                _LOGGER.error("No redirect after password submit")
                raise EONAuthError("Invalid username or password")

            if "/u/login" in redirect_url:
                _LOGGER.error("Login failed - redirected back to login page: %s", redirect_url)
                raise EONAuthError("Invalid username or password")

            # Step 3: Extract authorization code
            # Follow redirect chain until we get the code or hit the callback URI
            code = None
            current_redirect = redirect_url
            max_redirects = 10  # Safety limit

            for i in range(max_redirects):
                if not current_redirect:
                    break

                _LOGGER.debug("Following redirect %d: %s", i + 1, current_redirect)

                # Check if this is the final callback with the code
                if current_redirect.startswith(AUTH_REDIRECT_URI) or "code=" in current_redirect:
                    parsed = urllib.parse.urlparse(current_redirect)
                    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
                    if code:
                        _LOGGER.debug("Got authorization code from redirect")
                        break

                # Build full URL if it's a relative path
                if current_redirect.startswith("/"):
                    full_url = AUTH_DOMAIN + current_redirect
                else:
                    full_url = current_redirect

                # Follow the redirect
                async with session.get(
                    full_url, headers=headers, allow_redirects=False
                ) as resp:
                    _LOGGER.debug("Redirect %d status: %s", i + 1, resp.status)
                    next_redirect = resp.headers.get("Location")
                    _LOGGER.debug("Redirect %d next location: %s", i + 1, next_redirect)

                    if resp.status == 200:
                        # This is a page, not a redirect
                        page_html = await resp.text()

                        # Check if it's mfa-detect page
                        if "mfa-detect" in full_url:
                            # MFA detection page - need to POST browser capabilities
                            mfa_hidden_fields = _extract_hidden_fields(page_html)
                            _LOGGER.debug("MFA detect page hidden fields: %s", list(mfa_hidden_fields.keys()))

                            form_action = _extract_form_action(page_html, full_url)

                            # Submit the MFA detection form with browser capabilities
                            mfa_data = {
                                **mfa_hidden_fields,
                                "js-available": "true",
                                "webauthn-available": "false",
                                "is-brave": "false",
                                "webauthn-platform-available": "false",
                                "action": "default",
                            }

                            _LOGGER.debug("Submitting MFA detect form to %s", form_action)

                            async with session.post(
                                form_action,
                                headers=headers,
                                data=mfa_data,
                                allow_redirects=False,
                            ) as mfa_resp:
                                next_redirect = mfa_resp.headers.get("Location")
                                _LOGGER.debug("MFA detect response status: %s", mfa_resp.status)
                                _LOGGER.debug("MFA detect redirect: %s", next_redirect)

                        # Check if it's MFA SMS/OTP code entry page
                        elif "mfa-sms" in full_url or "mfa-otp" in full_url or "enter code" in page_html.lower() or "verification code" in page_html.lower():
                            _LOGGER.info("MFA code required - SMS verification")
                            mfa_hidden_fields = _extract_hidden_fields(page_html)
                            form_action = _extract_form_action(page_html, full_url)

                            # Export cookies for session continuation
                            cookies_dict = {c.key: c.value for c in jar}

                            raise EONMFARequiredError(
                                "MFA SMS code required",
                                {
                                    "mfa_url": form_action,
                                    "hidden_fields": mfa_hidden_fields,
                                    "cookies": cookies_dict,
                                    "state": auth_state,
                                    "headers": headers,
                                }
                            )
                        else:
                            # Unknown page, log content for debugging
                            _LOGGER.error("Got 200 response on unknown page: %s", full_url)
                            _LOGGER.debug("Page content (first 2000 chars): %s", page_html[:2000])
                            break

                    if not next_redirect:
                        # No more redirects
                        break

                    current_redirect = next_redirect

            # Final check for code in the last redirect
            if not code and current_redirect:
                if current_redirect.startswith(AUTH_REDIRECT_URI):
                    parsed = urllib.parse.urlparse(current_redirect)
                    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
                    _LOGGER.debug("Got code from final callback URI")

            if not code:
                _LOGGER.error("Failed to extract authorization code from redirect: %s", redirect_url)
                raise EONAuthError(f"Auth0 login failed: {redirect_url}")

            # Step 4: Exchange code for tokens
            _LOGGER.debug("Exchanging authorization code for tokens")

            token_url = f"{AUTH_DOMAIN}/oauth/token"
            token_payload = {
                "grant_type": "authorization_code",
                "client_id": AUTH_CLIENT_ID,
                "code": code,
                "redirect_uri": AUTH_REDIRECT_URI,
            }

            async with session.post(
                token_url,
                headers={"Content-Type": "application/json"},
                json=token_payload,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.error("Token exchange failed: %s - %s", resp.status, text[:500])
                    raise EONAuthError(f"Token exchange failed: {text[:200]}")

                token_data = await resp.json()

            if "access_token" not in token_data:
                _LOGGER.error("No access token in response: %s", token_data)
                raise EONAuthError("Authentication failed - no access token received")

            _LOGGER.debug("Authentication successful")
            return token_data

    @staticmethod
    async def exchange_code_for_tokens(code: str) -> dict[str, Any]:
        """
        Exchange an authorization code for tokens.

        Args:
            code: The authorization code from OAuth callback

        Returns:
            dict with access_token, refresh_token, etc.

        Raises:
            EONAuthError: If token exchange fails
        """
        _LOGGER.debug("Exchanging authorization code for tokens")

        token_url = f"{AUTH_DOMAIN}/oauth/token"
        token_payload = {
            "grant_type": "authorization_code",
            "client_id": AUTH_CLIENT_ID,
            "code": code,
            "redirect_uri": AUTH_REDIRECT_URI,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                token_url,
                headers={"Content-Type": "application/json"},
                json=token_payload,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.error("Token exchange failed: %s - %s", resp.status, text[:500])
                    raise EONAuthError(f"Token exchange failed: {text[:200]}")

                token_data = await resp.json()

        if "access_token" not in token_data:
            _LOGGER.error("No access token in response: %s", token_data)
            raise EONAuthError("Authentication failed - no access token received")

        _LOGGER.debug("Token exchange successful")
        return token_data

    @staticmethod
    async def submit_mfa_code(mfa_code: str, session_data: dict[str, Any]) -> dict[str, Any]:
        """
        Submit MFA code and complete authentication.

        Args:
            mfa_code: The SMS/OTP code received by the user
            session_data: Session data from EONMFARequiredError

        Returns:
            dict with access_token, refresh_token, etc.

        Raises:
            EONAuthError: If authentication fails
        """
        mfa_url = session_data["mfa_url"]
        hidden_fields = session_data["hidden_fields"]
        cookies = session_data["cookies"]
        headers = session_data["headers"]

        _LOGGER.debug("Submitting MFA code to %s", mfa_url)

        # Recreate session with saved cookies
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            # Restore cookies
            for name, value in cookies.items():
                jar.update_cookies({name: value})

            # Submit MFA code
            mfa_data = {
                **hidden_fields,
                "code": mfa_code,
                "action": "default",
            }

            async with session.post(
                mfa_url,
                headers=headers,
                data=mfa_data,
                allow_redirects=False,
            ) as resp:
                redirect_url = resp.headers.get("Location")
                _LOGGER.debug("MFA submit response status: %s", resp.status)
                _LOGGER.debug("MFA submit redirect: %s", redirect_url)

            if not redirect_url:
                raise EONAuthError("MFA code submission failed - no redirect")

            if "/u/login" in redirect_url or "mfa" in redirect_url.lower():
                raise EONAuthError("Invalid MFA code")

            # Follow redirects to get the authorization code
            code = None
            current_redirect = redirect_url
            max_redirects = 10

            for i in range(max_redirects):
                if not current_redirect:
                    break

                _LOGGER.debug("Following MFA redirect %d: %s", i + 1, current_redirect)

                # Check if this is the final callback with the code
                if current_redirect.startswith(AUTH_REDIRECT_URI) or "code=" in current_redirect:
                    parsed = urllib.parse.urlparse(current_redirect)
                    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
                    if code:
                        _LOGGER.debug("Got authorization code from MFA redirect")
                        break

                # Build full URL if it's a relative path
                if current_redirect.startswith("/"):
                    full_url = AUTH_DOMAIN + current_redirect
                else:
                    full_url = current_redirect

                async with session.get(
                    full_url, headers=headers, allow_redirects=False
                ) as resp:
                    next_redirect = resp.headers.get("Location")
                    _LOGGER.debug("MFA redirect %d status: %s, next: %s", i + 1, resp.status, next_redirect)

                    if not next_redirect:
                        break

                    current_redirect = next_redirect

            # Final check
            if not code and current_redirect and current_redirect.startswith(AUTH_REDIRECT_URI):
                parsed = urllib.parse.urlparse(current_redirect)
                code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]

            if not code:
                raise EONAuthError(f"Failed to get authorization code after MFA: {current_redirect}")

            # Exchange code for tokens
            _LOGGER.debug("Exchanging authorization code for tokens")

            token_url = f"{AUTH_DOMAIN}/oauth/token"
            token_payload = {
                "grant_type": "authorization_code",
                "client_id": AUTH_CLIENT_ID,
                "code": code,
                "redirect_uri": AUTH_REDIRECT_URI,
            }

            async with session.post(
                token_url,
                headers={"Content-Type": "application/json"},
                json=token_payload,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.error("Token exchange failed: %s - %s", resp.status, text[:500])
                    raise EONAuthError(f"Token exchange failed: {text[:200]}")

                token_data = await resp.json()

            if "access_token" not in token_data:
                _LOGGER.error("No access token in response: %s", token_data)
                raise EONAuthError("Authentication failed - no access token received")

            _LOGGER.debug("Authentication successful after MFA")
            return token_data
