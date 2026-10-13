"""Evidence strength: verbs, negation, aspiration, quantification, E and R (SPEC §5.1, §15).

The 100 bullets of tests/fixtures/bullets.yaml are labelled by hand from SPEC §15 and DECISIONS
D-32..D-34a. Expected multipliers are closed-form arithmetic from the spec's tables (written out
below), never values read back from the implementation (EVAL §2.2).
"""

from __future__ import annotations

import ast
import math
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import matcher.score
from matcher.config import EngineConfig, VerbsConfig, load_config
from matcher.errors import EngineFault, Flag
from matcher.models import Evidence, Polarity, VerbClass
from matcher.score.evidence import (
    BulletMention,
    MentionEvidence,
    analyze_bullet,
    apply_polarity,
    evidence_multiplier,
    is_quantified,
    recency_multiplier,
)
from matcher.score.numeric import clamp01, finite

FIXTURE = Path(__file__).parent / "fixtures" / "bullets.yaml"
_FIXTURE_DATA = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
BULLETS: list[dict] = _FIXTURE_DATA["bullets"]  # the SPEC §15.2 set: exactly 100
PROBES: list[dict] = _FIXTURE_DATA["probes"]  # review round 1 regressions (DECISIONS D-34b..D-34f)
LEXICON = load_config()  # read at collection time to parametrise over every configured entry

# SPEC §15.1 / §15.2 / §5.1, written out rather than read from config.
SPEC_VERB = {
    VerbClass.OWNERSHIP: 1.00,
    VerbClass.EXECUTION: 0.85,
    VerbClass.PARTICIPATION: 0.65,
    VerbClass.ASSISTANCE: 0.45,
    VerbClass.EXPOSURE: 0.30,
    VerbClass.NO_VERB: 0.70,
}
QUANTIFIED_BONUS, QUANTIFIED_CAP = 1.15, 1.00
NEGATED_M, ASPIRATIONAL_M = 0.0, 0.15
HARD_TERMS = {"not", "no", "without", "lacking", "never", "no experience"}
ASPIRATIONAL_TERMS = {"seeking", "aspiring", "looking to", "interested in", "eager to learn",
                      "currently learning", "studying", "hope to", "want to"}


def _closed_form(verb_class: VerbClass, polarity: Polarity, quantified: bool) -> float:
    e = min(QUANTIFIED_CAP, SPEC_VERB[verb_class] * QUANTIFIED_BONUS) if quantified else SPEC_VERB[verb_class]
    if polarity is Polarity.NEGATED:
        return NEGATED_M
    if polarity is Polarity.ASPIRATIONAL:
        return min(e, ASPIRATIONAL_M)
    return e


def _span(text: str, surface: str, occurrence: int = 1) -> tuple[int, int]:
    found = list(re.finditer(r"(?<![^\W_])" + re.escape(surface) + r"(?![^\W_])", text))
    assert len(found) >= occurrence, f"{surface!r} occurs fewer than {occurrence} times in {text!r}"
    return found[occurrence - 1].span()


def _mention(text: str, surface: str, occurrence: int = 1) -> BulletMention:
    start, end = _span(text, surface, occurrence)
    canonical = re.sub(r"[^0-9a-z]+", "_", surface.lower()).strip("_") or "skill"
    return BulletMention(canonical_id=canonical, start=start, end=end)


def _one(text: str, surface: str, config: EngineConfig) -> MentionEvidence:
    (evidence,) = analyze_bullet(text, [_mention(text, surface)], config)
    return evidence


def _replace(config: EngineConfig, **sections) -> EngineConfig:
    fields = {name: getattr(config, name) for name in EngineConfig.model_fields}
    fields.update(sections)
    return EngineConfig(**fields)


# ─── The 100-bullet fixture ──────────────────────────────────────────────────────────────


def test_fixture_holds_100_bullets_labelled_with_spec_arithmetic() -> None:
    assert len(BULLETS) == 100
    texts = [b["text"] for b in BULLETS + PROBES]
    assert len(set(texts)) == len(texts)
    for bullet in BULLETS + PROBES:
        assert isinstance(bullet["quantified"], bool) and bullet["mentions"]
        for label in bullet["mentions"]:
            verb_class, polarity = VerbClass(label["verb_class"]), Polarity(label["polarity"])
            expected = _closed_form(verb_class, polarity, bullet["quantified"])
            assert math.isclose(label["multiplier"], expected, rel_tol=0.0, abs_tol=1e-9), bullet["text"]
            assert (label["verb"] == "") == (verb_class is VerbClass.NO_VERB), bullet["text"]
            assert ("trigger" in label) == (polarity is not Polarity.AFFIRMED), bullet["text"]


