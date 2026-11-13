"""Sealed-box crypto for sandbox envelopes.

The sandbox seals every command and file to the guest's ephemeral X25519 key with
`crypto_box_seal` (X25519 + XSalsa20-Poly1305, ephemeral sender key hashed into
the nonce). ``PyNaCl`` is a binding to the same libsodium, so ``SealedBox`` is
the identical construction rather than a reimplementation; the guest side uses
``golang.org/x/crypto/nacl/box`` SealAnonymous, which the sandbox repo documents
as the same thing.
"""

from __future__ import annotations

import base64
import hashlib
import os

from nacl.public import PrivateKey, PublicKey, SealedBox

KEY_BYTES = 32
NONCE_BYTES = 32
MAX_ENVELOPE_BYTES = 2 * 1024 * 1024


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(value: str) -> bytes:
    # Accept both alphabets: the server emits standard base64, but tolerating
    # url-safe input costs nothing and avoids a confusing failure in a shell.
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, validate=False)
    except Exception:
        return base64.urlsafe_b64decode(padded)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def generate_keypair() -> tuple[bytes, bytes]:
    """(public, private) 32-byte keys, base64-ready."""
    private = PrivateKey.generate()
    return bytes(private.public_key), bytes(private)


def generate_nonce() -> bytes:
    return os.urandom(NONCE_BYTES)


def seal_to(recipient_public_key: bytes, plaintext: bytes) -> bytes:
    if len(recipient_public_key) != KEY_BYTES:
        raise ValueError(f"recipient public key must be {KEY_BYTES} bytes, got {len(recipient_public_key)}")
    return SealedBox(PublicKey(recipient_public_key)).encrypt(plaintext)


def open_sealed(ciphertext: bytes, public_key: bytes, private_key: bytes) -> bytes:
    sealed = SealedBox(PrivateKey(private_key))
    return sealed.decrypt(ciphertext)
