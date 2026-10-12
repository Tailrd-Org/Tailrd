"""Roles: experience lines into drafts (`split_roles`), drafts into scored `Role`s (`build_role`).

SPEC §5.3, §14.2, §14.3; DECISIONS D-11, D-12, D-29, D-32..D-34, D-38.

`split_roles(section, *, as_of, lang)` reads an experience-like section with `_entries`: dated
header lines (dates read on that line only, never on a bullet) and the head lines around them.
The pieces of an entry's head lines become title, employer and location: the piece that reads most
like a job is the title (job words: engineer, analyst, intern, ...), the piece that reads most like
an organisation is the employer (Inc, University, Bank, ...); on a tie the brief's patterns decide
(title first on one line; across lines the line nearest the bullets holds the title:
"Employer / Title / dates", "Employer (dates) / Title"). Pieces left over join the title. An
employment type written as a piece is kept on the title when it marks an internship ("Software
Developer (Internship)"). Then:
* promotions: an employer line over several dated titles ("RBC / Senior Analyst | 2024 –
  Present / Analyst | 2022 – 2023"), or a dated employer line directly over them, gives every title
  that names no employer of its own that employer; one draft per title (SPEC §14.3);
* several ranges on one header line ("Fall 2022, Winter 2023") are one draft each;
* consecutive drafts at the same employer with the same title, apart from employment-type words
  ("(Contract)"), less than 60 days apart merge (contract-to-hire chains, SPEC §14.3): the dates
  are joined, the later title is kept, the bullets of both stay in document order;
* founders and the self-employed are ordinary roles; a range the date parser drops (a future
  start, an impossible range) keeps the role with that range, which has no dates.

`build_role(draft, ...)` fills a `Role`: title id from the titles index, seniority from the title
(IC0 for a role at a student organisation; an executive title, M4-M6, at a student organisation
or at an employer neither the company table nor the keyword rules know abstains, DECISIONS
D-38a), industry from the employer and the first bullet, `is_internship` from the title or the
date text, and the skills. An "employer" that is a list of skills ("React, Node.js, PostgreSQL",
"ROS, C++") is a tech stack: no employer, its skills are ROLE_TITLE mentions. Then the title's
mentions (ROLE_TITLE), the military codes in the title and bullets (their crosswalk skills as
ROLE_TITLE, the code as the surface), and every bullet's mentions with their verb class and
polarity (`score.evidence.analyze_bullet`) as ROLE_BULLET, or ROLE_BULLET_QUANTIFIED when the
bullet states a quantified outcome. Mentions are dated from the role (`last_used` = as_of while it
is current) and kept once per (id, evidence, verb, polarity).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Optional

from matcher.config import EngineConfig
from matcher.models import (
    Evidence,
    Frozen,
    Polarity,
    Role,
    SeniorityLevel,
    SkillRef,
    SkillSource,
    Track,
    VerbClass,
)
from matcher.normalize.resolver import Resolver
from matcher.parse import _entries
from matcher.parse._text import fold, words
from matcher.parse.config import ParseConfig
from matcher.parse.dates import DateRange
from matcher.parse.industry import classify_industry
from matcher.parse.sections import Section
from matcher.parse.seniority import is_executive, is_internship_text, is_student_organisation, title_seniority
from matcher.parse.taxonomy_extras import MosIndex, TitleIndex
from matcher.score.evidence import BulletMention, analyze_bullet, is_quantified

__all__ = ["RoleDraft", "build_role", "split_roles"]

_CHAIN_DAYS = 60  # SPEC §14.3 contract-to-hire
_MAX_TEXT = 2000  # models.Role free-text bound
_MAX_SKILLS = 1000  # models.Role.skills bound
_MAX_INDUSTRIES = 20
_EMPLOYMENT_WORDS = frozenset({
    "contract", "contractor", "contractual", "temporary", "temp", "permanent", "full", "part", "time",
    "fulltime", "parttime", "seasonal", "casual", "freelance", "contractuel", "contractuelle", "temps",
    "plein", "partiel", "saisonnier", "saisonniere",
})


class RoleDraft(Frozen):
    title_raw: str
    employer: str
    location: str
    date_range: Optional[DateRange]
    bullets: tuple[str, ...]


# ─── split_roles ────────────────────────────────────────────────────────────────────────────


class _Assigned(Frozen):
    title: str
    employer: str
    location: str
    employer_from_line_above: bool  # a lone employer line over a title-only header line


def _pieces(entry: _entries.Entry) -> list[tuple[str, int]]:
    """(piece, line index) of the entry's head lines in document order."""
    out: list[tuple[str, int]] = []
    heads = [*entry.before, *([entry.anchor] if entry.anchor is not None else []), *entry.after]
    for index in heads:
        parts = entry.pieces.get(index)
        if parts is not None:
            out.extend((piece, index) for piece in parts.pieces)
    return out