def test_fixture_covers_classes_forms_polarities_and_terms(config) -> None:
    labels = [m for b in BULLETS for m in b["mentions"]]
    assert {VerbClass(m["verb_class"]) for m in labels} == set(VerbClass)
    assert {Polarity(m["polarity"]) for m in labels} == set(Polarity)
    triggers = {m.get("trigger") for m in labels}
    assert HARD_TERMS <= triggers and ASPIRATIONAL_TERMS <= triggers
    verbs = {m["verb"] for m in labels}
    # past, base, third person and gerund of one lemma per class (verbs.yaml lists all four)
    for forms in ({"architected", "architect", "architects", "architecting"},
                  {"developed", "develop", "develops", "developing"},
                  {"used", "use", "uses", "using"},
                  {"assisted", "assist", "assists", "assisting"},
                  {"exposed", "exposure", "familiar", "familiarity", "aware", "awareness",
                   "coursework in", "basic knowledge"}):
        assert forms <= verbs, sorted(forms - verbs)
    assert {b["quantified"] for b in BULLETS} == {True, False}
    assert any(len(b["mentions"]) >= 3 for b in BULLETS)


@pytest.mark.parametrize(
    "bullet",
    BULLETS + PROBES,
    ids=[f"b{i:03d}" for i in range(1, len(BULLETS) + 1)] + [f"p{i:02d}" for i in range(1, len(PROBES) + 1)],
)
def test_bullet(bullet: dict, config) -> None:
    text = bullet["text"]
    mentions = [_mention(text, m["surface"], m.get("occurrence", 1)) for m in bullet["mentions"]]
    got = analyze_bullet(text, mentions, config)
    assert len(got) == len(mentions)
    assert is_quantified(text, config) is bullet["quantified"], text
    evidence = Evidence.ROLE_BULLET_QUANTIFIED if bullet["quantified"] else Evidence.ROLE_BULLET
    for label, mention, ev in zip(bullet["mentions"], mentions, got):
        where = f"{label['surface']!r} in {text!r}"
        assert (ev.canonical_id, ev.start, ev.end) == (mention.canonical_id, mention.start, mention.end)
        assert ev.verb_class is VerbClass(label["verb_class"]), where
        assert ev.verb == label["verb"], where
        assert ev.polarity is Polarity(label["polarity"]), where
        assert ev.trigger == label.get("trigger", ""), where
        value = apply_polarity(evidence_multiplier(evidence, ev.verb_class, config), ev.polarity, config)
        assert math.isclose(value, label["multiplier"], rel_tol=0.0, abs_tol=1e-9), where


# ─── Lexicons are config, every entry works ─────────────────────────────────────────────


_VERB_ENTRIES = [(cls, entry) for cls, cfg in LEXICON.verbs.classes.items() for entry in cfg.verbs]


@pytest.mark.parametrize("verb_class,entry", _VERB_ENTRIES, ids=[e for _, e in _VERB_ENTRIES])
def test_every_lexicon_entry_attaches_its_class(verb_class: VerbClass, entry: str, config) -> None:
    # Clause-initial and followed by "with": every form and noun entry is in attaching position.
    for written in (entry, entry.capitalize(), entry.upper()):
        ev = _one(f"{written} with Python", "Python", config)
        assert ev.verb_class is verb_class, written
        assert ev.verb == " ".join(entry.lower().split())
        assert ev.polarity is Polarity.AFFIRMED


@pytest.mark.parametrize("form,verb_class", [
    ("design", VerbClass.OWNERSHIP), ("designs", VerbClass.OWNERSHIP), ("lead", VerbClass.OWNERSHIP),
    ("drive", VerbClass.OWNERSHIP), ("engineers", VerbClass.EXECUTION), ("scale", VerbClass.EXECUTION),
    ("work", VerbClass.PARTICIPATION), ("use", VerbClass.PARTICIPATION), ("support", VerbClass.ASSISTANCE),
    ("helps", VerbClass.ASSISTANCE),
])
def test_base_and_third_person_forms_attach_only_in_verb_position(form: str, verb_class: VerbClass,
                                                                   config) -> None:
    # DECISIONS D-34e: mid-clause they are nouns ("design docs", "support tickets", "5 engineers")
    assert _one(f"Wrote {form} notes in Go", "Go", config).verb_class is VerbClass.NO_VERB
    assert _one(f"Learned to {form} in Go", "Go", config).verb_class is verb_class  # right after "to"
    assert _one(f"{form.capitalize()} reports in Go", "Go", config).verb_class is verb_class  # first word
    # past and -ing forms attach anywhere
    assert _one("Wrote notes on tools designed in Go", "Go", config).verb_class is VerbClass.OWNERSHIP
    assert _one("Wrote notes while designing tools in Go", "Go", config).verb_class is VerbClass.OWNERSHIP


