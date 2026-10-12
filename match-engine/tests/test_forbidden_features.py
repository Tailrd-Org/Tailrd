"""Prohibited features (SPEC §9; DECISIONS D-14). CLAUDE.md: never modify this test to make a build pass.

Two kinds of evidence that the score never reads what §9 forbids:

1. Static. An AST scan of every `score/` module except `score/gates.py` (the one module allowed to
   read work authorisation, places and credentials, for eligibility only, SPEC §4) fails on:
   * an attribute named `title_raw`, `employer`, `surface_form`, `field_raw`, `locations`,
     `work_auth`, `name`, `institution`, `school`, `address`, `birth`, `age`, `gender` or `photo`,
     or any name whose `_`-separated words include a person word (employer, name, institution,
     school, address, birth, age, gender, photo, dob, zip, postal: `date_of_birth`, `zip_code`);
     dunders (`__name__`, Python's own object metadata) are the only exception;
   * any string constant that is such a name, whole or as a `.`-separated part: subscripts,
     `.get` / `.pop`, `itemgetter`, `include={...}`, comparisons and names kept in a variable;
   * a match-class keyword with such a name (`case Role(employer=e)`);
   * `getattr` / `hasattr` / `setattr` / `delattr` / `attrgetter` / `itemgetter` / `methodcaller`
     (or an import or assignment alias of one) with a run-time name, which cannot be checked;
   * `__getattribute__`, `__getattr__`, `__dict__` and `vars()`, which read any field;
   and the bodies of `models.py` properties, which `score/` may call, are scanned the same way.
2. Behavioural. Changing an employer, a title_raw, a surface form, a field_raw, a project name, the
   résumé's places (when no onsite gate reads them), its work authorisation and clearances (when no
   work-authorisation or clearance gate reads them), the posting's places (when no location or
   work-authorisation gate reads them) or the remote policy changes no byte of any fixture's result
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
EXEMPT = frozenset({"gates.py"})  # paths relative to SCORE_DIR: only score/gates.py, never a namesake
MODELS = Path(__file__).resolve().parents[1] / "src" / "matcher" / "models.py"
FORBIDDEN = frozenset({
    "title_raw", "employer", "surface_form", "field_raw", "locations", "work_auth", "name", "institution",
    "school", "address", "birth", "age", "gender", "photo",
})
PERSON_WORDS = frozenset({
    "employer", "name", "institution", "school", "address", "birth", "age", "gender", "photo", "dob", "zip",
    "postal",
})
# Functions that read an attribute or a key named by an argument; import aliases count as themselves.
NAME_ARGUMENT_FUNCTIONS = frozenset({"getattr", "hasattr", "setattr", "delattr"})  # the name is argument 2
NAMING_FUNCTIONS = NAME_ARGUMENT_FUNCTIONS | {"attrgetter", "itemgetter", "methodcaller"}
# Calls and attributes that can read any field whatever the code names: never used in score/.
OPAQUE_READERS = frozenset({"__getattribute__", "__getattr__", "vars"})


# ─── 1. Static scan ──────────────────────────────────────────────────────────────────────────


def _forbidden(text: str) -> bool:
    """A forbidden field name, or a name whose `_`-separated words include a person word. Dunders
    (`__name__`, Python's own object metadata) are never model fields."""
    if text.startswith("__") and text.endswith("__"):
        return False
    words = set(text.lower().strip("_").split("_"))
    return text in FORBIDDEN or bool(words & PERSON_WORDS)


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local names bound to a naming function or an opaque reader: `from operator import attrgetter
    as ag`, `ga = getattr`."""
    known = NAMING_FUNCTIONS | OPAQUE_READERS
    aliases = {name: name for name in known}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in known:
                    aliases[alias.asname or alias.name] = alias.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
            source = node.value.id if isinstance(node.value, ast.Name) else node.value.attr
            if source in aliases:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = aliases[source]
    return aliases


def _called(call: ast.Call, aliases: dict[str, str]) -> str:
    if isinstance(call.func, ast.Name):
        return aliases.get(call.func.id, "")
    if isinstance(call.func, ast.Attribute):  # operator.attrgetter, object.__getattribute__, x.__getattribute__
        return aliases.get(call.func.attr, "")
    return ""


def violations(source: str, filename: str = "<source>") -> list[str]:
    """Every forbidden read in `source`, as 'file:line: what'. Any string constant naming a forbidden
    field is one (`d["employer"]`, `.get("employer")`, `F = "employer"`, `include={"employer"}`,
    `k == "employer"`), whole or as a `.`-separated part; so are such attributes, match-class
    keywords (`case Role(employer=e)`), naming functions with a run-time name, and opaque readers."""
    tree = ast.parse(source, filename)
    aliases = _aliases(tree)
    found = []
    for node in ast.walk(tree):
        where = f"{filename}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Attribute):
            if _forbidden(node.attr) or node.attr in OPAQUE_READERS or node.attr == "__dict__":
                found.append(f"{where}: attribute .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(_forbidden(part) for part in node.value.split(".")):
                found.append(f"{where}: string {node.value!r}")
        elif isinstance(node, ast.MatchClass):
            found.extend(f"{where}: match keyword {kw}" for kw in node.kwd_attrs if _forbidden(kw))
        elif isinstance(node, ast.Call):
            function = _called(node, aliases)
            if function in OPAQUE_READERS:
                found.append(f"{where}: {function}()")
            elif function in NAMING_FUNCTIONS:
                names = node.args[1:2] if function in NAME_ARGUMENT_FUNCTIONS else node.args
                if any(not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)) for arg in names):
                    found.append(f"{where}: {function} with a run-time name")
    return found


def _modules() -> list[Path]:
    return sorted(path for path in SCORE_DIR.rglob("*.py") if path.relative_to(SCORE_DIR).as_posix() not in EXEMPT)


def test_the_scan_covers_every_scoring_module() -> None:
    names = {path.relative_to(SCORE_DIR).as_posix() for path in _modules()}
    assert {"engine.py", "prepare.py", "coverage.py", "aggregate.py", "confidence.py", "intervals.py",
            "dimensions/experience.py", "dimensions/education.py", "dimensions/seniority.py"} <= names
    assert "gates.py" not in names
    assert all((SCORE_DIR / exempt).is_file() for exempt in EXEMPT)  # the exemption names a real module


def test_no_scoring_module_reads_a_forbidden_field() -> None:
    found = [hit for path in _modules()
             for hit in violations(path.read_text(encoding="utf-8"), path.relative_to(SCORE_DIR).as_posix())]
    assert found == []


def test_no_model_property_reads_a_forbidden_field() -> None:
    # score/ may call model properties, and a property that read a forbidden field would launder it.
    # Every property body in models.py is scanned; `dimension` reads DimensionScore.name, the
    # dimension's key (not a person's), and is the one allowed.
    allowed = {"dimension"}
    found = []
    for node in ast.walk(ast.parse(MODELS.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name not in allowed and any(
                isinstance(decorator, ast.Name) and decorator.id == "property" for decorator in node.decorator_list):
            body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            found += violations(body, f"models.py:{node.name}")
    assert found == []


PLANTED = [
    "role.employer", "resume.locations", "resume.work_auth", "ref.surface_form", "education.field_raw",
    "role.title_raw", "project.name", "person.age", "person.date_of_birth", "person.zip_code",
    "candidate.school_tier", "x.gender", "x.photo_url", "x.institution", "x.address_line",
    "getattr(role, 'employer')", "getattr(role, field)", "hasattr(resume, 'work_auth')",
    "attrgetter('roles.employer')", "attrgetter(tier)", "methodcaller(name)", "dict(role)['employer']",
    "role.model_dump()['title_raw']",
    # Fix round 1: the bypasses the Task 3.6 review found, and their relatives.
    "role.model_dump().get('employer')", "dict(role).pop('title_raw')", "role.__dict__.get('employer')",
    "itemgetter('employer')", "role.__getattribute__('employer')", "object.__getattribute__(role, 'employer')",
    "F = 'employer'\nd[F]", "from operator import attrgetter as ag\nag('employer')",
    "from operator import attrgetter as ag\nag(key)", "from operator import itemgetter as ig\nig(key)",
    "ga = getattr\nga(role, key)", "import operator as op\nop.attrgetter(key)",
    "match role:\n    case Role(employer=e):\n        pass",
    "match role:\n    case {'employer': e}:\n        pass",
    "for k, v in role:\n    if k == 'employer':\n        pass",
    "role.model_dump(include={'employer'})", "vars(role)", "role.__dict__", "role.__getattr__",
]


def test_the_scan_catches_every_form_of_forbidden_read() -> None:
    # The scanner must not pass by being blind: each of these is a violation.
    for source in PLANTED:
        assert violations(source), source
    # ... and these are not: Python metadata and ordinary words that merely contain a person word.
    for source in ("type(x).__name__", "c.coverage", "m.message", "p.average", "getattr(c, 'calibrate', None)",
                   "d['value']", "'the average message, of a page.'", "attrgetter('exact')"):
        assert violations(source) == [], source


# ─── 2. Behaviour: free text and places never move a score ──────────────────────────────────


def _has_onsite_gate(posting: dict) -> bool:
    return any(gate.get("kind") == "LOCATION" and gate.get("onsite_required", True) for gate in posting.get("gates", ()))


def _gate_kinds(posting: dict) -> set[str]:
    return {gate.get("kind") for gate in posting.get("gates", ())}


def _refs(resume: dict):
    for container in (*resume.get("roles", ()), *resume.get("projects", ())):
        yield from container.get("skills", ())
    yield from resume.get("skills_blob", ())
    yield from resume.get("certifications", ())


def _rewritten(fixture: dict, text: str, places: list[dict], work_auth: object = None,
               posting_places: object = None, remote: str = "HYBRID") -> dict:
    """The fixture with every input the score may not read changed: free text replaced by `text`;
    the résumé's places by `places` unless an onsite gate reads them; `work_auth` (authorised
    countries, sponsorship, citizenships, clearances) unless a work-authorisation or clearance gate
    reads it; the posting's places unless a location or work-authorisation gate reads them; and the
    remote policy, which no gate reads."""
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
    kinds = _gate_kinds(posting)
    if not _has_onsite_gate(posting):
        resume["locations"] = places
    if not kinds & {"WORK_AUTHORIZATION", "SECURITY_CLEARANCE"}:
        resume["work_auth"] = work_auth
    if not kinds & {"LOCATION", "WORK_AUTHORIZATION"} and posting_places is not None:
        posting["locations"] = posting_places
    posting["remote_policy"] = remote
    return changed


def _without_digest(result) -> dict:
    dumped = result.model_dump(mode="json")
    dumped.pop("input_digest")
    return dumped


PLACES = [[], [{"country": "GB", "region": "ENG", "city": "Leeds"}],
          [{"country": "CA", "region": "QC", "city": "Montréal"}, {"country": "US", "region": "NY"}]]
TEXTS = ["", "Zainab Okafor-Lindqvist", "Ünïcødé Ltd. \u200b\u00ad", "x" * 2000]
WORK_AUTHS = [
    None,
    {"authorized_countries": ["CA"], "needs_sponsorship": False, "citizenships": ["CA"]},
    {"authorized_countries": [], "needs_sponsorship": True, "citizenships": ["NG", "IN"],
     "clearances": ["US_TOP_SECRET", "ACTIVE_UNKNOWN_LEVEL"]},
    {"needs_sponsorship": None, "clearances": ["CA_SECRET"]},
]
POSTING_PLACES = [[], [{"country": "US", "region": "CA", "city": "San Jose"}], None, [{"country": "FR"}]]
REMOTE = ["ONSITE", "REMOTE", "UNKNOWN", "HYBRID"]


def test_free_text_places_and_work_authorisation_never_change_a_result() -> None:
    covered = {"work_auth": 0, "places": 0, "posting_places": 0}
    for path in FIXTURES:
        fixture = load(path)
        base = run_fixture(fixture)
        kinds = _gate_kinds(fixture["posting"])
        for variant in zip(TEXTS, PLACES + [PLACES[1]], WORK_AUTHS, POSTING_PLACES, REMOTE):
            changed = run_fixture(_rewritten(fixture, *variant))
            assert changed.input_digest != base.input_digest, path.stem  # the input really changed
            assert _without_digest(changed) == _without_digest(base), (path.stem, variant[0][:20])
        covered["work_auth"] += not kinds & {"WORK_AUTHORIZATION", "SECURITY_CLEARANCE"}
        covered["places"] += not _has_onsite_gate(fixture["posting"])
        covered["posting_places"] += not kinds & {"LOCATION", "WORK_AUTHORIZATION"}
    assert min(covered.values()) >= 25, covered  # the gate-only rewrites ran on almost every pair


def test_the_gate_reads_what_the_invariance_test_leaves_alone() -> None:
    # Control: the same rewrite DOES change a result when a gate reads the field, so the test above
    # is not passing because the rewrite never reaches the engine.
    fixture = load(next(path for path in FIXTURES if path.stem.endswith("gate_fail_sponsorship")))
    changed = json.loads(json.dumps(fixture))
    changed["resume"]["work_auth"] = {"authorized_countries": ["CA"], "needs_sponsorship": False}
    assert run_fixture(fixture).outcome.value == "GATE_FAIL"
    assert run_fixture(changed).outcome.value != "GATE_FAIL"


_places = st.lists(st.fixed_dictionaries({"country": st.sampled_from(["CA", "US", "IN", "NG"]),
                                         "city": st.text(max_size=10)}), max_size=2)
_work_auths = st.none() | st.fixed_dictionaries({
    "authorized_countries": st.lists(st.sampled_from(["CA", "US", "GB"]), max_size=2),
    "needs_sponsorship": st.none() | st.booleans(),
    "citizenships": st.lists(st.sampled_from(["CA", "US", "IN", "NG"]), max_size=2),
    "clearances": st.lists(st.sampled_from(["US_SECRET", "CA_RELIABILITY", "ACTIVE_UNKNOWN_LEVEL"]), max_size=2),
})


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(index=st.integers(0, len(FIXTURES) - 1), text=st.text(max_size=60), places=_places,
       work_auth=_work_auths, posting_places=st.none() | _places,
       remote=st.sampled_from(["ONSITE", "HYBRID", "REMOTE", "UNKNOWN"]))
def test_any_free_text_or_gate_only_input_never_changes_a_result(index, text, places, work_auth, posting_places,
                                                                  remote) -> None:
    fixture = load(FIXTURES[index])
    changed = _rewritten(fixture, text, places, work_auth, posting_places, remote)
    assert _without_digest(run_fixture(changed)) == _without_digest(run_fixture(fixture))


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
