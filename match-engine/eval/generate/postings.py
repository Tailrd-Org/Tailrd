"""Synthetic job postings (EVAL.md section 2.1): the targets the candidate profiles are built for.

A posting is a plain engine `JobPosting` with REQUIRED, PREFERRED and IMPLIED requirements. The
engine model has no id field, so a posting's id is derived from its content (`posting_id`): the
same posting always has the same id, and two different postings never share one.
"""

from __future__ import annotations

import hashlib
import random
import re

from eval.generate.careers import IC0
from eval.generate.lexicon import family_data, title_for
from eval.generate.vocabulary import VocabIndex, Vocabulary, vocabulary_index
from matcher.models import (
    DegreeLevel,
    EducationRequirement,
    JobPosting,
    JobRequirement,
    Location,
    Necessity,
    RemotePolicy,
    SeniorityLevel,
    Track,
    canonical_json,
)

# A family needs this many bullet-able skills to staff a posting plus the candidates written for it.
MIN_FAMILY_SKILLS = 18

_SENIORITY_MIX: tuple[tuple[SeniorityLevel, float, tuple[int, int]], ...] = (
    # (level, weight, range of minimum total years)
    (IC0, 0.12, (0, 0)),
    (SeniorityLevel(track=Track.IC, level=1), 0.22, (0, 1)),
    (SeniorityLevel(track=Track.IC, level=2), 0.26, (2, 3)),
    (SeniorityLevel(track=Track.IC, level=3), 0.22, (4, 6)),
    (SeniorityLevel(track=Track.IC, level=4), 0.06, (7, 9)),
    (SeniorityLevel(track=Track.MGMT, level=2), 0.08, (6, 8)),
    (SeniorityLevel(track=Track.MGMT, level=3), 0.04, (9, 12)),
)
_LOCATIONS = (
    Location(country="CA", region="ON", city="Toronto"),
    Location(country="US", region="NY", city="New York"),
    Location(country="US", region="WA", city="Seattle"),
    Location(country="CA", region="BC", city="Vancouver"),
    Location(country="US", region="TX", city="Austin"),
)


def posting_id(posting: JobPosting) -> str:
    """`<family>-<8 hex digits of the content hash>`."""
    digest = hashlib.sha256(canonical_json(posting).encode("utf-8")).hexdigest()[:8]
    family = re.sub(r"[^a-z0-9]+", "_", (posting.job_family or "posting").lower()).strip("_") or "posting"
    return f"{family}-{digest}"


def viable_families(vocab: Vocabulary, index: VocabIndex | None = None) -> tuple[str, ...]:
    index = index or vocabulary_index(vocab)
    return tuple(f for f in vocab.families if len(index.hard[f]) >= MIN_FAMILY_SKILLS)


def _weighted(rng: random.Random, options):
    roll, acc = rng.random() * sum(w for _o, w, _y in options), 0.0
    for option in options:
        acc += option[1]
        if roll < acc:
            return option
    return options[-1]


def _education(
    rng: random.Random, index: VocabIndex, family: str, sen: SeniorityLevel
) -> EducationRequirement:
    if sen.track is Track.MGMT or sen.level >= 4:
        level = DegreeLevel.MASTER if rng.random() < 0.4 else DegreeLevel.BACHELOR
    elif family == "healthcare" and sen.level >= 2 and rng.random() < 0.25:
        level = DegreeLevel.ASSOCIATE
    elif family == "data" and sen.level >= 3 and rng.random() < 0.3:
        level = DegreeLevel.MASTER
    else:
        level = DegreeLevel.BACHELOR
    fields = index.fields[family]
    field_ids = (
        tuple(f.id for f in rng.sample(fields, min(len(fields), rng.randint(1, 2))))
        if fields and rng.random() < 0.8
        else ()
    )
    return EducationRequirement(min_level=level, field_ids=field_ids, or_equivalent=rng.random() < 0.35)


def _one_posting(rng: random.Random, index: VocabIndex, family: str) -> JobPosting:
    fam = family_data(family)
    sen, _weight, years_range = _weighted(rng, _SENIORITY_MIX)
    years = rng.randint(*years_range)
    hard = list(index.hard[family])
    n_req = rng.randint(8, min(12, len(hard) - 7))
    required = rng.sample(hard, n_req)
    rest = [s for s in hard if s not in required]
    preferred = rng.sample(rest, min(rng.randint(3, 5), len(rest)))
    leftovers = [s for s in rest if s not in preferred]
    implied_pool = list(index.soft[family]) + leftovers[:6]
    implied = rng.sample(implied_pool, min(rng.randint(2, 4), len(implied_pool)))

    # a few required skills ask for years of use, never more than the posting asks in total
    min_years: dict[str, int] = {}
    if years >= 2:
        for skill in rng.sample(required, rng.randint(0, 3)):
            min_years[skill.id] = rng.randint(1, min(5, years))

    def requirement(skill, necessity: Necessity) -> JobRequirement:
        form = (
            rng.choice((skill.label, *skill.aliases)) if skill.aliases and rng.random() < 0.2 else skill.label
        )
        return JobRequirement(
            skill_id=skill.id,
            necessity=necessity,
            min_years=min_years.get(skill.id) if necessity is Necessity.REQUIRED else None,
            surface_form=form,
        )

    requirements = (
        *(requirement(s, Necessity.REQUIRED) for s in required),
        *(requirement(s, Necessity.PREFERRED) for s in preferred),
        *(requirement(s, Necessity.IMPLIED) for s in implied),
    )
    credentials = index.credentials[family]
    where = rng.choice(_LOCATIONS)
    policy = rng.choice((RemotePolicy.ONSITE, RemotePolicy.HYBRID, RemotePolicy.REMOTE))
    return JobPosting(
        title_raw=title_for(fam, sen, rng),
        job_family=family,
        seniority=sen,
        requirements=requirements,
        min_years_total=float(years),
        education_requirement=_education(rng, index, family, sen),
        certifications_preferred=(rng.choice(credentials).id,) if credentials and rng.random() < 0.45 else (),
        industry_codes=(rng.choice(fam.naics),),
        locations=() if policy is RemotePolicy.REMOTE else (where,),
        remote_policy=policy,
        requirement_token_count=len(requirements),
        is_internship=sen == IC0,
    )


def generate_postings(vocab: Vocabulary, n_per_family: int, seed: int) -> tuple[JobPosting, ...]:
    """`n_per_family` postings for every family with enough skills, family by family.

    Posting k of a family depends on (seed, family, k) only, so asking for more postings never
    changes the ones already generated.
    """
    index = vocabulary_index(vocab)
    out: list[JobPosting] = []
    seen: set[str] = set()
    for family in viable_families(vocab, index):
        for k in range(n_per_family):
            for attempt in range(20):  # a content collision is vanishingly rare; re-draw if it happens
                rng = random.Random(f"posting:{seed}:{family}:{k}:{attempt}")
                posting = _one_posting(rng, index, family)
                if posting_id(posting) not in seen:
                    break
            else:
                raise RuntimeError(f"could not draw a distinct posting for {family} #{k}")
            seen.add(posting_id(posting))
            out.append(posting)
    return tuple(out)