def test_noun_entries_attach_only_before_of_to_with_in(config) -> None:
    assert _one("Gained exposure to Kafka", "Kafka", config).verb_class is VerbClass.EXPOSURE
    assert _one("Ran exposure campaigns with Kafka", "Kafka", config).verb_class is VerbClass.NO_VERB
    assert _one("Grew brand awareness of Canva", "Canva", config).verb_class is VerbClass.EXPOSURE


def test_hyphen_splitting_keeps_prefixed_verbs(config) -> None:
    ev = _one("Co-led a Flutter workshop", "Flutter", config)
    assert (ev.verb_class, ev.verb) == (VerbClass.OWNERSHIP, "led")


_TERMS = [(Polarity.NEGATED, t) for t in LEXICON.negation.hard_negation.terms] + [
    (Polarity.ASPIRATIONAL, t) for t in LEXICON.negation.aspirational.terms
]


@pytest.mark.parametrize("polarity,term", _TERMS, ids=[t for _, t in _TERMS])
def test_every_negation_term_fires(polarity: Polarity, term: str, config) -> None:
    ev = _one(f"{term.capitalize()} Python", "Python", config)
    assert ev.polarity is polarity
    assert ev.trigger == term


def test_hard_negation_beats_aspiration_in_one_window(config) -> None:
    # The aspiration term sits inside the negation's scope (DECISIONS D-34c).
    ev = _one("Not interested in Java", "Java", config)
    assert (ev.polarity, ev.trigger) == (Polarity.NEGATED, "not")


@pytest.mark.parametrize("text", [
    *(f"No {word} experience with Docker" for word in (
        "direct", "commercial", "practical", "production", "real", "industry", "extensive", "significant",
        "relevant")),
    "Not very familiar with Docker", "Not currently using Docker", "Have not previously used Docker",
    "Never before used Docker", "Never really used Docker", "Never once used Docker",
    "Not too familiar with Docker", "Not fully familiar with Docker", "Not much experience with Docker",
])
def test_ordinary_modifiers_stay_in_negation_scope(text: str, config) -> None:
    assert _one(text, "Docker", config).polarity is Polarity.NEGATED, text


@pytest.mark.parametrize("text", [
    "Shipped with no production downtime using Kubernetes", "Shipped with no real downtime using Kubernetes",
])
def test_a_filler_never_carries_negation_past_a_noun(text: str, config) -> None:
    assert _one(text, "Kubernetes", config).polarity is Polarity.AFFIRMED


def test_leverage_behaves_like_using(config) -> None:
    ev = _one("Built dashboards leveraging Looker", "Looker", config)
    assert (ev.verb_class, ev.verb) == (VerbClass.OWNERSHIP, "built")  # defers to the earlier verb
    ev = _one("Leveraged Snowflake for reporting", "Snowflake", config)
    assert (ev.verb_class, ev.verb) == (VerbClass.PARTICIPATION, "leveraged")  # nothing earlier


def test_a_deferring_participle_attaches_when_nothing_is_inherited(config) -> None:
    # D-34f: "using" alone leaves the clause verbless, but an inherited NO_VERB does not erase it
    ev = _one("Shipped features and reports using Tableau", "Tableau", config)
    assert (ev.verb_class, ev.verb) == (VerbClass.PARTICIPATION, "using")


def test_contractions_count_as_not(config) -> None:
    for text in ("Haven't worked with Docker", "Don\u2019t have experience with Docker", "Didn't use Docker"):
        ev = _one(text, "Docker", config)
        assert (ev.polarity, ev.trigger) == (Polarity.NEGATED, "not"), text


