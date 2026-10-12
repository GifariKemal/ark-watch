"""The test environment itself: no network, no ambient credentials."""

import re
import socket
from pathlib import Path

import pytest

from tests import conftest

ROOT = Path(__file__).resolve().parents[2]


def test_network_is_blocked_loopback_is_not():
    with pytest.raises(RuntimeError, match="reach the network"):
        socket.getaddrinfo("api.stlouisfed.org", 443)
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_every_env_var_the_code_reads_is_scrubbed():
    """A new os.environ read in arkwatch/ must be added to conftest's scrub list, or a dev
    shell's real key leaks into the suite."""
    names = set()
    for f in (ROOT / "arkwatch").rglob("*.py"):
        names |= set(
            re.findall(
                r"(?:environ\.get|getenv|environ)\(?\[?\s*[\"']([A-Z][A-Z0-9_]+)[\"']",
                f.read_text(encoding="utf-8"),
            )
        )
    not_secret = {"GIT_SHA", "PYTEST_CURRENT_TEST"}
    missing = {
        n
        for n in names - not_secret
        if n not in conftest._NAMES and not n.startswith(conftest._PREFIXES)
    }
    assert not missing, f"add to tests/conftest.py _NAMES: {sorted(missing)}"
