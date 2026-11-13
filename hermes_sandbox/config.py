"""Configuration for the sandbox backend.

Every value comes from the environment so it can be set from a ConfigMap/Secret
without rebuilding the image. Defaults match the homelab deployment: in-cluster
ClusterIP, mock payments, one pinned guest image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .payment import PAYER_KEY_ENV, MockPayer, PayerKey, PayerKeyError

#: Tiers the platform exposes today (docs/resource-model.md).
TIERS = ("short", "standard", "extended", "long", "custom")


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class SandboxConfig:
    #: ClusterIP Service, not the public ingress: in-cluster traffic avoids a
    #: TLS round trip through the Tailscale proxy and the forgeable XFF path.
    api_url: str = "http://api.sandbox-system.svc.cluster.local:3000"
    verify_tls: bool = True
    tier: str = "standard"
    duration_seconds: int | None = None
    cpu_millis: int | None = None
    memory_mib: int | None = None
    disk_mib: int | None = None
    #: The platform pins one guest image; this is validated against /v1/images.
    image: str = "guest"
    #: Refuse to start a command in a session with less lease than this, so a
    #: rollover happens between commands instead of mid-command.
    min_remaining_seconds: int = 60
    #: Hard cap from the guest watchdog, mirrored here so we can fail early.
    max_command_seconds: int = 300
    #: Carry the workspace into a replacement session on rollover.
    carry_workspace: bool = True
    #: Never send the host environment to the guest. Only these names are passed.
    env_allowlist: tuple[str, ...] = ()
    #: Optional stable payer key. When set, every session create is paid from the
    #: same address, which is what a sandbox allowlist matches on. repr=False and
    #: a redacting PayerKey keep it out of logs and config dumps.
    payer_key: PayerKey | None = field(default=None, repr=False)
    #: Why a set key could not be used. Safe to show: it never contains the key.
    payer_key_error: str | None = None
    # Above the controller's own ~90s provisioning timeout, so the platform
    # decides a cold start failed and refunds, instead of the client giving up
    # on a session that is about to become ready.
    create_timeout_seconds: float = 150.0
    request_timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SandboxConfig:
        source = dict(os.environ if env is None else env)
        allow = source.get("SANDBOX_ENV_ALLOWLIST", "")
        payer_key, payer_key_error = _payer_key_from(source)
        return cls(
            api_url=source.get("SANDBOX_API_URL", cls.api_url),
            verify_tls=_bool(source.get("SANDBOX_VERIFY_TLS"), True),
            tier=source.get("SANDBOX_TIER", cls.tier),
            duration_seconds=_optional_int(source.get("SANDBOX_DURATION_SECONDS")),
            cpu_millis=_optional_int(source.get("SANDBOX_CPU_MILLIS")),
            memory_mib=_optional_int(source.get("SANDBOX_MEMORY_MIB")),
            disk_mib=_optional_int(source.get("SANDBOX_DISK_MIB")),
            image=source.get("SANDBOX_IMAGE", cls.image),
            min_remaining_seconds=_int(source.get("SANDBOX_MIN_REMAINING_SECONDS"), 60),
            max_command_seconds=_int(source.get("SANDBOX_MAX_COMMAND_SECONDS"), 300),
            carry_workspace=_bool(source.get("SANDBOX_CARRY_WORKSPACE"), True),
            env_allowlist=tuple(name.strip() for name in allow.split(",") if name.strip()),
            payer_key=payer_key,
            payer_key_error=payer_key_error,
            create_timeout_seconds=float(_int(source.get("SANDBOX_CREATE_TIMEOUT_SECONDS"), 90)),
            request_timeout_seconds=float(_int(source.get("SANDBOX_REQUEST_TIMEOUT_SECONDS"), 120)),
        )

    def validate(self) -> list[str]:
        """Problems with this configuration, as human-readable strings."""
        problems: list[str] = []
        if not self.api_url:
            problems.append("SANDBOX_API_URL is empty")
        elif not self.api_url.startswith(("http://", "https://")):
            problems.append(f"SANDBOX_API_URL must be an http(s) URL, got {self.api_url!r}")
        if self.tier not in TIERS:
            problems.append(f"SANDBOX_TIER must be one of {list(TIERS)}, got {self.tier!r}")
        if self.tier == "custom" and not self.duration_seconds:
            problems.append("SANDBOX_DURATION_SECONDS is required when SANDBOX_TIER=custom")
        if self.max_command_seconds <= 0:
            problems.append("SANDBOX_MAX_COMMAND_SECONDS must be positive")
        if PAYER_KEY_ENV in self.env_allowlist:
            # The one way the key could reach a model-authored command: explicit
            # operator opt-in to forward it. Refuse instead of honouring it.
            problems.append(f"SANDBOX_ENV_ALLOWLIST must not include {PAYER_KEY_ENV}")
        if self.payer_key_error:
            problems.append(self.payer_key_error)
        return problems

    def payer(self) -> MockPayer | None:
        """The payer for every create: stable when a key is set, else ephemeral.

        ``None`` means ``SandboxClient`` makes a fresh wallet per create, which is
        the behaviour every deployment had before this option existed.
        """
        return self.payer_key.payer() if self.payer_key is not None else None

    def session_kwargs(self) -> dict:
        return {
            "tier": self.tier,
            "duration_seconds": self.duration_seconds,
            "cpu_millis": self.cpu_millis,
            "memory_mib": self.memory_mib,
            "disk_mib": self.disk_mib,
        }


def _optional_int(raw: str | None) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _payer_key_from(source: dict[str, str]) -> tuple[PayerKey | None, str | None]:
    """Parse ``HERMES_PAYER_KEY`` into a validated key, or an error to report.

    Parsing happens here, at config load, so a malformed key fails before the
    first create rather than on the first payment. The error string is built from
    the variable name and the reason only; ``from_env`` never raises so the
    picker and doctor can render the problem instead of a traceback.
    """
    raw = source.get(PAYER_KEY_ENV, "")
    if not raw.strip():
        return None, None
    try:
        return PayerKey.parse(raw), None
    except PayerKeyError as exc:
        return None, str(exc)

