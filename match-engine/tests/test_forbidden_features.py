"""Prohibited features (SPEC §9; DECISIONS D-14). CLAUDE.md: never modify this test to make a build pass.

Two kinds of evidence that the score never reads what §9 forbids:

1. Static. An AST scan of every `score/` module except `gates.py` (the one module allowed to read
   work authorisation, places and credentials, for eligibility only, SPEC §4) fails on:
   * an attribute named `title_raw`, `employer`, `surface_form`, `field_raw`, `locations`,
     `work_auth`, `name`, `institution`, `school`, `address`, `birth`, `age`, `gender` or `photo`,
     or any attribute whose `_`-separated words include a person word (employer, name, institution,
     school, address, birth, age, gender, photo, dob, zip, postal: `date_of_birth`, `zip_code`);
     dunder attributes (`__name__`, Python's own object metadata) are the only exception;
   * `getattr` / `hasattr` / `setattr` / `delattr` / `attrgetter` / `methodcaller` naming one of
     those, or naming anything with a non-literal (an attribute name built at run time cannot be
     checked);
   * a subscript with one of those names as a string key (`dict(role)["employer"]`).
2. Behavioural. Changing an employer, a title_raw, a surface form, a field_raw, a project name or
   the résumé's places (when no onsite gate reads them) changes every byte of every fixture's result
   except `input_digest`; and moving roles apart in time (gaps), with their durations and the skills'
   last-use dates kept, changes no score (§9: employment gaps are never computed or scored).
"""

from __future__ import annotations

import ast
import json
from datetime import date, timedelta
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.test_fixture_pairs import FIXTURES, load, run_fixture

SCORE_DIR = Path(__file__).resolve().parents[1] / "src" / "matcher" / "score"
EXEMPT = {"gates.py"}
FORBIDDEN = frozenset({
    "title_raw", "employer", "surface_form", "field_raw", "locations", "work_auth", "name", "institution",
    "school", "address", "birth", "age", "gender", "photo",
})
PERSON_WORDS = frozenset({
    "employer", "name", "institution", "school", "address", "birth", "age", "gender", "photo", "dob", "zip",
    "postal",
})
ATTRIBUTE_FUNCTIONS = frozenset({"getattr", "hasattr", "setattr", "delattr", "attrgetter", "methodcaller"})


# ─── 1. Static scan ──────────────────────────────────────────────────────────────────────────


def _forbidden(attribute: str) -> bool:
    if attribute.startswith("__") and attribute.endswith("__"):
        return False  # Python object metadata (`type(x).__name__`), never a model field
    words = set(attribute.lower().strip("_").split("_"))
    return attribute in FORBIDDEN or bool(words & PERSON_WORDS)


def _function_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def violations(source: str, filename: str = "<source>") -> list[str]:
    """Every forbidden read in `source`, as 'file:line: what'."""
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        where = f"{filename}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Attribute) and _forbidden(node.attr):
            found.append(f"{where}: attribute .{node.attr}")
        elif isinstance(node, ast.Call) and _function_name(node) in ATTRIBUTE_FUNCTIONS:
            names = node.args[1:2] if _function_name(node) in {"getattr", "hasattr", "setattr", "delattr"} else node.args
            for arg in names:
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    found.append(f"{where}: {_function_name(node)} with a run-time attribute name")
                elif any(_forbidden(part) for part in arg.value.split(".")):
                    found.append(f"{where}: {_function_name(node)}({arg.value!r})")
        elif isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and _forbidden(key.value):
                found.append(f"{where}: subscript [{key.value!r}]")
    return found


def _modules() -> list[Path]:
    return sorted(path for path in SCORE_DIR.rglob("*.py") if path.name not in EXEMPT)