def _assign(entry: _entries.Entry) -> _Assigned:
    heads = [*entry.before, *([entry.anchor] if entry.anchor is not None else []), *entry.after]
    order = [entry.anchor] if entry.anchor is not None else []
    order += [*entry.after, *entry.before]
    location = next((entry.pieces[i].place for i in order if i in entry.pieces and entry.pieces[i].place), "")
    kinds: list[str] = []
    for index in heads:
        parts = entry.pieces.get(index)
        if parts is not None:
            kinds.extend(kind for kind in parts.kinds if kind not in kinds)
    pieces = _pieces(entry)
    if not pieces:
        return _Assigned(title=_with_kinds("", kinds), employer="", location=location, employer_from_line_above=False)
    texts = [piece for piece, _ in pieces]
    lines = [index for _, index in pieces]
    title_scores = [_entries.title_score(piece) for piece in texts]
    employer_scores = [_entries.employer_score(piece) for piece in texts]
    one_line = len(set(lines)) == 1
    last_line = max(lines)
    default_title = 0 if one_line else lines.index(last_line)
    n = len(texts)
    if n == 1:
        if employer_scores[0] > title_scores[0]:
            return _Assigned(title=_with_kinds("", kinds), employer=texts[0], location=location,
                             employer_from_line_above=False)
        return _Assigned(title=_with_kinds(texts[0], kinds), employer="", location=location,
                         employer_from_line_above=False)
    title_at = max(range(n), key=lambda i: (title_scores[i] - employer_scores[i], title_scores[i],
                                            i == default_title, -i))
    rest = [i for i in range(n) if i != title_at]
    default_employer = rest[-1] if one_line else rest[0]
    employer_at = max(rest, key=lambda i: (employer_scores[i] - title_scores[i], i == default_employer, -i))
    leftovers = [texts[i] for i in rest if i != employer_at]
    title = ", ".join([texts[title_at], *leftovers])
    above = (len(entry.before) == 1 and lines[employer_at] == entry.before[0] and entry.anchor is not None
             and lines[title_at] == entry.anchor)
    return _Assigned(title=_with_kinds(title, kinds), employer=texts[employer_at], location=location,
                     employer_from_line_above=above)


def _with_kinds(title: str, kinds: Sequence[str]) -> str:
    out = title
    for kind in kinds:
        if fold(kind) not in fold(out):
            out = f"{out} ({kind})".strip() if out else kind
    return out


def _employer_key(employer: str) -> str:
    return " ".join(words(employer))


def _title_core(title: str) -> tuple[str, ...]:
    return tuple(word for word in words(title) if word not in _EMPLOYMENT_WORDS)


def _chained(a: RoleDraft, b: RoleDraft) -> bool:
    """Same employer, same title (employment-type words aside), less than 60 days apart."""
    ra, rb = a.date_range, b.date_range
    if ra is None or rb is None or ra.start is None or rb.start is None:
        return False
    if not _employer_key(a.employer) or _employer_key(a.employer) != _employer_key(b.employer):
        return False
    if not _title_core(a.title_raw) or _title_core(a.title_raw) != _title_core(b.title_raw):
        return False
    earlier, later = (ra, rb) if ra.start.value <= rb.start.value else (rb, ra)
    if earlier.is_current or earlier.end is None:
        return True  # they overlap
    return (later.start.value - earlier.end.value).days < _CHAIN_DAYS


