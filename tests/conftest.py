"""Shared helpers.

Tests that need Hermes itself (the provider and environment import
``agent.*`` and ``tools.*``) are skipped unless ``HERMES_SRC`` points at a
Hermes checkout. The HTTP client, crypto and payment tests never need it.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERMES_SRC = os.environ.get("HERMES_SRC", "")


def add_hermes_path() -> bool:
    """Put a Hermes checkout on sys.path. Returns False when there is none."""
    if not HERMES_SRC:
        return False
    path = Path(HERMES_SRC).expanduser()
    if not (path / "agent" / "provider_base.py").is_file():
        return False
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return True


requires_hermes = pytest.mark.skipif(
    not add_hermes_path(),
    reason="set HERMES_SRC to a hermes-agent checkout to run backend tests",
)


def iso_in(seconds: int) -> str:
    """An ISO timestamp *seconds* from now, in the format the API emits."""
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