def test_the_scan_covers_every_scoring_module() -> None:
    names = {path.relative_to(SCORE_DIR).as_posix() for path in _modules()}
    assert {"engine.py", "prepare.py", "coverage.py", "aggregate.py", "confidence.py", "intervals.py",
            "dimensions/experience.py", "dimensions/education.py", "dimensions/seniority.py"} <= names
    assert "gates.py" not in names


def test_no_scoring_module_reads_a_forbidden_field() -> None:
    found = [hit for path in _modules() for hit in violations(path.read_text(encoding="utf-8"), path.name)]
    assert found == []


def test_the_scan_catches_every_form_of_forbidden_read() -> None:
    # The scanner must not pass by being blind: each of these is a violation.
    planted = [
        "role.employer", "resume.locations", "resume.work_auth", "ref.surface_form", "education.field_raw",
        "role.title_raw", "project.name", "person.age", "person.date_of_birth", "person.zip_code",
        "candidate.school_tier", "x.gender", "x.photo_url", "x.institution", "x.address_line",
        "getattr(role, 'employer')", "getattr(role, field)", "hasattr(resume, 'work_auth')",
        "attrgetter('roles.employer')", "attrgetter(tier)", "methodcaller(name)", "dict(role)['employer']",
        "role.model_dump()['title_raw']",
    ]
    for source in planted:
        assert violations(source), source
    # ... and these are not: Python metadata and ordinary words that merely contain a person word.
    for source in ("type(x).__name__", "c.coverage", "m.message", "p.average", "getattr(c, 'calibrate', None)",
                   "d['value']"):
        assert violations(source) == [], source


# ─── 2. Behaviour: free text and places never move a score ──────────────────────────────────


def _has_onsite_gate(posting: dict) -> bool:
    return any(gate.get("kind") == "LOCATION" and gate.get("onsite_required", True) for gate in posting.get("gates", ()))


def _refs(resume: dict):
    for container in (*resume.get("roles", ()), *resume.get("projects", ())):
        yield from container.get("skills", ())
    yield from resume.get("skills_blob", ())
    yield from resume.get("certifications", ())


def _rewritten(fixture: dict, text: str, places: list[dict]) -> dict:
    """The fixture with every free-text field the score may not read replaced by `text`, and the
    résumé's places replaced by `places` when no onsite gate reads them."""
    changed = json.loads(json.dumps(fixture))
    resume, posting = changed["resume"], changed["posting"]
    for role in resume.get("roles", ()):
        role["employer"] = text
        role["title_raw"] = text
    for project in resume.get("projects", ()):
        project["name"] = text
    for education in resume.get("education", ()):
        education["field_raw"] = text
    for ref in _refs(resume):
        ref["surface_form"] = text
    for requirement in posting.get("requirements", ()):
        requirement["surface_form"] = text
    posting["title_raw"] = text
    if not _has_onsite_gate(posting):
        resume["locations"] = places
    return changed


def _without_digest(result) -> dict:
    dumped = result.model_dump(mode="json")
    dumped.pop("input_digest")
    return dumped


PLACES = [[], [{"country": "GB", "region": "ENG", "city": "Leeds"}],
          [{"country": "CA", "region": "QC", "city": "Montréal"}, {"country": "US", "region": "NY"}]]
TEXTS = ["", "Zainab Okafor-Lindqvist", "Ünïcødé Ltd. ​­", "x" * 2000]


def test_free_text_and_places_never_change_a_result() -> None:
    for path in FIXTURES:
        fixture = load(path)
        base = run_fixture(fixture)
        for text, places in zip(TEXTS, PLACES + [PLACES[1]]):
            changed = run_fixture(_rewritten(fixture, text, places))
            assert changed.input_digest != base.input_digest, path.stem  # the input really changed
            assert _without_digest(changed) == _without_digest(base), (path.stem, text[:20])


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(index=st.integers(0, len(FIXTURES) - 1), text=st.text(max_size=60),
       places=st.lists(st.fixed_dictionaries({"country": st.sampled_from(["CA", "US", "IN", "NG"]),
                                              "city": st.text(max_size=10)}), max_size=2))
