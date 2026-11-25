"""Plugin shim.

Hermes scans ``/opt/hermes/plugins/<name>/`` for a plugin module. The provider
itself lives in the venv's site-packages (``hermes_sandbox``), so this re-exports
its ``register``.

The refresh below is deliberate. A supervised gateway that reloads plugins can
lose the venv site-packages from ``sys.path``, or keep a stale finder for it, and
the import then fails with ``No module named 'hermes_sandbox'`` even though the
package is installed. Re-adding the directory and invalidating the import caches
makes the shim independent of how the gateway was started.
"""

from __future__ import annotations

import glob
import importlib
import os
import sys

for _candidate in (
    os.environ.get("HERMES_SITE"),
    *glob.glob("/opt/hermes/.venv/lib/python*/site-packages"),
):
    if not _candidate:
        continue
    while _candidate in sys.path:
        sys.path.remove(_candidate)
    sys.path.insert(0, _candidate)

importlib.invalidate_caches()

from hermes_sandbox import register  # noqa: E402

__all__ = ["register"]
