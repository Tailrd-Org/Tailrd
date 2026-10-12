"""The hash-pinned extra artifacts of Task 2.2 as the parsers read them (Task 6.3).

`TitleIndex` maps a job title to its O*NET occupation (full title, then without seniority
modifiers, then the head before a team or specialty qualifier); `MosIndex` finds military
occupation codes (SPEC §14.3: "a legally sensitive population, get it right"), so a code counts
only when it is labelled ("MOS 25B"), written in brackets after its own title ("Rifleman (0311)"),
or a letters-and-digits code in a military role ("68W" at the U.S. Army). Expectations use the
tiny committed fixture (tests/fixtures/parse_taxonomy); the real artifacts are only read through
their MANIFEST and compared with themselves, never pinned.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matcher.graph.loader import ArtifactError, load_json_artifact
from matcher.parse.config import load_parse_config
from matcher.parse.taxonomy_extras import (
    MosIndex,
    TitleIndex,
    TitleInfo,
    load_mos_index,
    load_naics_codes,
    load_title_index,
)
from tests.fixtures.build_parse_taxonomy import FIXTURE_DIR, MOS, build

REAL = Path(__file__).resolve().parents[1] / "data" / "taxonomy"


@pytest.fixture(scope="module")
def parse_config():
    return load_parse_config()


@pytest.fixture(scope="module")
def titles(parse_config) -> TitleIndex:
    return load_title_index(FIXTURE_DIR, parse_config=parse_config)


@pytest.fixture(scope="module")
def mos() -> MosIndex:
    return load_mos_index(FIXTURE_DIR)


# ─── the fixture itself ─────────────────────────────────────────────────────────────────────


def test_committed_parse_fixture_is_current(tmp_path: Path) -> None:
    build(tmp_path)
    for name in ("MANIFEST.json", "titles.json", "naics.json", "mos_crosswalk.json"):
        assert (FIXTURE_DIR / name).read_bytes() == (tmp_path / name).read_bytes(), (
            f"{name} drifted from tests/fixtures/build_parse_taxonomy.py; rerun the builder"
        )


def test_a_tampered_artifact_is_refused(tmp_path: Path, parse_config) -> None:
    shutil.copytree(FIXTURE_DIR, tmp_path / "t")
    path = tmp_path / "t" / "titles.json"
    path.write_bytes(path.read_bytes().replace(b"15-1252.00", b"15-1252.01"))
    with pytest.raises(ArtifactError, match="titles.json"):
        load_title_index(tmp_path / "t", parse_config=parse_config)
    path = tmp_path / "t" / "mos_crosswalk.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ArtifactError, match="mos_crosswalk.json"):
        load_mos_index(tmp_path / "t")


def test_naics_codes_load_through_the_manifest() -> None:
    codes = load_naics_codes(FIXTURE_DIR)
    assert codes["5132"] == "Software Publishers"
    assert "44-45" not in codes and "44" in codes and "45" in codes


# ─── titles ─────────────────────────────────────────────────────────────────────────────────

DEVELOPER = TitleInfo(soc="15-1252.00", family="Computer and Mathematical Occupations", job_zone=4)


@pytest.mark.parametrize(
    "title",
    [
        "Software Developer",
        "software developer",
        "SOFTWARE DEVELOPER",
        "Senior Software Developer",  # without seniority modifiers
        "Sr. Software Developer",
        "Software Developer II",
        "Junior Software Developer",
        "Software Developer Intern",
        "Software Developer Co-op",
        "Software Developer (Co-op)",
        "Software Developer, Payments",  # the head before a team qualifier
        "Software Developer - Platform Team",
        "Lead Software Developer",
        "Software Engineer",
        "Engineer, Software",  # O*NET's inverted form
        "Staff Software Engineer",
    ],
)
def test_title_lookup_finds_the_occupation(titles: TitleIndex, title: str) -> None:
    assert titles.lookup(title) == DEVELOPER
    assert titles.canonical_id(title) == "title.15-1252.00"


def test_title_lookup_reads_hyphen_variants(titles: TitleIndex) -> None:
    assert titles.lookup("Front-End Developer").soc == "15-1254.00"
    assert titles.lookup("front end developer").soc == "15-1254.00"
    # A word written closed up is only known when the artifact lists that spelling.
    assert titles.lookup("Frontend Developer") is None


def test_a_title_with_no_job_zone(titles: TitleIndex) -> None:
    info = titles.lookup("Rifleman")
    assert info == TitleInfo(soc="55-3016.00", family="Military Specific Occupations", job_zone=None)


@pytest.mark.parametrize(
    "title",
    ["", "   ", "Intern", "Co-op Student", "Senior", "Wizard of Light", "Software", "Developer Relations Lead",
     "Student", "II"],
)
def test_unknown_and_generic_titles_have_no_occupation(titles: TitleIndex, title: str) -> None:
    assert titles.lookup(title) is None
    assert titles.canonical_id(title) is None


def test_title_lookup_never_strips_the_occupation_itself(titles: TitleIndex) -> None:
    # "Teaching Assistant" is a student job (IC0) but names the occupation: it is looked up whole.
    assert titles.lookup("Teaching Assistant").soc == "25-9042.00"
    assert titles.lookup("Graduate Teaching Assistant").soc == "25-9042.00"


def test_title_index_size(titles: TitleIndex) -> None:
    assert len(titles) == 9


# ─── military codes ───────────────────────────────────────────────────────────────────────


def _ids(code: str) -> tuple[str, ...]:
    return tuple(MOS["codes"][code]["skill_ids"])


@pytest.mark.parametrize(
    "text, context, expected",
    [
        # Letters-and-digits codes in a military role.
        ("Combat Medic Specialist (68W)", "U.S. Army", (("68W", _ids("68W")),)),
        ("Served as a 68W combat medic", "United States Army", (("68W", _ids("68W")),)),
        ("Cyber Systems Operations, 3D0X2", "U.S. Air Force", (("3D0X2", _ids("3D0X2")),)),
        # Labelled codes need no other context.
        ("Information Technology Specialist (MOS 25B)", "", (("25B", ()),)),
        ("MOS: 25B", "", (("25B", ()),)),
        ("AFSC 3D0X2", "", (("3D0X2", _ids("3D0X2")),)),
        # Digits-only codes: labelled, or bracketed after their own title.
        ("Rifleman (0311)", "USMC", (("0311", ()),)),
        ("Combat Engineer (1371)", "United States Marine Corps", (("1371", _ids("1371")),)),
        ("Infantry Rifleman 0311", "U.S. Marine Corps", (("0311", ()),)),
        # Letter-only ratings: bracketed after their own title, in a military role.
        ("Hospital Corpsman (HM)", "U.S. Navy", (("HM", _ids("HM")),)),
        ("Hospital Corpsman, HM", "", (("HM", _ids("HM")),)),  # "corpsman" itself is a military word
        ("Rating: HM", "U.S. Navy", (("HM", _ids("HM")),)),
        ("Navy Enlisted Classification NEC 8404", "U.S. Navy", (("8404", _ids("8404")),)),
        # Another service's code with the same letters: the branch named in the role wins.
        ("Information Systems Technician (IT)", "U.S. Navy", (("IT", _ids("IT")),)),
        ("Information Systems Technician (IT)", "U.S. Coast Guard", (("IT", ("skill.cisco_ios",)),)),
        # Several codes, in order, each once.
        ("MOS 68W, later 25B; trained 68W medics", "U.S. Army", (("68W", _ids("68W")), ("25B", ()))),
    ],
)
def test_mos_find(mos: MosIndex, text: str, context: str, expected: tuple) -> None:
    assert mos.find(text, context=context) == expected


@pytest.mark.parametrize(
    "text, context",
    [
        ("Room 25B, Engineering Building", ""),  # no military context
        ("Provided IT support to 300 Marines", "USMC"),  # a word, not a rating
        ("Human Resources (HR) Coordinator", "Canadian Armed Forces"),  # an abbreviation of its own title
        ("Information Technology (IT) Coordinator", "Acme Corp"),  # civilian
        ("Wired panels to NEC 8404 clearances", "Bright Electric"),  # an ambiguous label needs a military role
        ("Information Systems Technician (IT)", "Acme Corp"),  # a bracketed code needs a military role
        ("Reduced costs by 0311 percent", ""),
        ("Sales Associate, HM flagship store", "H&M"),
        ("Python, SQL, HM, IT", ""),
        ("", "U.S. Army"),
    ],
)
def test_mos_find_is_precise(mos: MosIndex, text: str, context: str) -> None:
    assert mos.find(text, context=context) == ()


def test_mos_find_reads_context_only_from_the_text_by_default(mos: MosIndex) -> None:
    assert mos.find("68W, U.S. Army") == (("68W", _ids("68W")),)
    assert mos.find("68W") == ()


# ─── the real artifacts, read through their MANIFEST (never pinned) ───────────────────────


def test_real_titles_load_and_strip_modifiers(parse_config) -> None:
    real = load_title_index(REAL, parse_config=parse_config)
    plain = real.lookup("Software Engineer")
    assert plain is not None
    assert real.lookup("Senior Software Engineer") == plain
    assert real.lookup("Software Engineer II") == plain
    assert real.canonical_id("Software Engineer") == f"title.{plain.soc}"


def test_real_mos_codes_load_and_match_the_crosswalk() -> None:
    real = load_mos_index(REAL)
    crosswalk = load_json_artifact(REAL, "mos_crosswalk.json")["codes"]
    found = real.find("Combat Medic Specialist (MOS 68W)")
    assert found == (("68W", tuple(crosswalk["68W"]["skill_ids"])),)
    assert real.find("Information Technology Specialist (MOS 25B)") == (
        ("25B", tuple(crosswalk["25B"]["skill_ids"])),
    )
