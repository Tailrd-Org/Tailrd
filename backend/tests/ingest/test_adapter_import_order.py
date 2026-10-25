"""Every shipped adapter stays registered whatever is imported first.

Regression: an adapter module imported before services.ats_scraper (a test
importing it directly, a script) was dropped from the registry for the rest
of the process. The adapter imports ATSJob from the scraper; the scraper's
import loaded the company registry, which asked the adapter registry which
platforms are enabled; that discovery found the adapter half-loaded, without
PLATFORM yet, skipped it and cached the result. Each case runs in a fresh
interpreter, so the adapter really is the first thing imported.
"""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.ingest import adapters

REPO_ROOT = Path(__file__).resolve().parents[3]

# The adapter is imported before anything else; the registry is replaced by
# one enabled board on its platform (patching company_registry imports nothing
# else), so the probe holds whatever the shipped registry lists.
PROBE = """
import importlib, json, sys
module_name, platform = sys.argv[1], sys.argv[2]
from backend.data import company_registry
board = {"company_name": "Probe", "ats_platform": platform, "board_slug": "probe", "enabled": True}
company_registry._load_raw = lambda: [board]
module = importlib.import_module(module_name)
from backend.ingest import adapters
print(json.dumps({
    "registered": adapters.get_adapter(platform) is module,
    "listed": (platform, "probe", "Probe") in company_registry.load_companies(),
}))
"""

SHIPPED = sorted((platform, adapters.get_adapter(platform).__name__)
                 for platform in adapters.adapter_platforms())


@pytest.mark.parametrize("platform,module_name", SHIPPED)
def test_an_adapter_imported_first_stays_registered_and_its_boards_listed(platform, module_name):
    module = importlib.import_module(module_name)
    keys = {name: "probe-key" for name in getattr(module, "REQUIRED_ENV", ()) or ()}
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), **keys)

    result = subprocess.run([sys.executable, "-c", PROBE, module_name, platform],
                            capture_output=True, text=True, env=env, timeout=120)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"registered": True, "listed": True}
