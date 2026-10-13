"""Hypothesis strategies for valid `Resume` / `JobPosting` models over the graph_small ids (SPEC §10,
§13), plus the garbage the totality test throws at the engine.

The valid strategies cover every field the engine reads and the shapes real data takes: skills the
graph knows and ids it does not, every evidence tier, verb class and polarity, dated, undated,
current, future and imprecise roles, internships, projects, degrees, credentials, NAICS codes at
every depth (and blank ones), all three necessities, OR-groups, overflow weights, every gate kind,
unresolved requirement tokens and informational or review flags. Everything they build passes model
validation, so the engine must score it without a fault (SPEC §13).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from hypothesis import strategies as st

from matcher.errors import NEEDS_REVIEW_REASONS, Flag
from matcher.models import (
    ClearanceGate,
    DatePrecision,
    DateValue,
    DegreeLevel,
    Education,
    EducationRequirement,
    Evidence,
    JobPosting,
    JobRequirement,
    LicenseGate,
    Location,
    LocationGate,
    Necessity,
    ParseTier,
    Polarity,
    Project,
    RemotePolicy,
    Resume,
    Role,
    SeniorityLevel,
    SkillRef,
    SkillSource,
    Track,
    VerbClass,
    WorkAuth,
    WorkAuthGate,
)
from tests.fixtures.build_graph_small import NODES

AS_OF = date(2026, 10, 8)
GRAPH_IDS: tuple[str, ...] = tuple(sorted(node.id for node in NODES))
UNKNOWN_IDS: tuple[str, ...] = ("skill.unknown_alpha", "skill.unknown_beta", "cred.unknown_licence")
FIELD_IDS: tuple[str, ...] = ("field.computer_science", "field.statistics", "skill.python", "field.unknown")
NAICS: tuple[str, ...] = ("5415", "541511", "541512", "5182", "54", "52", "522110", "31-33", "336411", "311",
                          "325", "44", "4521", "6221", "", " ")
COUNTRIES: tuple[str, ...] = ("CA", "US", "GB", "FR")
REGIONS: tuple[str, ...] = ("ON", "BC", "QC", "NY", "CA")
CITIES: tuple[str, ...] = ("toronto", "vancouver", "montreal", "new york")
CLEARANCES: tuple[str, ...] = ("CA_RELIABILITY", "CA_SECRET", "US_SECRET", "US_TOP_SECRET", "ACTIVE_UNKNOWN_LEVEL",
                               "us secret", "XX_ODD")
INFO_FLAGS: tuple[Flag, ...] = tuple(sorted(set(Flag) - NEEDS_REVIEW_REASONS, key=lambda flag: flag.value))
# Review flags a parser can put on an input; the engine's own reasons (faults, invalid input or as_of,
# nothing scorable, low confidence) are outcomes, never input flags.
ENGINE_REASONS = frozenset({Flag.INVALID_INPUT, Flag.INVALID_AS_OF, Flag.NUMERIC_FAULT, Flag.INTERNAL_ERROR,
                            Flag.NOTHING_SCORABLE, Flag.LOW_CONFIDENCE})
REVIEW_FLAGS: tuple[Flag, ...] = tuple(sorted(NEEDS_REVIEW_REASONS - ENGINE_REASONS, key=lambda flag: flag.value))

skill_ids = st.one_of(st.sampled_from(GRAPH_IDS), st.sampled_from(UNKNOWN_IDS))
# Free text never reaches a score (SPEC §9); a few fixed strings, odd ones included, keep generation
# cheap enough for the 50,000-example gate run, with arbitrary text now and then.
TEXTS: tuple[str, ...] = ("", "Python", "Acme Corp", "Senior Engineer II", "École ünï", "​­",
                          "x" * 300)
free_text = st.one_of(st.sampled_from(TEXTS), st.sampled_from(TEXTS), st.sampled_from(TEXTS), st.text(max_size=12))
days = st.dates(min_value=date(1975, 1, 1), max_value=date(2031, 12, 31))

# Every sub-strategy is built once here: a composite that builds strategies on each draw makes
# Hypothesis validate them again every time, which costs more than the engine run itself.
maybe_days = st.none() | days
evidences = st.sampled_from(list(Evidence))
verb_classes = st.sampled_from(list(VerbClass))
polarities = st.sampled_from(list(Polarity))
sources = st.sampled_from(list(SkillSource))
precisions = st.sampled_from(list(DatePrecision))
spreads = st.integers(min_value=0, max_value=200)
booleans = st.booleans()
maybe_booleans = st.none() | st.booleans()
naics_lists = st.lists(st.sampled_from(NAICS), max_size=3).map(tuple)
titles = st.none() | st.sampled_from(("title.software_developer", "title.data_analyst"))
credential_ids = st.sampled_from(GRAPH_IDS[:6] + UNKNOWN_IDS)
credential_lists = st.lists(credential_ids, max_size=2).map(tuple)
country_lists = st.lists(st.sampled_from(COUNTRIES), max_size=2).map(tuple)


@st.composite
def date_values(draw: Any, earliest: date = date(1975, 1, 1)) -> DateValue:
    """A precise date (bounds = value) or an imprecise one with bounds around its value, never
    before `earliest`."""
    value = max(earliest, draw(days))
    precision = draw(precisions)
    if precision in (DatePrecision.DAY, DatePrecision.MONTH):
        return DateValue.exact(value, precision)
    low = max(date(1970, 1, 1), value - timedelta(days=draw(spreads)))
    return DateValue(value=value, earliest=low, latest=value + timedelta(days=draw(spreads)), precision=precision)


maybe_date_values = st.none() | date_values()


@st.composite
def skill_refs(draw: Any, skill: Any = skill_ids) -> SkillRef:
    first, last = draw(maybe_days), draw(maybe_days)
    if first is not None and last is not None and first > last:
        first, last = last, first
    return SkillRef(
        canonical_id=draw(skill),
        surface_form=draw(free_text),
        evidence=draw(evidences),
        verb_class=draw(verb_classes),
        polarity=draw(polarities),
        source=draw(sources),
        first_used=first,
        last_used=last,
    )


role_skills = st.lists(skill_refs(), max_size=6).map(tuple)
project_skills = st.lists(skill_refs(), max_size=4).map(tuple)
blob_skills = st.lists(skill_refs(), max_size=8).map(tuple)
certification_refs = st.lists(skill_refs(credential_ids), max_size=2).map(tuple)

seniority_levels = st.one_of(
    st.builds(SeniorityLevel, track=st.just(Track.IC), level=st.integers(0, 6)),
    st.builds(SeniorityLevel, track=st.just(Track.MGMT), level=st.integers(1, 6)),
)
maybe_levels = st.none() | seniority_levels


@st.composite
def roles(draw: Any) -> Role:
    start = draw(maybe_date_values)
    current = draw(booleans)
    end = None
    if not current and draw(booleans):
        end = draw(date_values(earliest=start.value)) if start is not None else draw(date_values())
    return Role(
        title_raw=draw(free_text),
        title_canonical=draw(titles),
        seniority=draw(maybe_levels),
        employer=draw(free_text),
        industry_codes=draw(naics_lists),
        start=start,
        end=end,
        is_current=current,
        is_internship=draw(booleans),
        skills=draw(role_skills),
    )


projects = st.builds(Project, name=free_text, skills=project_skills, start=maybe_date_values, end=maybe_date_values)

educations = st.builds(
    Education,
    level=st.sampled_from(list(DegreeLevel)),
    field_ids=st.lists(st.sampled_from(FIELD_IDS), max_size=2).map(tuple),
    field_raw=free_text,
    in_progress=booleans,
)

locations = st.builds(
    Location,
    country=st.none() | st.sampled_from(COUNTRIES),
    region=st.none() | st.sampled_from(REGIONS),
    city=st.none() | st.sampled_from(CITIES),
)
location_lists = st.lists(locations, max_size=2).map(tuple)

work_auths = st.builds(
    WorkAuth,
    authorized_countries=country_lists,
    needs_sponsorship=maybe_booleans,
    citizenships=country_lists,
    clearances=st.lists(st.sampled_from(CLEARANCES), max_size=2).map(tuple),
)

_info_flags = st.sampled_from(INFO_FLAGS)
_any_flags = st.one_of(*[_info_flags] * 9, st.sampled_from(REVIEW_FLAGS))
FLAG_LISTS = {
    True: st.lists(_any_flags, max_size=3).map(tuple),  # a review flag now and then
    False: st.lists(_info_flags, max_size=3).map(tuple),
}

role_lists = st.lists(roles(), max_size=3).map(tuple)
project_lists = st.lists(projects, max_size=2).map(tuple)
education_lists = st.lists(educations, max_size=2).map(tuple)
months = st.none() | st.floats(0.0, 1200.0)
maybe_work_auths = st.none() | work_auths
parse_tiers = st.sampled_from(list(ParseTier))


@st.composite
def resumes(draw: Any, review_flags: bool = True) -> Resume:
    return Resume(
        roles=draw(role_lists),
        projects=draw(project_lists),
        education=draw(education_lists),
        certifications=draw(certification_refs),
        skills_blob=draw(blob_skills),
        total_experience_months=draw(months),
        locations=draw(location_lists),
        work_auth=draw(maybe_work_auths),
        parse_tier=draw(parse_tiers),
        flags=draw(FLAG_LISTS[review_flags]),
    )


requirements = st.builds(
    JobRequirement,
    skill_id=skill_ids,
    necessity=st.sampled_from(list(Necessity)),
    min_years=st.none() | st.integers(0, 60),
    or_group=st.none() | st.sampled_from(("g1", "g2", "skill.python")),
    or_equivalent=booleans,
    surface_form=free_text,
    weight_multiplier=st.sampled_from((1.0, 1.0, 1.0, 0.5, 0.0)),
)
REQUIREMENT_LISTS = {count: st.lists(requirements, min_size=count, max_size=10).map(tuple) for count in (0, 1)}

gates = st.one_of(
    st.builds(WorkAuthGate, countries=country_lists, sponsorship_available=maybe_booleans,
              citizenship_required=booleans),
    st.builds(LicenseGate, credential_ids=st.lists(credential_ids, min_size=1, max_size=2).map(tuple)),
    st.builds(ClearanceGate, level=st.sampled_from(CLEARANCES + ("",)), active_required=booleans),
    st.builds(LocationGate, locations=location_lists, onsite_required=booleans),
)
gate_lists = st.lists(gates, max_size=3).map(tuple)
education_requirements = st.none() | st.builds(
    EducationRequirement,
    min_level=st.sampled_from(list(DegreeLevel)),
    field_ids=st.lists(st.sampled_from(FIELD_IDS), max_size=2).map(tuple),
    or_equivalent=booleans,
    necessity=st.sampled_from(list(Necessity)),
)
token_lists = st.lists(free_text, max_size=3).map(tuple)
years = st.none() | st.floats(0.0, 60.0)
remote_policies = st.sampled_from(list(RemotePolicy))
token_counts = st.integers(0, 30)


@st.composite
def postings(draw: Any, review_flags: bool = True, min_requirements: int = 0) -> JobPosting:
    return JobPosting(
        title_raw=draw(free_text),
        seniority=draw(maybe_levels),
        requirements=draw(REQUIREMENT_LISTS[min_requirements]),
        min_years_total=draw(years),
        education_requirement=draw(education_requirements),
        certifications_required=draw(credential_lists),
        certifications_preferred=draw(credential_lists),
        industry_codes=draw(naics_lists),
        locations=draw(location_lists),
        remote_policy=draw(remote_policies),
        gates=draw(gate_lists),
        unresolved_requirement_tokens=draw(token_lists),
        requirement_token_count=draw(token_counts),
        is_internship=draw(booleans),
        flags=draw(FLAG_LISTS[review_flags]),
    )


# ─── Garbage (SPEC §13: arbitrary bytes, unicode, malformed structures) ─────────────────────

json_scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=True, allow_infinity=True),
                         st.text(max_size=20))
json_values = st.recursive(
    json_scalars,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=12), children, max_size=4),
    max_leaves=25,
)

RESUME_FIELDS = tuple(Resume.model_fields) + ("name", "address", "age")
POSTING_FIELDS = tuple(JobPosting.model_fields)
ROLE_FIELDS = tuple(Role.model_fields)


def jsonish_dicts(fields: tuple[str, ...]) -> Any:
    """Dicts keyed by real field names (and some others) with arbitrary JSON-ish values, including
    nested role-like dicts, so validation is exercised deep inside the models."""
    role_like = st.dictionaries(st.sampled_from(ROLE_FIELDS), json_values, max_size=6)
    value = st.one_of(json_values, st.lists(role_like, max_size=3))
    return st.dictionaries(st.sampled_from(fields) | st.text(max_size=8), value, max_size=8)


def constructed(model: Any, fields: tuple[str, ...]) -> Any:
    """`model_construct` with arbitrary field values: a malformed model that never saw validation."""
    return st.dictionaries(st.sampled_from(fields), json_values, max_size=5).map(lambda kw: model.model_construct(**kw))


def garbage(fields: tuple[str, ...], model: Any) -> Any:
    return st.one_of(
        st.binary(max_size=200),
        st.text(max_size=200),
        json_values,
        json_values.map(lambda value: json.dumps(value, allow_nan=True)),
        jsonish_dicts(fields),
        jsonish_dicts(fields).map(lambda value: json.dumps(value, allow_nan=True)),
        constructed(model, fields),
    )


as_of_values = st.one_of(
    st.just(AS_OF), st.just(AS_OF), st.dates(), st.datetimes(), st.none(), st.text(max_size=10),
    st.integers(), st.floats(allow_nan=True),
)
