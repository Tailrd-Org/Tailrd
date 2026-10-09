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


@pytest.fixture(scope="session")
def small_graph():
    """The committed tests/fixtures/graph_small artifacts, loaded (and hash-checked) once."""
    from matcher.graph.loader import load_graph
    from tests.fixtures.build_graph_small import FIXTURE_DIR

    return load_graph(FIXTURE_DIR)
