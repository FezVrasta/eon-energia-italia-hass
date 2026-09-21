#!/usr/bin/env python3
"""Seal the fallback API configuration for embedding in api_config.py.

    python tools/seal_fallback.py --base-url https://... --subscription-key <32 hex>

Prints the `SEALED_FALLBACK` constant to paste into
`custom_components/eon_energia/api_config.py`. Run it again whenever E.ON rotates the
key; do not hand-edit the blob.

## What this is, and what it is not

AES-256-GCM with an scrypt-derived key. The ciphertext is authenticated, so a corrupted
or tampered blob fails to open rather than yielding garbage.

It is **obfuscation, not protection.** The passphrase is a constant in the same
repository as the ciphertext, because the integration has to open it unattended on
someone else's machine, so anyone who can read the source can run the same two functions
and recover the plaintext. It keeps the values from being greppable in the tree and from
turning up in a search engine. It does not make them secret, and it does not change what
the repository distributes.

Base64 here is only how bytes are written into a Python source file. It is not the
encryption.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import textwrap

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

#: Shared with api_config.py. Changing either side invalidates every existing blob.
PASSPHRASE = b"eon_energia:api-config-fallback:v1"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LENGTH = 32
SALT_LENGTH = 16
NONCE_LENGTH = 12


def derive_key(salt: bytes) -> bytes:
    """Derive the AES key from the embedded passphrase and a per-blob salt."""
    kdf = Scrypt(salt=salt, length=KEY_LENGTH, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(PASSPHRASE)


def seal(payload: dict[str, str]) -> str:
    """Encrypt `payload` and return it as one base64 blob: salt || nonce || ciphertext."""
    salt = os.urandom(SALT_LENGTH)
    nonce = os.urandom(NONCE_LENGTH)
    ciphertext = AESGCM(derive_key(salt)).encrypt(
        nonce, json.dumps(payload, sort_keys=True).encode(), None
    )
    return base64.b64encode(salt + nonce + ciphertext).decode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--subscription-key", required=True)
    args = parser.parse_args()

    blob = seal({
        "base_url": args.base_url,
        "subscription_key": args.subscription_key,
    })

    wrapped = "\n".join(
        f'    "{line}"' for line in textwrap.wrap(blob, 72)
    )
    print("SEALED_FALLBACK = (")
    print(wrapped)
    print(")")


if __name__ == "__main__":
    main()
