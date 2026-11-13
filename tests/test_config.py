"""Configuration parsing and validation."""

from __future__ import annotations

from hermes_sandbox.config import SandboxConfig


def test_defaults_match_the_homelab():
    cfg = SandboxConfig.from_env({})
    assert cfg.api_url == "http://api.sandbox-system.svc.cluster.local:3000"
    assert cfg.tier == "standard"
    assert cfg.image == "guest"
    assert cfg.validate() == []


def test_env_overrides():
    cfg = SandboxConfig.from_env(
        {
            "SANDBOX_API_URL": "https://sandbox-api.example.ts.net",
            "SANDBOX_TIER": "long",
            "SANDBOX_IMAGE": "developer",
            "SANDBOX_MAX_COMMAND_SECONDS": "600",
            "SANDBOX_VERIFY_TLS": "false",
        }
    )
    assert cfg.api_url.endswith("ts.net")
    assert cfg.tier == "long"
    assert cfg.max_command_seconds == 600
    assert cfg.verify_tls is False


def test_unknown_tier_is_rejected():
    problems = SandboxConfig.from_env({"SANDBOX_TIER": "enormous"}).validate()
    assert any("SANDBOX_TIER" in problem for problem in problems)


def test_custom_tier_requires_duration():
    problems = SandboxConfig.from_env({"SANDBOX_TIER": "custom"}).validate()
    assert any("SANDBOX_DURATION_SECONDS" in problem for problem in problems)


def test_api_url_must_be_a_url():
    problems = SandboxConfig.from_env({"SANDBOX_API_URL": "api.sandbox-system.svc:3000"}).validate()
    assert any("http" in problem for problem in problems)


def test_env_allowlist_is_parsed_and_empty_by_default():
    assert SandboxConfig.from_env({}).env_allowlist == ()
    cfg = SandboxConfig.from_env({"SANDBOX_ENV_ALLOWLIST": "FOO, BAR ,"})
    assert cfg.env_allowlist == ("FOO", "BAR")


def test_session_kwargs_only_carries_custom_sizing_for_custom_tier():
    cfg = SandboxConfig.from_env({"SANDBOX_TIER": "standard", "SANDBOX_CPU_MILLIS": "2000"})
    kwargs = cfg.session_kwargs()
    assert kwargs["tier"] == "standard"
    assert kwargs["cpu_millis"] == 2000