def test_polarity_stops_at_sentence_ends_and_but_yet(config) -> None:
    # DECISIONS D-34b: the list still takes the verb class, never the negation, across ; and yet
    for text in ("Not familiar with Docker; Kubernetes", "Not familiar with Docker, yet Kubernetes"):
        docker, kubernetes = analyze_bullet(text, [_mention(text, "Docker"), _mention(text, "Kubernetes")], config)
        assert docker.polarity is Polarity.NEGATED, text
        assert (kubernetes.verb_class, kubernetes.polarity) == (VerbClass.EXPOSURE, Polarity.AFFIRMED), text


def test_after_punctuation_only_a_pure_list_inherits(config) -> None:
    # DECISIONS D-34f: and/or/nor hand on the verb class whatever follows; a comma does not
    assert _one("Built dashboards, reports in Tableau", "Tableau", config).verb_class is VerbClass.NO_VERB
    assert _one("Built dashboards or reports in Tableau", "Tableau", config).verb_class is VerbClass.OWNERSHIP
    assert _one("Built dashboards. And reports in Tableau", "Tableau", config).verb_class is VerbClass.NO_VERB


def test_a_new_verb_in_config_attaches(config) -> None:
    text = "Shipped features in Go"
    assert _one(text, "Go", config).verb_class is VerbClass.NO_VERB
    data = config.verbs.model_dump()
    data["classes"][VerbClass.EXECUTION]["verbs"] = (*data["classes"][VerbClass.EXECUTION]["verbs"], "shipped")
    custom = _replace(config, verbs=VerbsConfig(**data))
    ev = _one(text, "Go", custom)
    assert (ev.verb_class, ev.verb) == (VerbClass.EXECUTION, "shipped")


def test_window_size_comes_from_config(config) -> None:
    text = "Looking to grow my skills in Rust"  # "Looking to" ends 5 tokens before "Rust"
    assert _one(text, "Rust", config).polarity is Polarity.ASPIRATIONAL
    narrow = _replace(config, negation=config.negation.model_copy(update={"window_tokens": 3}))
    assert _one(text, "Rust", narrow).polarity is Polarity.AFFIRMED


def test_scope_fillers_and_exceptions_come_from_config(config) -> None:
    hard = config.negation.hard_negation

    def with_hard(**changes) -> EngineConfig:
        return _replace(config, negation=config.negation.model_copy(
            update={"hard_negation": hard.model_copy(update=changes)}))

    assert _one("No formal Java training", "Java", config).polarity is Polarity.NEGATED
    assert _one("No formal training in Java", "Java", config).polarity is Polarity.AFFIRMED  # "training"
    wider = with_hard(scope_fillers=(*hard.scope_fillers, "training"))
    assert _one("No formal training in Java", "Java", wider).polarity is Polarity.NEGATED
    assert _one("Not only shipped features in Java", "Java", config).polarity is Polarity.AFFIRMED
    assert _one("Not Java", "Java", config).polarity is Polarity.NEGATED
    assert _one("Not Java", "Java", with_hard(exceptions=("not",))).polarity is Polarity.AFFIRMED


def test_clause_punctuation_comes_from_config(config) -> None:
    text = "Designed the schema, tuned queries in PostgreSQL"
    assert _one(text, "PostgreSQL", config).verb_class is VerbClass.NO_VERB
    no_comma = config.verbs.model_copy(update={"clause_punctuation": (".", ";")})
    assert _one(text, "PostgreSQL", _replace(config, verbs=no_comma)).verb_class is VerbClass.OWNERSHIP


def test_list_continuation_can_be_switched_off(config) -> None:
    off = _replace(config, verbs=config.verbs.model_copy(update={"list_continuation_inherits_verb": False}))
    text = "Built dashboards using React, Redux and D3"
    got = analyze_bullet(text, [_mention(text, s) for s in ("React", "Redux", "D3")], off)
    assert [e.verb_class for e in got] == [VerbClass.OWNERSHIP, VerbClass.NO_VERB, VerbClass.NO_VERB]
    text = "Not familiar with Docker or Kubernetes"
    docker, kubernetes = analyze_bullet(text, [_mention(text, "Docker"), _mention(text, "Kubernetes")], off)
    assert docker.polarity is Polarity.NEGATED
    assert (kubernetes.verb_class, kubernetes.polarity) == (VerbClass.NO_VERB, Polarity.AFFIRMED)


# ─── Clause, verb and window rules, one at a time ───────────────────────────────────────