def _merged(a: RoleDraft, b: RoleDraft) -> RoleDraft:
    ra, rb = a.date_range, b.date_range
    assert ra is not None and rb is not None and ra.start is not None and rb.start is not None
    first, second = (a, b) if ra.start.value <= rb.start.value else (b, a)
    earlier, later = first.date_range, second.date_range
    assert earlier is not None and later is not None
    end = later.end
    if end is not None and earlier.end is not None and earlier.end.value > end.value:
        end = earlier.end
    current = later.is_current or earlier.is_current
    flags = tuple(sorted(set(earlier.flags) | set(later.flags), key=lambda flag: flag.value))
    joined = DateRange(start=earlier.start, end=None if current else end, is_current=current, flags=flags,
                       raw=f"{a.date_range.raw}; {b.date_range.raw}")
    return RoleDraft(title_raw=second.title_raw, employer=second.employer, location=second.location or first.location,
                     date_range=joined, bullets=tuple(dict.fromkeys(a.bullets + b.bullets)))


def split_roles(section: Section, *, as_of: dt.date, lang: str = "en") -> tuple[RoleDraft, ...]:
    """The roles of an experience-like section, in document order (see the module docstring)."""
    lines = _entries.classify(section.lines, as_of=as_of, lang="fr" if lang == "fr" else "en")
    drafts: list[RoleDraft] = []
    group_employer = ""
    for entry in _entries.entries(lines, strict=False):
        if entry.group:
            pieces = _pieces(entry)
            group_employer = pieces[0][0] if pieces else ""
            continue
        assigned = _assign(entry)
        employer = assigned.employer
        if not employer and group_employer:
            employer = group_employer
        elif employer:
            group_employer = employer if assigned.employer_from_line_above else ""
        bullets = _entries.items(lines, entry.body)
        ranges = lines[entry.anchor].ranges if entry.anchor is not None else ()
        for date_range in ([found[2] for found in ranges] or [None]):
            drafts.append(RoleDraft(title_raw=assigned.title, employer=employer, location=assigned.location,
                                    date_range=date_range, bullets=bullets))
    merged: list[RoleDraft] = []
    for draft in drafts:
        if merged and _chained(merged[-1], draft):
            merged[-1] = _merged(merged[-1], draft)
        else:
            merged.append(draft)
    return tuple(merged)


# ─── build_role ─────────────────────────────────────────────────────────────────────────────

_IC0 = SeniorityLevel(track=Track.IC, level=0)
_EVIDENCE_RANK = {Evidence.ROLE_TITLE: 2, Evidence.ROLE_BULLET_QUANTIFIED: 2, Evidence.ROLE_BULLET: 1}
_POLARITY_RANK = {Polarity.AFFIRMED: 2, Polarity.ASPIRATIONAL: 1, Polarity.NEGATED: 0}


def _bounded(refs: list[SkillRef], config: EngineConfig) -> tuple[SkillRef, ...]:
    """Once per (id, evidence, verb, polarity); within the model's bound, the strongest per id first."""
    seen: set[tuple[str, Evidence, VerbClass, Polarity]] = set()
    unique: list[SkillRef] = []
    for ref in refs:
        key = (ref.canonical_id, ref.evidence, ref.verb_class, ref.polarity)
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    if len(unique) <= _MAX_SKILLS:
        return tuple(unique)

    def strength(ref: SkillRef) -> tuple[int, float, int]:
        return (_POLARITY_RANK[ref.polarity], config.verbs.multiplier(ref.verb_class), _EVIDENCE_RANK[ref.evidence])

    best: dict[str, SkillRef] = {}
    for ref in unique:
        if ref.canonical_id not in best or strength(ref) > strength(best[ref.canonical_id]):
            best[ref.canonical_id] = ref
    order = list(dict.fromkeys(ref.canonical_id for ref in unique))
    return tuple(best[canonical_id] for canonical_id in order[:_MAX_SKILLS])


_STACK_SHARE = 0.5  # share of an "employer" piece's words that skill mentions must cover


