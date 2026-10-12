"""SPEC §14.4 job-posting pathologies, one test per row, plus the Tailrd data facts (Task 6.5).

Rows: no requirements extractable; 40+ requirements; impossible years; contradictory level and
years; one bullet holding six skills; "X or equivalent" / "X or Y"; requirements buried in prose;
boilerplate only. Tailrd facts: stored descriptions may be escaped HTML, may be cut at 2,000 or
3,000 characters with a trailing "...", and about 1,100 postings a month have no description at
all. Postings are invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from matcher.calibrate import FixedCalibrator
from matcher.errors import NEEDS_REVIEW_REASONS, Flag
from matcher.graph.model import SkillGraph
from matcher.models import (
    DateValue,
    Evidence,
    Necessity,
    Outcome,
    Resume,
    Role,
    ScoreOptions,
    SeniorityLevel,
    SkillRef,
    Track,
    VerbClass,
)
from matcher.parse.posting import PostingMeta, parse_posting
from matcher.score.engine import score
from tests.posting_helpers import AS_OF, TORONTO, env, necessities, parse, reqs

# Thirty skills the committed taxonomy knows, in an order that is not alphabetical.
THIRTY = ["Python", "Kubernetes", "Docker", "Terraform", "React", "TypeScript", "PostgreSQL", "Kafka", "Redis",
          "GraphQL", "Django", "Flask", "Java", "Scala", "Rust", "Swift", "Angular", "Airflow", "Spark",
          "Tableau", "Jira", "Git", "Linux", "Ansible", "Jenkins", "MongoDB", "Cassandra", "Snowflake",
          "BigQuery", "Figma"]


def _resume(*skill_ids: str) -> Resume:
    skills = tuple(SkillRef(canonical_id=s, evidence=Evidence.ROLE_BULLET, verb_class=VerbClass.OWNERSHIP)
                   for s in skill_ids)
    role = Role(title_raw="Developer", seniority=SeniorityLevel(track=Track.IC, level=1),
                start=DateValue.exact(date(2024, 1, 1)), is_current=True, skills=skills)
    return Resume(roles=(role,))


def _score(resume, posting):
    e = env()
    return score(resume, posting, as_of=AS_OF, config=e["config"], graph=e["graph"], calibration=FixedCalibrator(),
                 options=ScoreOptions(strict=True))


# ─── no requirements extractable ──────────────────────────────────────────────────────────


def test_no_requirements_extractable_is_unscorable() -> None:
    posting = parse("We are a great company. Join our friendly team and grow with us!")
    assert posting.requirements == ()
    assert Flag.POSTING_UNSCORABLE in posting.flags
    result = _score(_resume("skill.python"), posting)
    assert result.outcome is Outcome.NEEDS_REVIEW and Flag.POSTING_UNSCORABLE in result.reasons
    assert result.display_score is None


def test_an_empty_description_is_title_and_metadata_only() -> None:
    for empty in ("", "   \n\t  ", None):
        posting = parse(empty, title="Software Engineer Intern", company="Shopify", work_type="Hybrid",
                        experience_level="internship")
        assert posting.requirements == () and posting.gates == ()
        assert posting.education_requirement is None and posting.min_years_total is None
        assert posting.flags == (Flag.POSTING_UNSCORABLE,)
        assert Flag.POSTING_UNSCORABLE in NEEDS_REVIEW_REASONS  # routed to a person, never scored on a blank
        # what the title and metadata say is kept
        assert posting.seniority.code == "IC0" and posting.is_internship
        assert posting.title_canonical == "title.15-1252.00" and posting.industry_codes == ("5132",)
        assert posting.remote_policy.value == "HYBRID"
        # the title names a skill-like word, yet nothing is invented from it
        assert parse(empty, title="Python Developer").requirements == ()


# ─── 40+ requirements ─────────────────────────────────────────────────────────────────────


def _idf_graph(values: dict[str, float]) -> SkillGraph:
    base = env()["graph"]
    return SkillGraph(base.nodes, base.related, values, version="test-idf")


def test_more_than_25_weighted_requirements_keep_the_top_25_by_idf() -> None:
    text = "Requirements\n" + "\n".join(f"- {name}" for name in THIRTY)
    plain = parse(text)
    ids = sorted(reqs(plain))
    assert len(ids) == 30
    # Give the five alphabetically FIRST ids the lowest IDF: they are the ones demoted.
    low = ids[:5]
    idf = {skill_id: (0.1 if skill_id in low else 0.9) for skill_id in ids}
    e = env()
    posting = parse_posting("Software Developer", text, meta=PostingMeta(locations=(TORONTO,)), as_of=AS_OF,
                            resolver=e["resolver"], graph=_idf_graph(idf), config=e["config"],
                            parse_config=e["parse_config"], titles=e["titles"])
    rows = reqs(posting)
    assert len(rows) == 30  # nothing is dropped
    demoted = {s for s, r in rows.items() if r.necessity is Necessity.IMPLIED}
    assert demoted == set(low)
    assert all(rows[s].weight_multiplier == e["config"].scoring.posting.overflow_weight_multiplier for s in low)
    assert all(rows[s].weight_multiplier == 1.0 and rows[s].necessity is Necessity.REQUIRED
               for s in ids if s not in low)
    assert Flag.REQUIREMENTS_TRUNCATED in posting.flags


def test_overflow_ties_break_by_id_and_a_group_is_one_unit() -> None:
    text = "Requirements\n" + "\n".join(f"- {name}" for name in THIRTY[:26]) + "\n- AWS or GCP"
    e = env()
    # An empty IDF table makes every IDF the unseen default, so every unit ties (independent of the
    # committed corpus IDF).
    posting = parse_posting("Software Developer", text, meta=PostingMeta(locations=(TORONTO,)), as_of=AS_OF,
                            resolver=e["resolver"], graph=_idf_graph({}), config=e["config"],
                            parse_config=e["parse_config"], titles=e["titles"])
    rows = reqs(posting)
    weighted = {r.or_group or r.skill_id for r in posting.requirements if r.necessity is not Necessity.IMPLIED}
    assert len(weighted) == 25
    demoted_units = sorted({r.or_group or r.skill_id for r in posting.requirements
                            if r.necessity is Necessity.IMPLIED})
    every_unit = sorted({r.or_group or r.skill_id for r in posting.requirements},
                        key=lambda u: min(s for s, r in rows.items() if (r.or_group or s) == u))
    assert demoted_units == sorted(every_unit[25:])  # the largest ids lose a tie
    assert rows["skill.aws"].or_group == rows["skill.gcp"].or_group  # the group moved, or stayed, as one


def test_exactly_25_is_not_truncated() -> None:
    posting = parse("Requirements\n" + "\n".join(f"- {name}" for name in THIRTY[:25]))
    assert Flag.REQUIREMENTS_TRUNCATED not in posting.flags
    assert all(r.necessity is Necessity.REQUIRED for r in posting.requirements)


# ─── impossible requirement ───────────────────────────────────────────────────────────────


def test_years_beyond_a_skills_age_are_clamped_and_flagged() -> None:
    # Kubernetes was released in 2014 (taxonomy release_year): 12 years by 2026-10-01.
    posting = parse("Requirements\n- 15+ years of Kubernetes\n- 5 years of Python")
    k8s, python = reqs(posting)["skill.kubernetes"], reqs(posting)["skill.python"]
    assert k8s.min_years == 12 and Flag.UNREALISTIC_REQUIREMENT in k8s.flags
    assert python.min_years == 5 and python.flags == ()
    assert Flag.UNREALISTIC_REQUIREMENT in posting.flags
    assert env()["graph"].node("skill.kubernetes").release_year == 2014


def test_a_realistic_requirement_is_untouched() -> None:
    posting = parse("Requirements\n- 10 years of Kubernetes", as_of=date(2026, 10, 1))
    assert reqs(posting)["skill.kubernetes"].min_years == 10
    assert Flag.UNREALISTIC_REQUIREMENT not in posting.flags


# ─── contradictory requirements ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("title, line", [
    ("Junior Developer", "- 10 years of experience required"),
    ("Software Engineer Intern", "- 5+ years of experience"),
    ("Junior Data Analyst", "- 6 years of SQL"),
])
def test_a_junior_title_with_many_years_is_flagged_and_both_facts_kept(title: str, line: str) -> None:
    posting = parse(f"Requirements\n{line}\n- Python", title=title)
    assert Flag.CONTRADICTORY_REQUIREMENTS in posting.flags
    assert posting.seniority.level <= 1  # the level stays what the title says
    years = posting.min_years_total or max(r.min_years or 0 for r in posting.requirements)
    assert years >= 5  # and the years stay what the text says: nothing averaged


def test_consistent_postings_are_not_flagged() -> None:
    assert Flag.CONTRADICTORY_REQUIREMENTS not in parse("Requirements\n- 2 years of experience",
                                                        title="Junior Developer").flags
    assert Flag.CONTRADICTORY_REQUIREMENTS not in parse("Requirements\n- 8 years of experience",
                                                        title="Senior Software Engineer").flags


# ─── one bullet containing six skills ─────────────────────────────────────────────────────


def test_one_bullet_with_six_skills_gives_six_requirements() -> None:
    posting = parse("Requirements\n- Strong Python, SQL, Docker, Kubernetes, Terraform and React skills")
    assert necessities(posting) == {s: "REQUIRED" for s in (
        "skill.python", "skill.sql", "skill.docker", "skill.kubernetes", "skill.terraform", "skill.react")}


# ─── X or equivalent, X or Y ──────────────────────────────────────────────────────────────


def test_or_groups_score_the_best_member_not_the_mean() -> None:
    posting = parse("Requirements\n- Python or Java\n- Docker or equivalent")
    rows = reqs(posting)
    assert rows["skill.python"].or_group == rows["skill.java"].or_group == "or:1"
    assert rows["skill.docker"].or_group == "or:2" and rows["skill.docker"].or_equivalent
    both = _score(_resume("skill.python", "skill.docker"), posting)
    one = _score(_resume("skill.python", "skill.java", "skill.docker"), posting)
    # having Java as well adds nothing: the group is max(m), not a mean over members
    assert both.dimensions["required_skills"].value == one.dimensions["required_skills"].value == 1.0


# ─── requirements buried in prose ─────────────────────────────────────────────────────────


def test_requirements_buried_in_prose() -> None:
    text = ("As a member of our data team you will build pipelines. The ideal hire must have hands-on "
            "experience with Spark and Airflow, and a minimum of 2 years of experience. Knowledge of "
            "Kafka would be a plus. We work in Python every day.")
    posting = parse(text)
    assert necessities(posting) == {"skill.spark": "REQUIRED", "skill.airflow": "REQUIRED",
                                    "skill.kafka": "PREFERRED", "skill.python": "IMPLIED"}
    assert posting.min_years_total == 2.0
    assert Flag.POSTING_UNSCORABLE not in posting.flags


# ─── boilerplate-only posting ─────────────────────────────────────────────────────────────


BOILERPLATE = """About us
We are a leading company with offices around the world. Our mission is to delight customers and to
build a workplace where everyone belongs. We have been recognised as a top employer many times.
Benefits
Competitive salary, health and dental coverage, retirement matching, generous paid time off.
Equal opportunity employer
We welcome applicants of every background and provide accommodation on request.
The role
Help the team with Excel reports."""


def test_boilerplate_only_is_flagged_and_abstains() -> None:
    posting = parse(BOILERPLATE)
    assert Flag.BOILERPLATE_ONLY in posting.flags
    assert len(posting.requirements) < 3
    result = _score(_resume("skill.excel"), posting)
    # few requirements drive the §17 abstention: no confident percentage
    assert result.outcome is not Outcome.SCORED and result.display_score is None


def test_a_real_posting_with_a_long_about_section_is_not_boilerplate() -> None:
    posting = parse(BOILERPLATE + "\nRequirements\n- Python\n- SQL\n- Docker\n- Kubernetes")
    assert Flag.BOILERPLATE_ONLY not in posting.flags


# ─── Tailrd facts: escaped HTML, truncation ───────────────────────────────────────────────


def test_escaped_html_never_reaches_the_resolver() -> None:
    raw = ("&lt;p&gt;&lt;strong&gt;What you&amp;#39;ll need&lt;/strong&gt;&lt;/p&gt;&lt;ul&gt;"
           "&lt;li&gt;Experience with React &amp;amp; TypeScript&lt;/li&gt;&lt;li&gt;Git&lt;/li&gt;&lt;/ul&gt;")
    posting = parse(raw)
    assert necessities(posting) == {"skill.react": "REQUIRED", "skill.typescript": "REQUIRED", "skill.git": "REQUIRED"}
    assert not any("<" in t or "&" in t for t in posting.unresolved_requirement_tokens)


@pytest.mark.parametrize("cap", [2000, 3000])
def test_a_capped_description_is_truncated_not_ended(cap: int) -> None:
    # The stored text was cut inside "JavaScript": the fragment "Java" must not read as Java.
    tail = "\nRequirements\n- Python\n- SQL\n- Experience with Java..."
    about = ("About us\n" + "We build tools that people love and we care about our customers. " * 60)
    raw = about[: cap - len(tail)].rstrip() + tail
    raw = raw.rjust(cap - len(raw) + len(raw))  # exactly at the cap
    assert abs(len(raw) - cap) <= 1
    posting = parse(raw)
    assert Flag.POSTING_TRUNCATED in posting.flags
    assert "skill.java" not in reqs(posting) and "skill.javascript" not in reqs(posting)
    assert necessities(posting) == {"skill.python": "REQUIRED", "skill.sql": "REQUIRED"}
    # the cut is not the end of a requirements list: no boilerplate verdict on what we never saw
    assert Flag.BOILERPLATE_ONLY not in posting.flags


def test_a_short_description_ending_in_an_ellipsis_is_not_truncated() -> None:
    posting = parse("Requirements\n- Python\n- SQL, and more...")
    assert Flag.POSTING_TRUNCATED not in posting.flags


def test_a_truncated_posting_lowers_confidence() -> None:
    body = "Requirements\n- Python\n- SQL\n- Docker\n- Kubernetes\n- React\n" + "We value craft. " * 200
    whole = parse(body[:1900] + ".")
    cut = parse(body[:1997] + "...")
    assert Flag.POSTING_TRUNCATED in cut.flags and Flag.POSTING_TRUNCATED not in whole.flags
    resume = _resume("skill.python", "skill.sql", "skill.docker", "skill.kubernetes", "skill.react")
    a, b = _score(resume, whole), _score(resume, cut)
    assert b.confidence.components["parse_quality"] < a.confidence.components["parse_quality"]
    assert b.confidence.value < a.confidence.value
    assert Flag.POSTING_TRUNCATED in b.flags
    # the penalty is always named, whatever the outcome (fix round 1)
    assert Flag.POSTING_TRUNCATED in b.reasons and Flag.POSTING_TRUNCATED in b.confidence.reasons
    assert Flag.POSTING_TRUNCATED not in a.reasons
