"""
Source adapter registry (scraper v2).

Every module in this package that defines ``PLATFORM`` and ``list_jobs`` is the
adapter for that platform: the ``ats_platform`` value of registry entries in
backend/data/ats_companies.json. The contract (docs/scraper-v2/PLAN.md, global
constraint 13):

  PLATFORM       the registry ats_platform value
  TIER           1 to 4
  COUNTRIES      tuple of the countries it covers
  RATE_LIMIT     seconds between requests to one host
  REQUIRED_ENV   env var names; the adapter is disabled while any is unset
  HOST_PATTERNS  regexes for the posting URLs it owns
  async list_jobs(client, slug, company_name, *, config, deadline=None)
      -> (list[ATSJob], complete, total): every listing, unfiltered
  async fetch_job(client, slug, ref, *, config)
      -> {"description": str, "employment_type": str}          (optional)
  async check_alive(client, url) -> platform_liveness.LivenessResult  (optional)

Adapters are found on first use, never at import: ATSScraper.scrape_board
sends a platform outside its five built-ins here, company_registry.
load_companies accepts a board whose adapter is enabled, and platform_liveness
asks the adapter that owns a URL before falling back to the page itself. A
module that fails to import is logged and skipped, and never breaks the import
of anything else. Modules named with a leading underscore are helpers, never
adapters. Nothing may ask the registry at import time (adapters import
services.ats_scraper, so an import-time question would run discovery while
the adapter imported first is still half-loaded); if something does, that
discovery answers without the half-loaded adapter and is not cached, so no
adapter drops out for the rest of the process.

Tests swap in fakes by replacing ``_registry`` (platform -> module, or any
object with the same attributes), e.g. ``monkeypatch.setattr(adapters,
"_registry", {"fakeats": fake})``.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import re
import threading
from functools import lru_cache
from types import ModuleType
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# platform -> adapter module, discovered on first use (None until then).
_registry: Optional[dict[str, ModuleType]] = None
# The adapters found so far, while this thread is discovering them.
_discovering = threading.local()


def _adapters() -> dict[str, ModuleType]:
    global _registry
    if _registry is not None:
        return _registry
    partial = getattr(_discovering, "found", None)
    if partial is not None:
        # Re-entered from inside the import of an adapter this discovery is
        # loading (its import-time code asked the registry): answer with what
        # is loaded so far; the outer discovery decides what to cache.
        return partial
    found: dict[str, ModuleType] = {}
    complete = True
    _discovering.found = found
    try:
        for info in sorted(pkgutil.iter_modules(__path__), key=lambda module: module.name):
            if info.name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"{__name__}.{info.name}")
            except Exception:
                logger.exception("Adapter module %s failed to import; skipped", info.name)
                continue
            if _still_loading(module):
                # Imported first, and something its own import runs asked the
                # registry: its PLATFORM may not exist yet. Leave it out of
                # this answer and cache nothing, so the next call, made once
                # it has loaded, finds it.
                complete = False
                continue
            platform = getattr(module, "PLATFORM", None)
            if (not isinstance(platform, str) or not platform.strip()
                    or not callable(getattr(module, "list_jobs", None))):
                continue
            platform = platform.strip().lower()
            if platform in found:
                logger.error("Adapters %s and %s both claim %s; keeping the first",
                             found[platform].__name__, module.__name__, platform)
                continue
            found[platform] = module
    finally:
        _discovering.found = None
    if complete:
        _registry = found
    return found


def _still_loading(module: ModuleType) -> bool:
    """Whether the module's own import is still running. importlib flags the
    module's spec while it executes; it is the flag CPython reads to call a
    module "partially initialized" in import errors."""
    return bool(getattr(getattr(module, "__spec__", None), "_initializing", False))


def adapter_platforms() -> list[str]:
    """Every platform an adapter claims, enabled or not, sorted."""
    return sorted(_adapters())


def get_adapter(platform: str) -> Optional[ModuleType]:
    """The adapter for a registry platform, enabled or not; None when no
    adapter claims it."""
    return _adapters().get((platform or "").strip().lower())


def adapter_enabled(platform: str) -> bool:
    """Whether ``platform`` has an adapter and every variable in its
    REQUIRED_ENV is set to something. Read from the environment on every
    call, so a key added to the deployment turns the adapter on."""
    adapter = get_adapter(platform)
    if adapter is None:
        return False
    required = getattr(adapter, "REQUIRED_ENV", ()) or ()
    if isinstance(required, str):
        required = (required,)
    return all((os.environ.get(name) or "").strip() for name in required)


def adapter_for_url(url: str) -> Optional[ModuleType]:
    """The first adapter (in module-name order) with a HOST_PATTERNS regex
    that matches ``url``, enabled or not; None when none does. Each regex is
    searched case-insensitively in the whole URL and in its host alone, so a
    URL pattern ("apply\\.workable\\.com/") and an anchored host pattern
    ("^[\\w-]+\\.recruitee\\.com$") both work."""
    url = (url or "").strip()
    if not url:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    for adapter in _adapters().values():
        patterns = getattr(adapter, "HOST_PATTERNS", ()) or ()
        if isinstance(patterns, (str, re.Pattern)):
            patterns = (patterns,)
        for pattern in patterns:
            compiled = _compile(pattern)
            if compiled is not None and (compiled.search(url) or (host and compiled.search(host))):
                return adapter
    return None


@lru_cache(maxsize=None)
def _compile(pattern) -> Optional[re.Pattern]:
    if isinstance(pattern, re.Pattern):
        return pattern
    try:
        return re.compile(pattern, re.IGNORECASE)
    except (re.error, TypeError) as exc:
        logger.error("Bad adapter HOST_PATTERNS entry %r: %s", pattern, exc)
        return None