def test_period_is_a_boundary_but_not_inside_a_word_or_after_an_abbreviation(config) -> None:
    assert _one("Led sprint planning. Tested releases with Cypress", "Cypress", config).verb_class is (
        VerbClass.NO_VERB)
    assert _one("Built a Vue.js admin for the Laravel API", "Laravel", config).verb_class is VerbClass.OWNERSHIP
    assert _one("Built version 2.5 of the Laravel API", "Laravel", config).verb_class is VerbClass.OWNERSHIP
    for text in ("Built tools for the U.K. office in Go", "Built tools at Acme Corp. in Go",
                 "Built tools (i.e. CLIs) in Go", "Built tools (e.g. CLIs) in Go"):
        assert _one(text, "Go", config).verb_class is VerbClass.OWNERSHIP, text


def test_conjunctions_split_in_any_case_and_only_as_whole_words(config) -> None:
    ev = _one("Built the frontend AND maintained the Kotlin backend", "Kotlin", config)
    assert (ev.verb_class, ev.verb) == (VerbClass.EXECUTION, "maintained")
    # "brand", "sorting", "nordic" contain and / or / nor but are not conjunctions
    assert _one("Built brand sorting tools for nordic teams in Go", "Go", config).verb_class is (
        VerbClass.OWNERSHIP)


def test_a_phrase_must_lie_wholly_inside_the_window(config) -> None:
    # "Hope" is the 7th token before "Vercel" and "to" the 6th: the phrase is not in the window.
    assert _one("Hope to soon have deployed apps on Vercel", "Vercel", config).polarity is Polarity.AFFIRMED
    assert _one("Hope to have deployed apps on Vercel", "Vercel", config).polarity is Polarity.ASPIRATIONAL


def test_another_mention_counts_as_one_window_token(config) -> None:
    # Before "Go": Hope(5) to(4) use(3) [Amazon Web Services](2) with(1). Counted word by word,
    # "Hope" would be the 7th token and the phrase would leave the window.
    text = "Hope to use Amazon Web Services with Go"
    got = analyze_bullet(text, [_mention(text, s) for s in ("Amazon Web Services", "Go")], config)
    assert [e.polarity for e in got] == [Polarity.ASPIRATIONAL] * 2


def test_words_inside_a_mention_are_never_verbs(config) -> None:
    text = "Used Design Thinking and Python"
    got = analyze_bullet(text, [_mention(text, "Design Thinking"), _mention(text, "Python")], config)
    assert [(e.verb_class, e.verb) for e in got] == [(VerbClass.PARTICIPATION, "used")] * 2


def test_a_verb_after_the_mention_never_attaches(config) -> None:
    assert _one("React apps built nightly", "React", config).verb_class is VerbClass.NO_VERB


def test_inherited_attachment_carries_verb_and_trigger(config) -> None:
    text = "Not familiar with Docker, Helm or Kubernetes"
    got = analyze_bullet(text, [_mention(text, s) for s in ("Docker", "Helm", "Kubernetes")], config)
    assert {(e.verb_class, e.verb, e.polarity, e.trigger) for e in got} == {
        (VerbClass.EXPOSURE, "familiar", Polarity.NEGATED, "not")}


def test_unicode_and_smart_quotes(config) -> None:
    assert _one("Built the team\u2019s café app in Swift", "Swift", config).verb_class is VerbClass.OWNERSHIP
    assert _one("Développé une API en Python", "Python", config).verb_class is VerbClass.NO_VERB


# ─── Shape, validation, determinism ─────────────────────────────────────────────────────


def test_output_follows_input_order_including_duplicates(config) -> None:
    text = "Familiar with Rust but built production services in Go"
    go, rust = _mention(text, "Go"), _mention(text, "Rust")
    got = analyze_bullet(text, (go, rust, go), config)
    assert [e.canonical_id for e in got] == ["go", "rust", "go"]
    assert [e.verb_class for e in got] == [VerbClass.OWNERSHIP, VerbClass.EXPOSURE, VerbClass.OWNERSHIP]
    assert all(isinstance(e, MentionEvidence) for e in got)


def test_overlapping_mentions_share_one_attachment(config) -> None:
    text = "Built apps in React Native"
    start, end = _span(text, "React Native")
    got = analyze_bullet(text, [BulletMention(canonical_id="react_native", start=start, end=end),
                                BulletMention(canonical_id="react", start=start, end=start + 5)], config)
    assert [(e.verb_class, e.verb) for e in got] == [(VerbClass.OWNERSHIP, "built")] * 2


