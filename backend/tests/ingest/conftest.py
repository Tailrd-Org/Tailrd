"""Shared setup for the scraper v2 tests.

A test that makes real network requests carries ``@pytest.mark.network`` and
is skipped unless ``RUN_NETWORK_TESTS=1``: CI and everyday runs never depend
on a live job board (docs/scraper-v2/PLAN.md, global constraint 6).
"""

import os

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "network: makes real network requests; runs only with RUN_NETWORK_TESTS=1",
    )


def pytest_runtest_setup(item):
    if item.get_closest_marker("network") and os.environ.get("RUN_NETWORK_TESTS") != "1":
        pytest.skip("makes real network requests; set RUN_NETWORK_TESTS=1 to run")
