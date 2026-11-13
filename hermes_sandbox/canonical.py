"""Canonical JSON, byte-identical to ``shared/src/crypto.ts``.

The sandbox signs and hashes canonicalised JSON, so any difference in key order
or separators produces a different hash and the server rejects the request. This
is a direct port of the TypeScript implementation rather than a guess at what
"canonical" means:

* object keys sorted by code unit
* no whitespace
* ``undefined`` (``None`` here) values dropped, not serialised as ``null``
* arrays keep their order
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(value: Any) -> str:
    if value is None or not isinstance(value, (dict, list)):
        # Matches JSON.stringify for scalars, including its escaping.
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    entries = sorted(
        ((key, item) for key, item in value.items() if item is not None),
        key=lambda pair: pair[0],
    )
    body = ",".join(f"{json.dumps(key, ensure_ascii=False)}:{canonical_json(item)}" for key, item in entries)
    return "{" + body + "}"


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")
