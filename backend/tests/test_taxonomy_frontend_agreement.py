"""The frontend's copy of the career families, the legacy job-function map and the
experience options must agree with the backend (ingest/taxonomy.py, services/job_filters.py).

The Jobs page filter bar and onboarding read them from
frontend/src/lib/jobFilterOptions.ts; this parses that file rather than importing it.
"""

import re
from pathlib import Path

import pytest

from backend.ingest.taxonomy import FAMILIES, LEGACY_CATEGORY_TO_FAMILIES
from backend.services.job_filters import (
    DEFAULT_EXPERIENCE_FILTER,
    EXPERIENCE_FILTER_MAP,
    EXPERIENCE_OPTION_KEYS,
)

OPTIONS_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "jobFilterOptions.ts"


@pytest.fixture(scope="module")
def source() -> str:
    return OPTIONS_TS.read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    """The text between ``start`` and the first ``end`` after it."""
    begin = source.index(start) + len(start)
    return source[begin:source.index(end, begin)]


def _options(source: str, name: str) -> list[tuple[str, str]]:
    block = _block(source, f"export const {name} = [", "];")
    return re.findall(r'\{\s*value:\s*"([^"]+)",\s*label:\s*"([^"]+)"\s*\}', block)


def _string_list(source: str, name: str) -> list[str]:
    return re.findall(r'"([^"]+)"', _block(source, f"export const {name} = [", "];"))


def test_career_families_match_the_taxonomy_in_order(source):
    assert _options(source, "CAREER_FAMILY_OPTIONS") == list(FAMILIES.items())


def test_legacy_job_function_map_matches_the_taxonomy(source):
    block = _block(source, "export const LEGACY_CATEGORY_TO_FAMILIES: Record<string, string[]> = {", "};")
    parsed = {
        category: re.findall(r'"([^"]+)"', families)
        for category, families in re.findall(r'"([^"]+)":\s*\[([^\]]*)\]', block)
    }
    assert parsed == LEGACY_CATEGORY_TO_FAMILIES
    assert len(parsed) == 17


def test_experience_options_are_keys_of_the_backend_filter_map(source):
    options = _options(source, "EXPERIENCE_OPTIONS")
    assert [value for value, _ in options] == [
        "intern_new_grad", "entry", "mid", "senior", "lead", "director",
    ]
    assert all(value in EXPERIENCE_FILTER_MAP for value, _ in options)
    # GET /jobs/levels answers in these keys, in this order.
    assert [value for value, _ in options] == list(EXPERIENCE_OPTION_KEYS)
    assert _string_list(source, "DEFAULT_EXPERIENCE_LEVELS") == list(DEFAULT_EXPERIENCE_FILTER)
