"""Tests for dashboard job filter helpers."""

import datetime

from backend.ingest.taxonomy import FAMILIES, LEGACY_CATEGORY_TO_FAMILIES, LEVELS
from backend.services.job_filters import (
    DEFAULT_EXPERIENCE_FILTER,
    EXPERIENCE_FILTER_MAP,
    date_posted_cutoff,
    expand_experience_filter_values,
    legacy_categories_for_families,
    parse_career_families,
    user_experience_levels,
)


def test_experience_filter_map_is_exactly_the_plan():
    """Constraint 11: the six UI options, the two legacy keys, and executive."""
    assert EXPERIENCE_FILTER_MAP == {
        "intern_new_grad": ["internship", "new_grad"],
        "entry": ["entry"],
        "mid": ["mid"],
        "senior": ["senior"],
        "lead": ["lead"],
        "director": ["director", "executive"],
        "internship": ["internship"],
        "new_grad": ["new_grad", "entry"],
        "executive": ["executive"],
    }
    assert DEFAULT_EXPERIENCE_FILTER == ("intern_new_grad", "entry")


def test_every_expansion_is_a_stored_level_and_every_level_is_reachable():
    stored = {level for levels in EXPERIENCE_FILTER_MAP.values() for level in levels}
    assert stored == set(LEVELS)


def test_expand_experience_intern_new_grad():
    assert expand_experience_filter_values(["intern_new_grad"]) == ["internship", "new_grad"]


def test_expand_experience_six_options_cover_every_level():
    options = ["intern_new_grad", "entry", "mid", "senior", "lead", "director"]
    assert expand_experience_filter_values(options) == sorted(LEVELS)


def test_expand_experience_legacy_values():
    # The old "New Grad / Entry Level" option keeps covering entry level.
    assert expand_experience_filter_values(["new_grad"]) == ["entry", "new_grad"]
    assert expand_experience_filter_values(["internship"]) == ["internship"]
    # Deliberate change: the old "entry" meant internship plus new grad.
    assert expand_experience_filter_values(["entry"]) == ["entry"]


def test_expand_experience_is_case_and_space_tolerant_and_unknown_keys_stay_narrow():
    assert expand_experience_filter_values([" Director ", ""]) == ["director", "executive"]
    assert expand_experience_filter_values(["foo"]) == ["foo"]


def test_user_levels_default_to_intern_new_grad_and_entry():
    default = ["entry", "internship", "new_grad"]
    assert user_experience_levels(None) == default
    assert user_experience_levels("") == default
    assert user_experience_levels([]) == default
    # Nothing recognized is no choice at all, never a filter that matches nothing.
    assert user_experience_levels("Senior Level,???") == default


def test_user_levels_expand_saved_choices():
    assert user_experience_levels("intern_new_grad") == ["internship", "new_grad"]
    assert user_experience_levels("director") == ["director", "executive"]
    assert user_experience_levels("mid, Senior") == ["mid", "senior"]
    assert user_experience_levels(["lead"]) == ["lead"]
    # What onboarding wrote for existing users before the six options.
    assert user_experience_levels("internship") == ["internship"]
    assert user_experience_levels("new_grad") == ["entry", "new_grad"]
    # An unknown value next to a known one is ignored.
    assert user_experience_levels("bogus,entry") == ["entry"]


def test_parse_career_families():
    assert parse_career_families(None) == []
    assert parse_career_families("") == []
    assert parse_career_families(" Legal ,software_engineering,,legal") == ["legal", "software_engineering"]


def test_legacy_categories_for_families_inverts_the_legacy_map():
    assert legacy_categories_for_families(["software_engineering"]) == ["Software Engineering"]
    assert legacy_categories_for_families(["data_ai_analytics"]) == [
        "Business Analyst", "Data Analysis", "Machine Learning and AI",
    ]
    assert legacy_categories_for_families(["it_security_infrastructure"]) == [
        "Engineering and Development",
    ]
    assert legacy_categories_for_families(["other"]) == ["Consultant", "Other"]
    # Families with no old job function behind them have no fallback.
    assert legacy_categories_for_families(["healthcare_clinical"]) == []
    assert legacy_categories_for_families([]) == []


def test_every_legacy_category_reaches_a_family():
    for category, families in LEGACY_CATEGORY_TO_FAMILIES.items():
        assert families and set(families) <= set(FAMILIES), category
        for family in families:
            assert category in legacy_categories_for_families([family])


def test_date_posted_cutoff_24h():
    cutoff = date_posted_cutoff("24h")
    assert cutoff is not None
    delta = datetime.datetime.utcnow() - cutoff
    assert datetime.timedelta(hours=23, minutes=30) < delta < datetime.timedelta(hours=24, minutes=30)


def test_date_posted_cutoff_week():
    cutoff = date_posted_cutoff("week")
    assert cutoff is not None
    delta = datetime.datetime.utcnow() - cutoff
    assert datetime.timedelta(days=6, hours=23) < delta < datetime.timedelta(days=7, hours=1)
