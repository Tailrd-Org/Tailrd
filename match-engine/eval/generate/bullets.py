"""Bullet composition: one sentence from a verb of the lexicon, an object and its skills.

Every bullet is written so that the engine reads it as declared (`matcher.score.evidence
.analyze_bullet`, SPEC 15.1, DECISIONS D-34a..f): the verb opens the bullet in its past form (or
"Basic knowledge of", "Coursework in", "Familiar with"), the skills follow in the same clause or in a
pure list ("... using Python, SQL and Airflow"), and an instrument participle ("using") defers to
the verb. A verbless bullet is a noun phrase with its tools in a parenthesis. A quantified bullet
carries a number in a trailing clause, a plain bullet carries no digit at all.

The templates here are grammar, not a copy of the engine's rules: the tests run every sentence they
can produce through `analyze_bullet` and `is_quantified`, so a change of lexicon is caught by
behaviour, not by a word list.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Sequence

from eval.generate import lexicon
from eval.generate.lexicon import FamilyData
from eval.generate.vocabulary import VocabSkill
from matcher.models import SkillKind, VerbClass

INSTRUMENT_KINDS = frozenset({SkillKind.TECHNOLOGY, SkillKind.TOOL})


def group_of(kind: SkillKind) -> str:
    """Skills of one group can share a bullet: instruments (up to three), or one method / domain."""
    if kind in INSTRUMENT_KINDS:
        return "instrument"
    return "method" if kind is SkillKind.METHODOLOGY else "domain"


def _join(forms: Sequence[str]) -> str:
    return forms[0] if len(forms) == 1 else ", ".join(forms[:-1]) + " and " + forms[-1]


def _cap(text: str) -> str:
    return text[0].upper() + text[1:]


def _tail(rng: random.Random, fam: FamilyData, quantified: bool) -> str:
    if not quantified:
        return rng.choice(lexicon.PLAIN_TAILS) if rng.random() < 0.5 else ""
    roll = rng.random()
    if roll < 0.55:
        template, (lo, hi) = rng.choice(lexicon.METRIC_TAILS)
        return template.format(metric=rng.choice(fam.metrics), n=rng.randint(lo, hi))
    if roll < 0.8:
        template, (lo, hi) = rng.choice(lexicon.FIXED_TAILS)
        return template.format(n=rng.randint(lo, hi))
    return fam.scale_tail.format(n=rng.randint(*fam.scale_range))


def _phrases(verb_class: VerbClass, with_skills: bool) -> tuple[tuple[str, str], ...]:
    """The verb phrases a bullet of this class can open with; without skills only the ones that
    take an object (a filler bullet about the profession's work)."""
    phrases = lexicon.VERB_PHRASES[verb_class]
    return phrases if with_skills else tuple(p for p in phrases if p[1] == "obj")


def _core(
    verb_class: VerbClass,
    group: str,
    forms: Sequence[str],
    obj: str,
    *,
    prefix: str = "",
    layout: str = "obj",
    team: str = "",
    joiner: str = "",
) -> str:
    """One sentence, without its tail, from the parts `compose_bullet` draws."""
    if verb_class is VerbClass.NO_VERB:
        if not forms:
            return f"{_cap(obj)} for {team}"
        if group == "instrument":
            return f"{_cap(obj)} for {team} ({', '.join(forms)})"
        if group == "method":
            return f"{_cap(obj)} following {forms[0]} practices"
        return f"{_cap(obj)} for {forms[0]} initiatives"
    if not forms:
        return f"{prefix} {obj}"
    if group == "instrument":
        if layout == "obj":
            if joiner == "with" and prefix.endswith(" with"):  # not "Assisted with X with Y"
                joiner = "using"
            return f"{prefix} {obj} {joiner} {_join(forms)}"
        if layout == "first":
            return f"{prefix} {_join(forms)} for {obj}"
        return f"{prefix} {_join(forms)}"
    if group == "method":
        return {
            "obj": f"{prefix} {obj} following {forms[0]} practices",
            "first": f"{prefix} {forms[0]} practices for {obj}",
            "only": f"{prefix} {forms[0]} practices",
        }[layout]
    return {
        "obj": f"{prefix} {obj} for {forms[0]} initiatives",
        "first": f"{prefix} {forms[0]} for {obj}",
        "only": f"{prefix} {forms[0]}",
    }[layout]


def iter_cores(fam: FamilyData, verb_class: VerbClass, group: str, forms: Sequence[str]) -> Iterator[str]:
    """Every sentence (before its tail) that `compose_bullet` can write for this profession, verb
    class and skills of one group: all objects x verb phrases x joiners, or all objects x teams for
    a verbless bullet. With no `forms` they are the filler sentences."""
    for obj in fam.objects:
        if verb_class is VerbClass.NO_VERB:
            for team in lexicon.TEAMS:
                yield _core(verb_class, group, forms, obj, team=team)
            continue
        for prefix, layout in _phrases(verb_class, bool(forms)):
            joiners = (
                lexicon.INSTRUMENT_JOINERS if forms and group == "instrument" and layout == "obj" else ("",)
            )
            for joiner in joiners:
                yield _core(verb_class, group, forms, obj, prefix=prefix, layout=layout, joiner=joiner)


def compose_bullet(
    rng: random.Random,
    fam: FamilyData,
    verb_class: VerbClass,
    skills: Sequence[tuple[VocabSkill, str]],
    quantified: bool,
) -> str:
    """One bullet about `skills` (skill, spelling to write), all of one group, in the given class.

    Without skills the bullet is filler about the profession's work (a class with an "obj" layout).
    An exposure bullet never carries a tail: "Familiar with X, reducing latency by 35%" is not
    something anyone writes.
    """
    obj = rng.choice(fam.objects)
    forms = [form for _skill, form in skills]
    group = group_of(skills[0][0].kind) if skills else ""
    parts: dict[str, str] = {}
    if verb_class is VerbClass.NO_VERB:
        parts["team"] = rng.choice(lexicon.TEAMS)
    else:
        phrases = _phrases(verb_class, bool(skills))
        if not phrases:
            raise ValueError(f"{verb_class.value} bullets must mention a skill")
        parts["prefix"], parts["layout"] = rng.choice(phrases)
        if skills and group == "instrument" and parts["layout"] == "obj":
            parts["joiner"] = rng.choice(lexicon.INSTRUMENT_JOINERS)
    core = _core(verb_class, group, forms, obj, **parts)
    if verb_class is VerbClass.EXPOSURE:
        if quantified:
            raise ValueError("an exposure bullet is never quantified")
        return _cap(core)
    return _cap(core + _tail(rng, fam, quantified))
