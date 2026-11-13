"""Payment and commitment signatures, verified by recovering the signer.

Recovering the address from the signature is the strongest check available
without the server: it proves the EIP-712 digest, the domain, the types and the
EIP-191 prefix all match what a verifier will reconstruct.
"""

from __future__ import annotations

import base64
import json

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

from hermes_sandbox.payment import (
    MOCK_EIP712_DOMAIN,
    MOCK_AUTHORIZATION_TYPES,
    MockPayer,
    payment_key,
    payment_signature_header,
)

REQUIREMENTS = {
    "scheme": "exact",
    "network": "mock:local",
    "amount": "50000",
    "asset": "mock:usdc",
    "payTo": "0x0000000000000000000000000000000000000402",
    "maxTimeoutSeconds": 300,
    "extra": {"decimals": 6, "tier": "standard", "durationSeconds": 300, "challengeNonce": "cn_" + "a" * 32},
}


def test_payment_signature_recovers_to_payer():
    payer = MockPayer()
    payload = payer.build_payment_payload(REQUIREMENTS)
    auth = payload["payload"]["authorization"]

    recovered = Account.recover_message(
        encode_typed_data(
            domain_data=MOCK_EIP712_DOMAIN,
            message_types=MOCK_AUTHORIZATION_TYPES,
            message_data={
                "from": auth["from"],
                "to": auth["to"],
                "value": int(auth["value"]),
                "validAfter": int(auth["validAfter"]),
                "validBefore": int(auth["validBefore"]),
                "nonce": bytes.fromhex(auth["nonce"][2:]),
            },
        ),
        signature=payload["payload"]["signature"],
    )
    assert recovered == payer.address


def test_payment_payload_echoes_requirements():
    payer = MockPayer()
    payload = payer.build_payment_payload(REQUIREMENTS)
    # The server hashes `accepted`, so it must be the advertised block verbatim.
    assert payload["accepted"] == REQUIREMENTS
    assert payload["x402Version"] == 2
    assert payload["payload"]["authorization"]["to"] == REQUIREMENTS["payTo"]
    assert payload["payload"]["authorization"]["value"] == REQUIREMENTS["amount"]


def test_authorization_window_is_bounded():
    payer = MockPayer()
    auth = payer.build_payment_payload(REQUIREMENTS)["payload"]["authorization"]
    assert int(auth["validAfter"]) < int(auth["validBefore"])
    assert int(auth["validBefore"]) - int(auth["validAfter"]) >= 120


def test_commitment_signature_recovers_to_payer():
    payer = MockPayer()
    payload = payer.build_payment_payload(REQUIREMENTS)
    signature = payer.sign_commitment(
        payment_payload=payload,
        client_public_key="Y2xpZW50LWtleQ==",
        client_nonce="bm9uY2U=",
        challenge_nonce=REQUIREMENTS["extra"]["challengeNonce"],
    )
    from hermes_sandbox.canonical import canonical_json
    from hermes_sandbox.payment import CLIENT_COMMITMENT_DOMAIN

    message = canonical_json(
        {
            "domain": CLIENT_COMMITMENT_DOMAIN,
            "paymentKey": payment_key(payload),
            "clientPublicKey": "Y2xpZW50LWtleQ==",
            "clientNonce": "bm9uY2U=",
            "challengeNonce": REQUIREMENTS["extra"]["challengeNonce"],
        }
    )
    recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    assert recovered == payer.address


def test_payment_key_changes_with_nonce():
    payer = MockPayer()
    first = payer.build_payment_payload(REQUIREMENTS)
    second = payer.build_payment_payload(REQUIREMENTS)
    assert payment_key(first) != payment_key(second)
    assert len(payment_key(first)) == 64


def test_signature_header_is_standard_base64_json():
    payer = MockPayer()
    payload = payer.build_payment_payload(REQUIREMENTS)
    header = payment_signature_header(payload)
    decoded = json.loads(base64.b64decode(header))
    assert decoded["x402Version"] == 2
    assert set(decoded) == {"x402Version", "accepted", "payload"}
    # The server's regex only accepts the standard alphabet.
    assert all(char.isalnum() or char in "+/=" for char in header)
