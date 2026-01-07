#!/usr/bin/env python3
"""
Standalone test script for EON Energia Auth0 authentication.
Run with: python test_auth.py

You can also pass credentials as arguments:
python test_auth.py your_email@example.com your_password
"""

import asyncio
import logging
import sys

# Set up logging to see debug output
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    datefmt='%H:%M:%S'
)

# Import the auth module
sys.path.insert(0, 'custom_components/eon_energia')
from auth import EONAuth0Client, EONAuthError, EONMFARequiredError


async def test_login(username: str, password: str) -> None:
    """Test the Auth0 login flow."""
    print(f"\n{'='*60}")
    print(f"Testing EON Energia Auth0 login")
    print(f"Username: {username}")
    print(f"{'='*60}\n")

    try:
        tokens = await EONAuth0Client.login(username, password)
        print_success(tokens)

    except EONMFARequiredError as e:
        print(f"\n{'='*60}")
        print("MFA REQUIRED - Check your phone for SMS code")
        print(f"{'='*60}")

        # Prompt for MFA code
        mfa_code = input("\nEnter the SMS verification code: ").strip()

        if not mfa_code:
            print("Error: MFA code is required")
            sys.exit(1)

        try:
            tokens = await EONAuth0Client.submit_mfa_code(mfa_code, e.session_data)
            print_success(tokens)
        except EONAuthError as mfa_err:
            print(f"\n{'='*60}")
            print(f"MFA AUTHENTICATION FAILED: {mfa_err}")
            print(f"{'='*60}")
            sys.exit(1)

    except EONAuthError as e:
        print(f"\n{'='*60}")
        print(f"AUTHENTICATION FAILED: {e}")
        print(f"{'='*60}")
        sys.exit(1)
    except Exception as e:
        print(f"\n{'='*60}")
        print(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        print(f"{'='*60}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def print_success(tokens: dict) -> None:
    """Print successful authentication result."""
    print(f"\n{'='*60}")
    print("SUCCESS! Authentication completed.")
    print(f"{'='*60}")
    print(f"Access Token: {tokens.get('access_token', 'N/A')[:50]}...")
    print(f"Refresh Token: {tokens.get('refresh_token', 'N/A')[:50] if tokens.get('refresh_token') else 'N/A'}...")
    print(f"Token Type: {tokens.get('token_type', 'N/A')}")
    print(f"Expires In: {tokens.get('expires_in', 'N/A')} seconds")
    print(f"Scope: {tokens.get('scope', 'N/A')}")


def main():
    if len(sys.argv) >= 3:
        username = sys.argv[1]
        password = sys.argv[2]
    else:
        print("EON Energia Auth0 Test Script")
        print("-" * 40)
        username = input("Enter your EON Energia email: ").strip()
        password = input("Enter your EON Energia password: ").strip()

    if not username or not password:
        print("Error: Username and password are required")
        sys.exit(1)

    asyncio.run(test_login(username, password))


if __name__ == "__main__":
    main()
