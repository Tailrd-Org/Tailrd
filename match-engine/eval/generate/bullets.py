"""Bullet composition: one sentence from a verb of the lexicon, an object and its skills.

Every bullet keeps its skills in one clause that starts at the verb (or, for a verbless bullet, in
a parenthesis), so the nearest-preceding-verb rule of SPEC 15.1 sees one unambiguous verb. A
quantified bullet carries a number in a trailing clause and a plain bullet carries no digit at all.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

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
    return fam.scale_tail.format(n=rng.randint(*lexicon.SCALE_RANGE))


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
    if verb_class is VerbClass.NO_VERB:
        team = rng.choice(lexicon.TEAMS)
        if not skills:
            core = f"{_cap(obj)} for {team}"
        elif group == "instrument":
            core = f"{_cap(obj)} for {team} ({', '.join(forms)})"
        elif group == "method":
            core = f"{_cap(obj)} following {forms[0]} practices"
        else:
            core = f"{_cap(obj)} for {forms[0]} initiatives"
    else:
        phrases = lexicon.VERB_PHRASES[verb_class]
        if not skills:
            phrases = tuple(p for p in phrases if p[1] == "obj")
            if not phrases:
                raise ValueError(f"{verb_class.value} bullets must mention a skill")
        prefix, layout = rng.choice(phrases)
        if not skills:
            core = f"{prefix} {obj}"
        elif group == "instrument":
            if layout == "obj":
                joiner = rng.choice(lexicon.INSTRUMENT_JOINERS)
                if joiner == "with" and prefix.endswith(" with"):  # not "Assisted with X with Y"
                    joiner = "using"
                core = f"{prefix} {obj} {joiner} {_join(forms)}"
            elif layout == "first":
                core = f"{prefix} {_join(forms)} for {obj}"
            else:
                core = f"{prefix} {_join(forms)}"
        elif group == "method":
            core = {
                "obj": f"{prefix} {obj} following {forms[0]} practices",
                "first": f"{prefix} {forms[0]} practices for {obj}",
                "only": f"{prefix} {forms[0]} practices",
            }[layout]
        else:
            core = {
                "obj": f"{prefix} {obj} for {forms[0]} initiatives",
                "first": f"{prefix} {forms[0]} for {obj}",
                "only": f"{prefix} {forms[0]}",
            }[layout]
    if verb_class is VerbClass.EXPOSURE:
        if quantified:
            raise ValueError("an exposure bullet is never quantified")
        return _cap(core)
    return _cap(core + _tail(rng, fam, quantified))
