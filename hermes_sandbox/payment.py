"""Mock x402 payment + client commitment, ported from the TypeScript SDK.

The homelab runs ``PAYMENT_MODE=mock`` (``network: mock:local``). Mock payments
have no economic value: the client signs a real EIP-712 EIP-3009
``TransferWithAuthorization`` with an ephemeral wallet, and the server verifies the
signature rather than moving any money. Nothing here is a bypass; the public x402
flow is unchanged and any other mock client takes the same path.

Two signatures are required by ``api/src/use-cases/create-session-stages.ts``:

1. the EIP-712 authorisation, carrying the payment;
2. an EIP-191 "client commitment" over a canonical JSON message that binds the
   paying wallet to *this* client's public key and the server's single-use
   ``challengeNonce``. Without (2) the server answers ``401 client_proof_required``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from .canonical import canonical_json
from .crypto import b64encode, sha256_hex

PAYMENT_VERSION = 2
MOCK_NETWORK = "mock:local"
MOCK_SCHEME = "exact"
MOCK_ASSET = "mock:usdc"

CLIENT_COMMITMENT_DOMAIN = "x402-sandbox/client-commitment/v2"

MOCK_EIP712_DOMAIN = {
    "name": "x402 Mock USD",
    "version": "1",
    "chainId": 0,
    "verifyingContract": "0x0000000000000000000000000000000000000402",
}

MOCK_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}


@dataclass(frozen=True)
class Challenge:
    """What the server hands back in the 402 challenge."""

    requirements: dict[str, Any]
    challenge_nonce: str


class MockPayer:
    """Signs mock payments and client commitments with one ephemeral wallet.

    One payer is created per session create, so the paying address, the
    commitment signer and the capability recipient all belong to the same client.
    """

    def __init__(self, private_key: bytes | None = None):
        self._account = Account.from_key(private_key or os.urandom(32))

    @property
    def address(self) -> str:
        return self._account.address

    def build_payment_payload(self, requirements: dict[str, Any]) -> dict[str, Any]:
        """EIP-3009 authorisation, signed, in the shape the server hashes.

        The server's ``paymentKey`` is sha256 over the canonical form of
        ``{x402Version, network, scheme, asset, payTo, amount, payload}``, so the
        ``accepted`` block must echo the advertised requirements exactly.
        """
        now = int(time.time())
        nonce_hex = os.urandom(32).hex()
        authorization = {
            "from": self._account.address,
            "to": requirements["payTo"],
            "value": requirements["amount"],
            "validAfter": str(now - 60),
            "validBefore": str(now + max(60, requirements.get("maxTimeoutSeconds") or 300)),
            "nonce": "0x" + nonce_hex,
        }
        signed = Account.sign_typed_data(
            self._account.key,
            domain_data=MOCK_EIP712_DOMAIN,
            message_types=MOCK_AUTHORIZATION_TYPES,
            message_data={
                "from": authorization["from"],
                "to": authorization["to"],
                "value": int(authorization["value"]),
                "validAfter": int(authorization["validAfter"]),
                "validBefore": int(authorization["validBefore"]),
                "nonce": bytes.fromhex(nonce_hex),
            },
        )
        return {
            "x402Version": PAYMENT_VERSION,
            "accepted": requirements,
            "payload": {"authorization": authorization, "signature": _hex(signed.signature)},
        }

    def sign_commitment(
        self,
        *,
        payment_payload: dict[str, Any],
        client_public_key: str,
        client_nonce: str,
        challenge_nonce: str,
    ) -> str:
        """EIP-191 signature over the canonical commitment message.

        ``Account.sign_message`` applies the EIP-191 prefix, which is what the
        server verifies with viem's ``verifyMessage``.
        """
        message = canonical_json(
            {
                "domain": CLIENT_COMMITMENT_DOMAIN,
                "paymentKey": payment_key(payment_payload),
                "clientPublicKey": client_public_key,
                "clientNonce": client_nonce,
                "challengeNonce": challenge_nonce,
            }
        )
        return _hex(self._account.sign_message(encode_defunct(text=message)).signature)


def _hex(value: bytes) -> str:
    text = value.hex()
    return text if text.startswith("0x") else "0x" + text


def payment_signature_header(payment_payload: dict[str, Any]) -> str:
    """``encodePaymentSignatureHeader``: standard base64 of the JSON payload."""
    body = json.dumps(payment_payload, separators=(",", ":")).encode("utf-8")
    return b64encode(body)


def payment_key(payload: dict[str, Any]) -> str:
    """Mirror of ``paymentKey`` in shared/src/payment.ts."""
    accepted = payload.get("accepted") or {}
    return sha256_hex(
        canonical_json(
            {
                "x402Version": payload.get("x402Version"),
                "network": accepted.get("network"),
                "scheme": accepted.get("scheme"),
                "asset": accepted.get("asset"),
                "payTo": accepted.get("payTo"),
                "amount": accepted.get("amount"),
                "payload": payload.get("payload"),
            }
        )
    )
