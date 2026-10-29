"""Test fixtures: the sandbox world (src/novacrm/sandbox.py), fresh for every test."""

from __future__ import annotations

import pytest

from novacrm.sandbox import (  # noqa: F401 - re-exported for the test modules
    AE,
    DEDICATED_CSMS,
    ENTERPRISE_TEMPLATE,
    GROWTH_TEMPLATE,
    OPP_15,
    OPP_18,
    OPP_LINK,
    OTHER_AE,
    POOLED_CSM,
    TEAM,
    World,
    careful_extraction,
    careful_transcript_reading,
    deal_email,
)


@pytest.fixture
def world() -> World:
    return World()
