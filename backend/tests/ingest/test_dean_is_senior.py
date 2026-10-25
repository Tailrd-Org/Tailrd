"""A dean is a senior post. "Associate Dean" used to read as the entry-level
"Associate" (the weak tier) because 'dean' was missing from the hard senior
words; 'Dean's Office' names an office, not a rank, and stays as it was."""

import pytest

from backend.ingest import taxonomy
from backend.services.ats_scraper import HARD_SENIOR, entry_tier


@pytest.fixture(autouse=True)
def setup_db():
    """No database here: this module skips the root conftest's per-test
    create_all/drop_all."""
    yield


@pytest.mark.parametrize("title", [
    "Associate Dean",
    "Associate Dean, School of Nursing",
    "Assistant Dean of Students",
    "Dean of Engineering",
    "Dean",
    "Associate Dean - Faculty of Science",
])
def test_a_dean_is_not_entry_level(title):
    assert HARD_SENIOR.search(title)
    assert entry_tier(title) is None
    assert taxonomy.classify_experience(title) == "director"


@pytest.mark.parametrize("title", [
    "Dean's Office Assistant",
    "Administrative Assistant - Dean's Office, College of IST",
    "Dean’s Office Coordinator",
])
def test_a_deans_office_is_not_a_rank(title):
    assert not HARD_SENIOR.search(title)
    assert taxonomy.classify_experience(title) == "entry"


def test_a_deans_office_intern_is_still_a_student_role():
    assert entry_tier("Dean's Office Intern") == "strong"
    assert taxonomy.classify_experience("Dean's Office Intern") == "internship"