def _stack_mentions(employer: str, resolver: Resolver, parse_config: ParseConfig) -> tuple:
    """The skill mentions of an employer piece that is really a tech stack ("React, Node.js,
    PostgreSQL", "ROS, C++"): a list (two mentions or more, or a comma or slash) whose mentions
    cover at least half of its words, and no organisation the industry table knows ("Shopify",
    "Salesforce" stay employers); else ()."""
    if not employer:
        return ()
    mentions = resolver.extract(employer, context="list")
    if not mentions or (len(mentions) < 2 and not any(ch in employer for ch in ",/")):
        return ()
    if classify_industry(employer, "", parse_config):
        return ()
    total = len(words(employer))
    covered = sum(len(words(m.surface)) for m in mentions)
    return tuple(mentions) if total and covered / total >= _STACK_SHARE else ()


def build_role(draft: RoleDraft, *, as_of: dt.date, resolver: Resolver, titles: TitleIndex, mos: MosIndex,
               parse_config: ParseConfig, config: EngineConfig) -> Role:
    """A scored `Role` from a draft (see the module docstring)."""
    title = " ".join(draft.title_raw.split())[:_MAX_TEXT]
    employer = " ".join(draft.employer.split())[:_MAX_TEXT]
    bullets = [" ".join(bullet.split()) for bullet in draft.bullets]
    bullets = [bullet for bullet in bullets if bullet]

    date_range = draft.date_range
    start = date_range.start if date_range is not None else None
    end = date_range.end if date_range is not None and start is not None else None
    current = bool(date_range is not None and start is not None and date_range.is_current)
    first_used = start.value if start is not None else None
    last_used = end.value if end is not None else (as_of if current and start is not None else None)
    if first_used is not None and last_used is not None and first_used > last_used:
        first_used = last_used  # an as_of earlier than the role's start: keep the mention valid

    def ref(canonical_id: str, surface: str, evidence: Evidence, verb: VerbClass = VerbClass.NO_VERB,
            polarity: Polarity = Polarity.AFFIRMED) -> SkillRef:
        return SkillRef(canonical_id=canonical_id, surface_form=surface[:_MAX_TEXT], evidence=evidence,
                        first_used=first_used, last_used=last_used, verb_class=verb, polarity=polarity,
                        source=SkillSource.ROLE)

    refs: list[SkillRef] = [ref(m.canonical_id, m.surface, Evidence.ROLE_TITLE) for m in resolver.extract(title)]
    stack = _stack_mentions(employer, resolver, parse_config)
    if stack:  # "Campus Eats | React, Node.js, PostgreSQL": a tech stack, not an employer
        refs.extend(ref(m.canonical_id, m.surface, Evidence.ROLE_TITLE) for m in stack)
        employer = ""
    for code, skill_ids in mos.find("\n".join([title, *bullets]), context=employer):
        refs.extend(ref(skill_id, code, Evidence.ROLE_TITLE) for skill_id in skill_ids)
    for bullet in bullets:
        mentions = resolver.extract(bullet)
        if not mentions:
            continue
        analysed = analyze_bullet(
            bullet, [BulletMention(canonical_id=m.canonical_id, start=m.start, end=m.end) for m in mentions], config)
        evidence = Evidence.ROLE_BULLET_QUANTIFIED if is_quantified(bullet, config) else Evidence.ROLE_BULLET
        refs.extend(ref(m.canonical_id, m.surface, evidence, a.verb_class, a.polarity)
                    for m, a in zip(mentions, analysed))

    seniority = title_seniority(title, parse_config)
    if is_executive(seniority) and (
            is_student_organisation(employer, parse_config) or not classify_industry(employer, "", parse_config)):
        seniority = None  # a club president or a founder of an unknown venture: abstain, never M4-M6
    elif is_student_organisation(employer, parse_config):
        seniority = _IC0
    internship = is_internship_text(title, parse_config) or (
        date_range is not None and is_internship_text(date_range.raw, parse_config))
    return Role(
        title_raw=title,
        title_canonical=titles.canonical_id(title),
        seniority=seniority,
        employer=employer,
        industry_codes=classify_industry(employer, bullets[0] if bullets else "", parse_config)[:_MAX_INDUSTRIES],
        start=start,
        end=None if current else end,
        is_current=current,
        is_internship=internship,
        skills=_bounded(refs, config),
    )