def test_no_mentions_and_empty_text(config) -> None:
    assert analyze_bullet("Built dashboards in React", [], config) == ()
    assert analyze_bullet("", [], config) == ()


def test_a_span_outside_the_bullet_is_rejected(config) -> None:
    with pytest.raises(ValueError, match="outside"):
        analyze_bullet("Built it in Go", [BulletMention(canonical_id="go", start=12, end=40)], config)


@pytest.mark.parametrize("fields", [
    {"canonical_id": "go", "start": 3, "end": 3},
    {"canonical_id": "go", "start": 4, "end": 2},
    {"canonical_id": "go", "start": -1, "end": 2},
    {"canonical_id": "", "start": 0, "end": 2},
])
def test_bullet_mention_validates_its_span(fields: dict) -> None:
    with pytest.raises(ValidationError):
        BulletMention(**fields)


@pytest.mark.slow
@pytest.mark.parametrize("unit,verb_class,polarity", [
    ("built Go ", VerbClass.OWNERSHIP, Polarity.AFFIRMED),  # one clause: 20,000 verbs, 20,000 mentions
    ("never Go ", VerbClass.NO_VERB, Polarity.NEGATED),  # a negation before every mention
    ("Go, ", VerbClass.NO_VERB, Polarity.AFFIRMED),  # 20,000 list-continuation clauses
])
def test_long_bullets_take_linear_time(unit: str, verb_class: VerbClass, polarity: Polarity, config) -> None:
    # ~0.5 s linear; matching every mention against every verb took ~42 s at this size. The bound
    # leaves 40x headroom for a loaded runner.
    n, offset = 20_000, unit.index("Go")
    mentions = [BulletMention(canonical_id="go", start=i * len(unit) + offset, end=i * len(unit) + offset + 2)
                for i in range(n)]
    started = time.perf_counter()
    got = analyze_bullet(unit * n, mentions, config)
    assert time.perf_counter() - started < 20.0
    assert len(got) == n and {(e.verb_class, e.polarity) for e in got} == {(verb_class, polarity)}


@pytest.mark.slow
def test_long_text_is_quantified_in_linear_time(config) -> None:
    # ~2 s linear; re-slicing the rest of the tokens per number took ~50 s at this size.
    text = "2 " * 200_000
    started = time.perf_counter()
    assert is_quantified(text, config) is False
    assert time.perf_counter() - started < 25.0
    assert is_quantified(text + "2 users", config) is True


def test_lexicons_are_built_once_per_config(config) -> None:
    import matcher.score.evidence as evidence

    assert evidence._lexicons(config) is evidence._lexicons(config)
    data = config.verbs.model_dump()
    data["classes"][VerbClass.EXECUTION]["verbs"] = (*data["classes"][VerbClass.EXECUTION]["verbs"], "shipped")
    assert evidence._lexicons(_replace(config, verbs=VerbsConfig(**data))) is not evidence._lexicons(config)


def test_analysis_is_deterministic(config) -> None:
    for bullet in BULLETS:
        text = bullet["text"]
        mentions = [_mention(text, m["surface"], m.get("occurrence", 1)) for m in bullet["mentions"]]
        assert analyze_bullet(text, mentions, config) == analyze_bullet(text, mentions, config)


# ─── is_quantified (SPEC: ROLE_BULLET_QUANTIFIED) ───────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Cut costs by 30%",
    "Grew retention 12 % in two quarters",
    "Increased conversion 30 percent",
    "Achieved 99.9% uptime",
    "Saved $2,000 per month",
    "Raised €50k for the club",
    "Handled 10K requests per second",
    "Processed 2.5M records nightly",
    "Indexed 1.2B rows",
    "Made queries 3x faster",
    "Made queries 3\u00d7 faster",
    "Ran the suite 4 times a day",
    "Served 500+ customers",
    "Onboarded 25 users",
    "Led 3 cross-functional teams",
    "Supported 2000 employees",
    "Shipped 12 features",
    "Reduced latency by 120ms",
    "Improved accuracy to 0.92",
    "Cut review time to 1.5 days",
    "Decreased churn by 3",
    "Boosted NPS by 12 points",
    "Lowered costs to $3 per order",
    "Grew followers to 2,500",
    "Scaled 10-15 services",
    "Built a tool. Saved 10 hours a week",
    "Made builds 10X faster",
    "Served 1000s of users",
    "Served 1,000s of users",
    "Reached 3 million readers",
    "Indexed 2 billion pages",
    "Answered 5 thousand questions",
    "Led a team of 4",
    "Tutored 15 Grade 9 students",
    "Supported 40 Fortune 500 clients",
    "Deployed to 300 Windows 10 machines",
    # fix round 2: a lower-case word before a number is a count unless it is a version name
    "Develop and maintain 12 microservices",
    "Design and build 12 dashboards",
    "Currently manage 12 servers",
    "Lead and mentor 5 engineers",
    "Train and support 40 users",
    "Develop and serve 500+ customers",
    "Migrated all 40 services to Kubernetes",
    "Fixed all 12 bugs before launch",
    "Reviewed all 200 pull requests",
    "Supported our 40 clients",
])
def test_quantified(text: str, config) -> None:
    assert is_quantified(text, config) is True


