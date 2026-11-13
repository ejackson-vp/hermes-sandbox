"""Terminal-environment provider registration for the x402 sandbox.

The provider is the whole integration point: Hermes selects it with
``terminal.backend: sandbox`` and every shell command, file tool call and code
execution then runs in a leased microVM instead of on the host.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .client import SandboxClient
from .config import SandboxConfig
from .environment import SandboxEnvironment
from .errors import SandboxError

logger = logging.getLogger(__name__)

#: Never let a model-authored command read these. Hermes strips them from every
#: subprocess even though the guest never receives the host environment: this is
#: the belt to that braces, and it also covers any future passthrough mistake.
STRIP_ENV_KEYS = frozenset(
    {
        "SANDBOX_API_URL",
        "GRAPHN_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CONTROLLER_ADMIN_TOKEN",
        "ADMIN_TOKEN",
        "CAPABILITY_SECRET",
        "SIGNAL_ACCOUNT",
        "SIGNAL_HTTP_URL",
        "SIGNAL_ALLOWED_USERS",
        "SIGNAL_GROUP_ALLOWED_USERS",
        "SIGNAL_HOME_CHANNEL",
        "SIGNAL_ALLOW_ALL_USERS",
        "EVM_PRIVATE_KEY",
        # The payer key signs the mock x402 payment. A model-authored command
        # must never see it, even though the guest gets no host environment.
        "HERMES_PAYER_KEY",
    }
)


class SandboxProvider(TerminalEnvironmentProvider):
    """Runs Hermes commands in the x402 sandbox service."""

    name = "sandbox"
    display_name = "x402 Sandbox"

    #: Commands do not run on this host, and the guest has its own root filesystem.
    is_remote = True
    is_container = True

    #: The sandbox is isolated, but requirement 11 keeps the approval layer on
    #: for consequential commands. is_container defaults this to True; overriding
    #: it explicitly is the difference between "isolated" and "unsupervised".
    @property
    def skip_container_guards(self) -> bool:
        return False

    #: Each non-persistent session gets its own sandbox. Without this, two
    #: ephemeral Hermes runs could attach one lease and destroy it under each other.
    session_isolated_when_nonpersistent = True

    #: Screens and `computer_use` need a persistent duplex channel into the
    #: guest, which the request/response exec API cannot provide. Refuse rather
    #: than pretend.
    supports_streams = False

    @property
    def description(self) -> str:
        try:
            cfg = self._config()
        except Exception:  # noqa: BLE001 - a picker paint must never raise
            return "Run commands in an x402 sandbox (not configured)."
        return (
            f"Run commands in an isolated Linux microVM leased from {cfg.api_url}. "
            f"Tier {cfg.tier}, image {cfg.image}."
        )

    @property
    def default_cwd(self) -> str:
        return "/workspace"

    @property
    def strip_env_keys(self) -> frozenset:
        return STRIP_ENV_KEYS

    @property
    def env_description(self) -> str:
        return "an isolated Linux microVM (the x402 sandbox)"

    # ------------------------------------------------------------- availability

    def _config(self) -> SandboxConfig:
        return SandboxConfig.from_env()

    def is_available(self) -> bool:
        """Cheap and offline: config parses, the HTTP client imports.

        Deliberately does not contact the API. Hermes calls this on every picker
        paint, and a network call here would turn a paused homelab into a hung UI.
        """
        try:
            return not self._config().validate()
        except Exception:  # noqa: BLE001
            return False

    def check_requirements(self, config: dict[str, Any]) -> bool:
        problems = self._config().validate()
        for problem in problems:
            logger.error("sandbox backend misconfigured: %s", problem)
        return not problems

    def probe(self) -> tuple[str, str]:
        """Picker health. Offline, per the contract; ``doctor_checks`` is the live probe."""
        problems = self._config().validate()
        if problems:
            return ("needs_setup", problems[0])
        return ("ready", "")

    def setup_instructions(self) -> list[str]:
        return [
            "Set SANDBOX_API_URL to the sandbox API, e.g.",
            "  http://api.sandbox-system.svc.cluster.local:3000   (in-cluster)",
            "  https://sandbox-api.<tailnet>.ts.net              (from outside)",
            "Optional: SANDBOX_TIER (default standard), SANDBOX_IMAGE (default guest),",
            "  SANDBOX_MAX_COMMAND_SECONDS (default 300).",
            "Optional: HERMES_PAYER_KEY, a 32-byte hex private key, so the paying",
            "  address is stable and can be allowlisted in the sandbox.",
            "Verify with: python -m hermes_sandbox.smoke",
        ]

    # -------------------------------------------------------------- diagnostics

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        cfg = self._config()
        rows: list[tuple[bool, str, str]] = []
        problems = cfg.validate()
        rows.append((not problems, "sandbox config", "; ".join(problems) or f"api={cfg.api_url} tier={cfg.tier}"))
        # The address is public and is the thing an operator has to allowlist, so
        # it is the useful half of this setting to print.
        if cfg.payer_key is not None:
            rows.append(
                (
                    True,
                    "sandbox payer",
                    f"stable address {cfg.payer_key.address} (it must be on the sandbox allowlist)",
                )
            )
        elif cfg.payer_key_error:
            rows.append((False, "sandbox payer", cfg.payer_key_error))
        else:
            rows.append((True, "sandbox payer", "ephemeral per session (HERMES_PAYER_KEY is not set)"))

        if importlib.util.find_spec("nacl") is None:
            rows.append((False, "sandbox crypto", "PyNaCl is not installed"))
        if importlib.util.find_spec("eth_account") is None:
            rows.append((False, "sandbox payments", "eth-account is not installed"))

        client: SandboxClient | None = None
        try:
            client = SandboxClient(cfg.api_url, timeout_seconds=10, verify_tls=cfg.verify_tls)
            images = client.list_images()
            ids = [image.get("id") for image in images]
            rows.append((True, "sandbox API", f"reachable at {cfg.api_url}, images={ids}"))
            if cfg.image in ids:
                rows.append((True, "sandbox image", f"{cfg.image} is offered by the platform"))
            else:
                rows.append(
                    (
                        False,
                        "sandbox image",
                        f"{cfg.image!r} is not offered by this deployment; available: {ids or 'none'}",
                    )
                )
        except SandboxError as exc:
            rows.append((False, "sandbox API", f"unreachable at {cfg.api_url}: {exc.code}"))
        except Exception as exc:  # noqa: BLE001
            rows.append((False, "sandbox API", f"unreachable at {cfg.api_url}: {exc}"))
        finally:
            if client is not None:
                client.close()
        return rows

    # ------------------------------------------------------------- environment

    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: str | None = None,
        container_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SandboxEnvironment:
        cfg = self._config()
        problems = cfg.validate()
        if problems:
            raise SandboxError("backend_misconfigured", "; ".join(problems))
        # A per-task image override is honoured only if the platform offers it.
        # The API has no image field today, so an unknown value must fail here
        # rather than silently run the pinned image under a different name.
        requested = image or cfg.image
        env = SandboxEnvironment(
            config=cfg,
            cwd=cwd or self.default_cwd,
            timeout=timeout,
            task_id=task_id,
            image=requested,
        )
        logger.info(
            "sandbox environment created: task=%s cwd=%s image=%s tier=%s api=%s payer=%s",
            task_id,
            env.cwd,
            requested,
            cfg.tier,
            cfg.api_url,
            cfg.payer_key.address if cfg.payer_key else "ephemeral",
        )
        return env


def register(ctx: Any) -> None:
    ctx.register_terminal_environment_provider(SandboxProvider())
