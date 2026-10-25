"""ingest/adapters: discovering adapter modules, telling an enabled adapter
from one missing its API keys, and finding the adapter that owns a URL.

The adapter package is pointed at a temporary directory of real modules, so
discovery runs exactly as it does over the shipped adapters."""

import importlib
import logging
import sys
import textwrap

import pytest

from backend.ingest import adapters

PACKAGE = adapters.__name__

LIST_JOBS = """
async def list_jobs(client, slug, company_name, *, config, deadline=None):
    return [], True, 0
"""


def _source(body: str, list_jobs: bool = True) -> str:
    return textwrap.dedent(body) + (LIST_JOBS if list_jobs else "")


MODULES = {
    "fake_good": _source(r"""
        PLATFORM = " FakeATS "
        TIER = 1
        COUNTRIES = ("CA", "US")
        RATE_LIMIT = 1.0
        REQUIRED_ENV = ()
        HOST_PATTERNS = (r"https://jobs\.fakeats\.test/",)
    """),
    "fake_keyed": _source(r"""
        PLATFORM = "keyedats"
        REQUIRED_ENV = ("T1_FAKE_KEY_A", "T1_FAKE_KEY_B")
        HOST_PATTERNS = (r"^[\w-]+\.keyed\.test$",)
    """),
    "fake_broken": _source("""
        raise RuntimeError("this adapter is broken")
    """, list_jobs=False),
    "fake_helper": _source("""
        def shared_helper():
            return 1
    """, list_jobs=False),
    "fake_nolist": _source("""
        PLATFORM = "nolist"
    """, list_jobs=False),
    "_fake_private": _source("""
        PLATFORM = "private"
    """),
    # An adapter whose import asks the registry (as services.ats_scraper's
    # import-time registry load does): it must see the adapters found so far,
    # not start a second discovery.
    "fake_reentrant": _source("""
        from backend.ingest import adapters
        SEEN_DURING_IMPORT = adapters.adapter_platforms()
        PLATFORM = "reentrant"
    """),
    "fake_twin": _source("""
        PLATFORM = "fakeats"
    """),
}


@pytest.fixture
def adapter_package(tmp_path, monkeypatch):
    """The adapter package, served from a fresh directory holding MODULES,
    with nothing discovered yet."""
    directory = tmp_path / "adapters"
    directory.mkdir()
    for name, source in MODULES.items():
        (directory / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(adapters, "__path__", [str(directory)])
    monkeypatch.setattr(adapters, "_registry", None)
    importlib.invalidate_caches()
    yield directory
    for name in [*MODULES, "fake_late"]:
        sys.modules.pop(f"{PACKAGE}.{name}", None)


def _module(name):
    return sys.modules[f"{PACKAGE}.{name}"]


def test_discovery_finds_every_module_with_platform_and_list_jobs(adapter_package, caplog):
    with caplog.at_level(logging.ERROR, logger=PACKAGE):
        platforms = adapters.adapter_platforms()

    assert platforms == ["fakeats", "keyedats", "reentrant"]
    assert adapters.get_adapter("FAKEATS ") is _module("fake_good")
    assert adapters.get_adapter("keyedats") is _module("fake_keyed")
    # No list_jobs, a helper module, a private module, nothing at all.
    for platform in ("nolist", "private", "nosuchats", ""):
        assert adapters.get_adapter(platform) is None
    # The second claim on a platform loses, and says so.
    assert any("fake_twin" in r.getMessage() and "fake_good" in r.getMessage()
               for r in caplog.records)


def test_a_broken_adapter_is_logged_once_and_skipped(adapter_package, caplog):
    with caplog.at_level(logging.ERROR, logger=PACKAGE):
        assert "fakeats" in adapters.adapter_platforms()

    broken = [r for r in caplog.records if "fake_broken" in r.getMessage()]
    assert len(broken) == 1
    assert broken[0].exc_info and "this adapter is broken" in str(broken[0].exc_info[1])


def test_an_adapter_asking_the_registry_during_discovery_sees_what_is_loaded(adapter_package):
    assert adapters.adapter_platforms() == ["fakeats", "keyedats", "reentrant"]
    assert _module("fake_reentrant").SEEN_DURING_IMPORT == ["fakeats", "keyedats"]


def test_discovery_runs_once(adapter_package):
    assert adapters.adapter_platforms() == ["fakeats", "keyedats", "reentrant"]
    (adapter_package / "fake_late.py").write_text(_source('PLATFORM = "late"\n'), encoding="utf-8")

    assert adapters.get_adapter("late") is None


def test_an_adapter_is_enabled_only_with_every_required_env_var(adapter_package, monkeypatch):
    monkeypatch.delenv("T1_FAKE_KEY_A", raising=False)
    monkeypatch.delenv("T1_FAKE_KEY_B", raising=False)

    assert adapters.adapter_enabled("fakeats") is True  # needs no key
    assert adapters.adapter_enabled("keyedats") is False
    monkeypatch.setenv("T1_FAKE_KEY_A", "a")
    assert adapters.adapter_enabled("keyedats") is False
    monkeypatch.setenv("T1_FAKE_KEY_B", "   ")  # blank is missing
    assert adapters.adapter_enabled("keyedats") is False
    monkeypatch.setenv("T1_FAKE_KEY_B", "b")
    assert adapters.adapter_enabled("keyedats") is True
    assert adapters.adapter_enabled("nosuchats") is False


def test_adapter_for_url_matches_url_and_host_patterns(adapter_package):
    good, keyed = adapters.get_adapter("fakeats"), adapters.get_adapter("keyedats")
    assert good is _module("fake_good") and keyed is _module("fake_keyed")

    assert adapters.adapter_for_url("https://jobs.fakeats.test/acme/123") is good
    assert adapters.adapter_for_url("HTTPS://JOBS.FAKEATS.TEST/acme/123") is good
    # An anchored host pattern is matched against the host alone.
    assert adapters.adapter_for_url("https://acme.keyed.test/o/engineer") is keyed
    assert adapters.adapter_for_url("https://keyed.test.example.com/o/engineer") is None
    for url in ("https://example.com/jobs/1", "", "not a url"):
        assert adapters.adapter_for_url(url) is None
