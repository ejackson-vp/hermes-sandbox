"""Print the payer address for ``HERMES_PAYER_KEY``.

    HERMES_PAYER_KEY=... python -m hermes_sandbox.payer_address

The address is the value a sandbox operator adds to the payer allowlist. Only the
address is printed: the key stays in the environment, and a failure names the
variable rather than quoting it.

Exit codes: 0 an address was printed, 2 the key is missing or unusable.
"""

from __future__ import annotations

import os
import sys

from .payment import PAYER_KEY_ENV, PayerKey, PayerKeyError


def main(env: dict[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    raw = source.get(PAYER_KEY_ENV, "")
    if not raw.strip():
        print(
            f"{PAYER_KEY_ENV} is not set. Sessions pay from an ephemeral address, "
            "which cannot be allowlisted.",
            file=sys.stderr,
        )
        return 2
    try:
        key = PayerKey.parse(raw)
    except PayerKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(key.address)
    return 0


if __name__ == "__main__":
    sys.exit(main())
