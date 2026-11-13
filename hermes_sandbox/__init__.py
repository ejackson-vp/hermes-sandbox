"""hermes-sandbox: run Hermes terminal commands in an x402 sandbox microVM.

The provider and environment are imported lazily. They depend on Hermes'
``agent`` and ``tools`` packages, which only exist inside a Hermes install, and
importing them eagerly would make the HTTP client unusable from a plain test
process or a one-off script.
"""

from __future__ import annotations

from .config import SandboxConfig
from .errors import (
    OutcomeUnknownError,
    ResultExpiredError,
    SandboxError,
    SessionBusyError,
    SessionExpiredError,
    TransportError,
)

__all__ = [
    "SandboxConfig",
    "SandboxError",
    "SessionExpiredError",
    "SessionBusyError",
    "OutcomeUnknownError",
    "ResultExpiredError",
    "TransportError",
    "register",
]


def register(ctx) -> None:
    """Plugin entry point, referenced from ``plugin.yaml``."""
    from .provider import register as _register

    _register(ctx)


def __getattr__(name: str):
    if name in ("SandboxProvider", "SandboxEnvironment"):
        from importlib import import_module

        module = import_module(".provider" if name == "SandboxProvider" else ".environment", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
