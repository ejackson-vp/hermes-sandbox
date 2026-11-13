"""Configuration for the sandbox backend.

Every value comes from the environment so it can be set from a ConfigMap/Secret
without rebuilding the image. Defaults match the homelab deployment: in-cluster
ClusterIP, mock payments, one pinned guest image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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
    # Above the controller's own ~90s provisioning timeout, so the platform
    # decides a cold start failed and refunds, instead of the client giving up
    # on a session that is about to become ready.
    create_timeout_seconds: float = 150.0
    request_timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SandboxConfig:
        source = dict(os.environ if env is None else env)
        allow = source.get("SANDBOX_ENV_ALLOWLIST", "")
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
        return problems

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