def test_any_free_text_never_changes_a_result(index: int, text: str, places: list[dict]) -> None:
    fixture = load(FIXTURES[index])
    assert _without_digest(run_fixture(_rewritten(fixture, text, places))) == _without_digest(run_fixture(fixture))


# ─── 2b. Behaviour: gaps between roles are never scored ─────────────────────────────────────

AS_OF = date(2026, 10, 8)
POSTING = load(next(path for path in FIXTURES if path.stem.endswith("strong_all_round")))["posting"]
POSTING_IDS = sorted({requirement["skill_id"] for requirement in POSTING["requirements"]})
VERBS = ["OWNERSHIP", "EXECUTION", "PARTICIPATION", "NO_VERB"]


def _day(value: date) -> dict:
    return {"value": value.isoformat(), "earliest": value.isoformat(), "latest": value.isoformat(),
            "precision": "DAY"}


@st.composite
def careers(draw) -> tuple[list[dict], list[int]]:
    """Ended roles laid back to back (no overlap, no gap) before a current role, with every skill
    mention carrying its own last-use date; and the gaps to open between them."""
    count = draw(st.integers(1, 3))
    durations = draw(st.lists(st.integers(30, 1500), min_size=count, max_size=count))
    current_start = AS_OF - timedelta(days=draw(st.integers(30, 1500)))
    roles, end = [], current_start
    for duration in reversed(durations):
        start = end - timedelta(days=duration)
        roles.insert(0, {"start": start, "end": end})
        end = start
    for role in [*roles, {"start": current_start, "end": None}]:
        skills = draw(st.lists(st.sampled_from(POSTING_IDS), min_size=1, max_size=6, unique=True))
        role["skills"] = [
            {"canonical_id": skill, "evidence": "ROLE_BULLET", "verb_class": draw(st.sampled_from(VERBS)),
             "last_used": (AS_OF - timedelta(days=draw(st.integers(0, 3000)))).isoformat()}
            for skill in skills
        ]
        role["seniority"] = {"track": "IC", "level": draw(st.integers(0, 4))}
        role["industry_codes"] = [draw(st.sampled_from(["541511", "6111", "5182"]))]
        role["is_internship"] = draw(st.booleans())
        if role["end"] is None:
            roles.append(role)
    gaps = draw(st.lists(st.integers(1, 2500), min_size=count, max_size=count))
    return roles, gaps


def _resume(roles: list[dict], shift: list[int]) -> dict:
    """The career with ended role i moved `shift[i]` days earlier, its duration kept."""
    built = []
    for index, role in enumerate(roles):
        entry = {key: value for key, value in role.items() if key not in ("start", "end")}
        back = timedelta(days=shift[index]) if index < len(shift) else timedelta(0)
        entry["start"] = _day(role["start"] - back)
        if role["end"] is None:
            entry["is_current"] = True
        else:
            entry["end"] = _day(role["end"] - back)
        built.append(entry)
    return {"roles": built, "education": [{"level": "BACHELOR", "field_ids": ["field.computer_science"]}]}


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(career=careers())
def test_gaps_between_roles_change_no_score(career) -> None:
    roles, gaps = career
    # Opening gap g_i before ended role i (counting from the current role backwards) moves it, and
    # every role before it, back by the sum of the gaps after it: durations are kept, roles never overlap.
    shift = [sum(gaps[index:]) for index in range(len(gaps))]
    fixture = {"as_of": AS_OF.isoformat(), "posting": POSTING}
    together = run_fixture({**fixture, "resume": _resume(roles, [0] * len(gaps))})
    apart = run_fixture({**fixture, "resume": _resume(roles, shift)})
    assert apart.raw_score == together.raw_score
    assert apart.outcome is together.outcome
    assert {name: dim.value for name, dim in apart.dimensions.items()} == {
        name: dim.value for name, dim in together.dimensions.items()}
