"""The stable payer key: allowlisted access without changing the default.

Two properties matter and are both asserted here. With ``HERMES_PAYER_KEY`` set,
every create is paid from one address, and the same address signs the client
commitment, so an operator can allowlist it. With the variable unset, nothing
changes: each create still gets a fresh wallet.

Nothing in this file needs Hermes or a running sandbox API.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from hermes_sandbox.canonical import canonical_json
from hermes_sandbox.client import SandboxClient
from hermes_sandbox.config import SandboxConfig
from hermes_sandbox.crypto import b64encode
from hermes_sandbox.payment import (
    CLIENT_COMMITMENT_DOMAIN,
    PAYER_KEY_ENV,
    MockPayer,
    PayerKey,
    PayerKeyError,
    payment_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Derived rather than random so the assertions are stable, and derived rather
#: than literal so no real key is ever pasted into a test file.
KEY_HEX = hashlib.sha256(b"hermes-sandbox payer key test fixture").hexdigest()

REQUIREMENTS = {
    "scheme": "exact",
    "network": "mock:local",
    "amount": "50000",
    "asset": "mock:usdc",
    "payTo": "0x0000000000000000000000000000000000000402",
    "maxTimeoutSeconds": 300,
    "extra": {"decimals": 6, "tier": "standard", "durationSeconds": 300, "challengeNonce": "cn_" + "a" * 32},
}

ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _challenge() -> httpx.Response:
    body = json.dumps({"x402Version": 2, "accepts": [REQUIREMENTS]}).encode("utf-8")
    return httpx.Response(402, headers={"payment-required": b64encode(body)})


def _created() -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "sessionId": "sbx_001",
            "capability": "cap-sbx_001",
            "state": "READY",
            "tier": "standard",
            "durationSeconds": 300,
            "session": {
                "guestPublicKey": b64encode(b"\x03" * 32),
                "leaseExpiresAt": "2999-01-01T00:00:00.000Z",
            },
        },
    )


def _client(monkeypatch) -> tuple[SandboxClient, list[dict]]:
    """A client whose first POST is challenged and whose retry succeeds.

    Records the payment payload and the request body that carried it, which is
    everything the two signatures are checked against.
    """
    client = SandboxClient("http://sandbox.invalid")
    seen: list[dict] = []

    def fake_request(method, path, **kwargs):
        body = kwargs.get("json") or {}
        header = (kwargs.get("headers") or {}).get("PAYMENT-SIGNATURE")
        if header:
            seen.append({"payment": json.loads(base64.b64decode(header)), "body": body})
            return _created()
        return _challenge()

    monkeypatch.setattr(client, "_request", fake_request)
    return client, seen


def _recover_commitment(entry: dict) -> str:
    message = canonical_json(
        {
            "domain": CLIENT_COMMITMENT_DOMAIN,
            "paymentKey": payment_key(entry["payment"]),
            "clientPublicKey": entry["body"]["clientPublicKey"],
            "clientNonce": entry["body"]["clientNonce"],
            "challengeNonce": entry["body"]["challengeNonce"],
        }
    )
    return Account.recover_message(encode_defunct(text=message), signature=entry["body"]["clientSignature"])


# ------------------------------------------------------------------ stable key


def test_key_set_pays_from_one_address_across_creates(monkeypatch):
    config = SandboxConfig.from_env({PAYER_KEY_ENV: KEY_HEX})
    assert config.validate() == []
    payer = config.payer()
    assert payer is not None

    client, seen = _client(monkeypatch)
    try:
        client.create_session(tier="standard", payer=payer)
        client.create_session(tier="standard", payer=payer)
    finally:
        client.close()

    addresses = [entry["payment"]["payload"]["authorization"]["from"] for entry in seen]
    assert addresses == [payer.address, payer.address]


def test_the_stable_address_is_also_the_commitment_signer(monkeypatch):
    """The allowlisted address must be the one the server recovers from both signatures."""
    config = SandboxConfig.from_env({PAYER_KEY_ENV: KEY_HEX})
    payer = config.payer()
    assert payer is not None

    client, seen = _client(monkeypatch)
    try:
        client.create_session(tier="standard", payer=payer)
    finally:
        client.close()

    entry = seen[0]
    # The EIP-712 authorisation recovers to the address the payment claims.
    auth = entry["payment"]["payload"]["authorization"]
    assert auth["from"] == payer.address
    # And so does the EIP-191 commitment, which is what binds the payer to this client.
    assert _recover_commitment(entry) == payer.address


def test_a_key_with_a_0x_prefix_is_accepted():
    with_prefix = PayerKey.parse("0x" + KEY_HEX)
    without = PayerKey.parse(KEY_HEX)
    assert with_prefix.address == without.address
    assert ADDRESS_PATTERN.match(with_prefix.address)


def test_the_key_never_appears_in_a_repr_or_a_config_dump():
    config = SandboxConfig.from_env({PAYER_KEY_ENV: KEY_HEX})
    assert KEY_HEX not in repr(config)
    assert KEY_HEX not in str(config)
    assert KEY_HEX not in repr(config.payer_key)
    assert KEY_HEX not in repr(config.payer)
    assert config.payer_key is not None
    assert config.payer_key.address in repr(config.payer_key)


# --------------------------------------------------------------- ephemeral key


def test_no_key_means_no_payer_and_a_new_address_per_create(monkeypatch):
    config = SandboxConfig.from_env({})
    assert config.payer_key is None
    assert config.payer() is None
    assert config.validate() == []

    client, seen = _client(monkeypatch)
    try:
        client.create_session(tier="standard")
        client.create_session(tier="standard")
    finally:
        client.close()

    addresses = [entry["payment"]["payload"]["authorization"]["from"] for entry in seen]
    assert len(set(addresses)) == 2, "the ephemeral default must not reuse one wallet"


def test_an_empty_variable_is_treated_as_unset():
    config = SandboxConfig.from_env({PAYER_KEY_ENV: "   "})
    assert config.payer_key is None
    assert config.payer_key_error is None
    assert config.validate() == []


# ---------------------------------------------------------------- bad key input


@pytest.mark.parametrize(
    "raw",
    [
        "not-hex",
        "0x" + "a" * 63,
        "a" * 65,
        "0" * 64,  # right length, not a usable scalar
        "f" * 64,  # above the secp256k1 curve order
    ],
)
def test_a_malformed_key_raises_without_quoting_it(raw):
    with pytest.raises(PayerKeyError) as excinfo:
        PayerKey.parse(raw)
    message = str(excinfo.value)
    assert PAYER_KEY_ENV in message
    assert raw not in message


def test_a_malformed_key_fails_at_config_load_not_at_the_first_create():
    raw = "zz" * 32
    config = SandboxConfig.from_env({PAYER_KEY_ENV: raw})
    assert config.payer_key is None
    problems = config.validate()
    assert any(PAYER_KEY_ENV in problem for problem in problems)
    assert all(raw not in problem for problem in problems)


def test_the_key_cannot_be_forwarded_to_the_guest_by_allowlist():
    """The allowlist is the only path from host env to guest env. Refuse it."""
    config = SandboxConfig.from_env({"SANDBOX_ENV_ALLOWLIST": f"FOO,{PAYER_KEY_ENV}", PAYER_KEY_ENV: KEY_HEX})
    problems = config.validate()
    assert any(PAYER_KEY_ENV in problem and "SANDBOX_ENV_ALLOWLIST" in problem for problem in problems)


# ----------------------------------------------------------------- the printer


def _run_printer(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_sandbox.payer_address"],
        cwd=REPO_ROOT,
        env={"PATH": os.environ.get("PATH", ""), **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_printer_prints_only_the_address_the_commitment_signs_with():
    result = _run_printer({PAYER_KEY_ENV: KEY_HEX})
    assert result.returncode == 0, result.stderr
    printed = result.stdout.strip()
    assert ADDRESS_PATTERN.match(printed)
    assert len(result.stdout.splitlines()) == 1, "the printer must print one line, the address"

    payer = PayerKey.parse(KEY_HEX).payer()
    assert printed == payer.address

    # Agreement with the signer, not just with another Account.from_key call:
    # recover the address from a real commitment signature.
    payment = payer.build_payment_payload(REQUIREMENTS)
    entry = {
        "payment": payment,
        "body": {
            "clientPublicKey": "Y2xpZW50LWtleQ==",
            "clientNonce": "bm9uY2U=",
            "challengeNonce": REQUIREMENTS["extra"]["challengeNonce"],
            "clientSignature": payer.sign_commitment(
                payment_payload=payment,
                client_public_key="Y2xpZW50LWtleQ==",
                client_nonce="bm9uY2U=",
                challenge_nonce=REQUIREMENTS["extra"]["challengeNonce"],
            ),
        },
    }
    assert _recover_commitment(entry) == printed


def test_the_printer_refuses_a_malformed_key_without_printing_it():
    raw = "b" * 62
    result = _run_printer({PAYER_KEY_ENV: raw})
    assert result.returncode == 2
    assert result.stdout == ""
    assert PAYER_KEY_ENV in result.stderr
    assert raw not in result.stderr


def test_the_printer_says_so_when_no_key_is_set():
    result = _run_printer({})
    assert result.returncode == 2
    assert result.stdout == ""
    assert "not set" in result.stderr