def test_version_names_come_from_config(config) -> None:
    text = "Shipped react 18 apps"
    assert is_quantified(text, config) is False
    data = config.verbs.model_dump()
    data["version_names"] = tuple(name for name in data["version_names"] if name != "react")
    assert is_quantified(text, _replace(config, verbs=VerbsConfig(**data))) is True


def test_counting_nouns_come_from_config(config) -> None:
    # The counting-noun lexicon is config (SPEC §15: lexicons are config, not code), like version_names.
    text = "Taught 15 students"
    assert is_quantified(text, config) is True
    data = config.verbs.model_dump()
    assert {"students", "locations", "servers", "clients"} <= set(data["counting_nouns"])
    data["counting_nouns"] = tuple(noun for noun in data["counting_nouns"] if noun != "students")
    assert is_quantified(text, _replace(config, verbs=VerbsConfig(**data))) is False


@pytest.mark.parametrize("text", [
    "Built a dashboard in React 18",
    "Migrated services to Python 3",
    "Built React 18 apps for the marketing site",
    "Developed microservices in Java 8+",
    "Shipped ES6 modules and HTML5 pages",
    "Stored files in S3 and EC2",
    "Interned at a fintech in 2023",
    "Grew the team by 2024",
    "Improved onboarding from 2019 to 2021",
    "Mentored 1 student",
    "Spent 1.5 days per release",
    "Maintained 3 large legacy servers",
    "Built a 3D renderer",
    "Ranked 2nd in the hackathon",
    "Reduced costs significantly",
    "Built apps for Windows 10 users",
    "Worked 24/7 support shifts",
    "Shipped react 18 apps",
    "Built services in java 8+",
    "Upgraded python 3 services",
    "Grew up in the 1990s",
    "Worked in a team of 1",
    "",
])
def test_not_quantified(text: str, config) -> None:
    assert is_quantified(text, config) is False


# ─── E (DECISIONS D-32) ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("verb_class", list(VerbClass))
def test_evidence_table(verb_class: VerbClass, config) -> None:
    def e(evidence: Evidence) -> float:
        return evidence_multiplier(evidence, verb_class, config)

    assert math.isclose(e(Evidence.SKILLS_BLOB), 0.65)
    assert math.isclose(e(Evidence.ROLE_TITLE), 1.00)
    assert math.isclose(e(Evidence.CERTIFICATION), 1.00)
    assert math.isclose(e(Evidence.ROLE_BULLET), SPEC_VERB[verb_class])
    assert math.isclose(e(Evidence.ROLE_BULLET_QUANTIFIED), min(1.0, SPEC_VERB[verb_class] * 1.15))


def test_quantified_bonus_and_cap(config) -> None:
    assert math.isclose(evidence_multiplier(Evidence.ROLE_BULLET_QUANTIFIED, VerbClass.EXECUTION, config),
                        0.9775, abs_tol=1e-12)  # 0.85 x 1.15
    assert math.isclose(evidence_multiplier(Evidence.ROLE_BULLET_QUANTIFIED, VerbClass.OWNERSHIP, config),
                        1.0, abs_tol=0.0)  # 1.00 x 1.15 = 1.15, capped
    assert math.isclose(evidence_multiplier(Evidence.ROLE_BULLET_QUANTIFIED, VerbClass.NO_VERB, config),
                        0.805, abs_tol=1e-12)


# ─── R (SPEC §5.1, DECISIONS D-34) ──────────────────────────────────────────────────────


