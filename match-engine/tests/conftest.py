"""Shared fixtures for the engine test suite."""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, settings

from matcher.config import EngineConfig, load_config

# Property tests do real work per example; wall-clock deadlines only make them flaky.
settings.register_profile("engine", deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "engine"))


@pytest.fixture(scope="session")
def config() -> EngineConfig:
    return load_config()
