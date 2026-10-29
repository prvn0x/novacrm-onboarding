"""Fault switches for the live demo: make Rocketlane, Slack or the voice platform "down" on
camera, then bring it back, without touching the network.

    novacrm fault rocketlane down     # every Rocketlane call fails as a 503 until...
    novacrm fault rocketlane up

The running agent reads var/faults.json on every call, so the switch takes effect immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ports import TransientError

FAULTS_FILE = Path("var/faults.json")
SYSTEMS = ("rocketlane", "slack", "voice", "gmail")


def read() -> dict[str, str]:
    try:
        data = json.loads(FAULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def set_fault(system: str, state: str) -> None:
    if system not in SYSTEMS or state not in ("down", "up"):
        raise SystemExit(f"usage: novacrm fault {{{'|'.join(SYSTEMS)}}} {{down|up}}")
    faults = read()
    if state == "down":
        faults[system] = "down"
    else:
        faults.pop(system, None)
    FAULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAULTS_FILE.write_text(json.dumps(faults), encoding="utf-8")


class Switchable:
    """Wraps an adapter: while its system is switched "down", every call raises TransientError,
    exactly as a real 503 would, so the agents' outage handling is what the video shows."""

    def __init__(self, system: str, inner: Any) -> None:
        self._system = system
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if read().get(self._system) == "down":
                raise TransientError(self._system, "503 Service Unavailable (demo fault switch)")
            return attr(*args, **kwargs)

        return call