def test_recency(config) -> None:
    as_of = date(2026, 10, 8)

    def r(days_ago: int) -> float:
        return recency_multiplier(as_of - timedelta(days=days_ago), as_of, config)

    assert math.isclose(recency_multiplier(None, as_of, config), 1.0)  # undated: never penalised
    assert math.isclose(r(0), 1.0)
    two_years = date(2024, 10, 8)
    assert (as_of - two_years).days == 730
    assert math.isclose(recency_multiplier(two_years, as_of, config), math.exp(-0.15 * 730 / 365.25),
                        abs_tol=1e-12)
    assert math.isclose(recency_multiplier(two_years, as_of, config), math.exp(-0.3), abs_tol=1e-3)  # 0.740818
    assert math.exp(-0.15 * 4) < 0.55  # 0.548812: four years is already at the floor
    assert math.isclose(r(1461), 0.55)  # 1461 days = 4 x 365.25
    assert math.isclose(r(3653), 0.55)  # ten years
    assert math.isclose(recency_multiplier(as_of + timedelta(days=90), as_of, config), 1.0)  # future: 0 years


# ─── Polarity overrides (DECISIONS D-33) ────────────────────────────────────────────────


def test_apply_polarity(config) -> None:
    assert apply_polarity(0.85, Polarity.NEGATED, config) == 0.0
    assert math.isclose(apply_polarity(0.70, Polarity.ASPIRATIONAL, config), 0.15)
    assert math.isclose(apply_polarity(0.10, Polarity.ASPIRATIONAL, config), 0.10)  # min, never raised
    assert math.isclose(apply_polarity(0.85, Polarity.AFFIRMED, config), 0.85)
    for polarity in Polarity:
        with pytest.raises(EngineFault) as err:
            apply_polarity(math.nan, polarity, config)
        assert err.value.flag is Flag.NUMERIC_FAULT


# ─── numeric helpers ────────────────────────────────────────────────────────────────────


def test_finite_and_clamp01() -> None:
    assert finite(0.25) == 0.25 and finite(3) == 3.0
    assert clamp01(-0.5) == 0.0 and clamp01(1.5) == 1.0 and clamp01(0.4) == 0.4
    assert math.copysign(1.0, clamp01(-0.0)) == 1.0  # -0.0 never reaches serialised output
    for bad in (math.nan, math.inf, -math.inf, "0.5", None, True, 10**400):
        for helper in (finite, clamp01):
            with pytest.raises(EngineFault) as err:
                helper(bad)
            assert err.value.flag is Flag.NUMERIC_FAULT


# ─── score/ stays pure (SPEC §1) ────────────────────────────────────────────────────────

# matcher.explain: the engine attaches the explanation layer, which is pure too (tests/test_breakdown.py).
_ALLOWED_MATCHER = ("matcher.models", "matcher.errors", "matcher.config", "matcher.graph", "matcher.score",
                    "matcher.explain")
_IMPURE_STDLIB = {"os", "io", "pathlib", "shutil", "tempfile", "glob", "socket", "subprocess", "urllib",
                  "http", "time", "random", "secrets", "sqlite3"}
_CLOCK_METHODS = {"today", "now", "utcnow"}  # date.today(), datetime.now(), datetime.utcnow()
_IO_BUILTINS = {"open", "input", "print"}


def _impure_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _IO_BUILTINS
    if isinstance(func, ast.Attribute):
        if func.attr in _CLOCK_METHODS:
            return True
        return isinstance(func.value, ast.Name) and func.value.id in {"time", "random"}  # time.time(), ...
    return False


def test_score_package_imports_only_pure_modules() -> None:
    root = Path(matcher.score.__file__).parent
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                assert not _impure_call(node), f"{path.name}:{node.lineno} reads the clock or does I/O"
                continue
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top == "matcher":
                    assert name.startswith(_ALLOWED_MATCHER), f"{path.name} imports {name}"
                else:
                    assert top == "pydantic" or top in sys.stdlib_module_names, f"{path.name} imports {name}"
                    assert top not in _IMPURE_STDLIB, f"{path.name} imports {name}"


@pytest.mark.parametrize("snippet,impure", [
    ("date.today()", True), ("dt.datetime.now()", True), ("datetime.utcnow()", True), ("time.time()", True),
    ("open('x')", True), ("random.random()", True), ("print(1)", True),
    ("math.exp(1)", False), ("value.end()", False),
])
def test_purity_guard_catches_clock_and_io(snippet: str, impure: bool) -> None:
    call = ast.parse(snippet, mode="eval").body
    assert isinstance(call, ast.Call) and _impure_call(call) is impure
