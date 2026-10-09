"""Surface forms to canonical ids (SPEC §3.3): normalisation, aliases, bounded fuzzy, extraction."""

from __future__ import annotations

import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from matcher.graph.aliases import normalize_surface, surface_variants
from matcher.graph.model import SkillGraph, SkillNode
from matcher.models import SkillKind
from matcher.normalize.fuzzy import FuzzyHit, FuzzyLog, bounded_levenshtein
from matcher.normalize.resolver import Mention, Resolution, Resolver

AMBIGUOUS = {"go": "Go", "r": "R", "rest": "REST", "spring": "Spring", "excel": "Excel", "swift": "Swift",
             "rust": "Rust"}


@pytest.fixture(scope="module")
def resolver(small_graph, config) -> Resolver:
    return Resolver(small_graph, config, ambiguous=AMBIGUOUS)


def _found(mentions: tuple[Mention, ...]) -> list[tuple[str, str, str]]:
    return [(m.canonical_id, m.surface, m.method) for m in mentions]


# ─── normalize_surface ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("React.js", "react.js"),
        (" Node.JS ", "node.js"),
        ("C++", "c++"),
        ("C#", "c#"),
        (".NET", ".net"),
        ("CI/CD", "ci/cd"),
        ("Scikit-Learn", "scikit-learn"),
        ("Microsoft Office\u00ae", "microsoft office"),
        ("\u201cAgile\u201d", "agile"),
        ("Python,", "python"),
        ("Node.js.", "node.js"),
        ("Python (computer programming)", "python computer programming"),
        ("\ufb01nance", "finance"),  # ligature, NFKC
        ("soft\u00adware", "software"),  # soft hyphen
        ("zero\u200bwidth", "zerowidth"),  # zero-width space
        ("Front\u2013End", "front-end"),  # en dash
        ("O'Reilly", "oreilly"),
        ("Google's", "googles"),
        ("O\u2019Reilly", "oreilly"),  # curly apostrophe
        ("Java\u2122", "java"),  # NFKC would spell the trade mark sign "TM"
        ("  [Docker] | {Kubernetes}; Terraform! ", "docker kubernetes terraform"),
        ("-Python-", "python"),
        ("ci/cd/", "ci/cd"),
        ("Python...", "python"),
        ("", ""),
    ],
)
def test_normalize_surface_table(raw: str, expected: str) -> None:
    assert normalize_surface(raw) == expected


_ALPHABET = (
    string.ascii_letters
    + string.digits
    + " .-/_+#&'()[]{},;:!?\"*|"
    + "\u2018\u2019\u201c\u201d\u2013\u2014\u00ad\u200b\ufeff\u00ae\u2122\u00a9\ufb01\u00a0"
)


@given(st.text(alphabet=_ALPHABET, max_size=40))
def test_normalize_surface_is_idempotent_and_tidy(text: str) -> None:
    once = normalize_surface(text)
    assert normalize_surface(once) == once
    assert once == " ".join(once.split())


def _reference_normalize(text: str, fold: bool) -> str:
    """The Unicode pipeline the ASCII fast path in graph.aliases must agree with."""
    import unicodedata

    from matcher.graph import aliases

    text = unicodedata.normalize("NFKC", text.translate(aliases._BEFORE_NFKC)).translate(aliases._TABLE)
    if fold:
        text = text.casefold()
    tokens = (token.lstrip(aliases._LEAD).rstrip(aliases._TRAIL) for token in text.split())
    return " ".join(token for token in tokens if token)


@given(st.text(alphabet=string.printable + _ALPHABET, max_size=60))
def test_the_fast_paths_agree_with_the_unicode_pipeline(text: str) -> None:
    from matcher.graph.aliases import normalize_keep_case

    assert normalize_surface(text) == _reference_normalize(text, fold=True)
    assert normalize_keep_case(text) == _reference_normalize(text, fold=False)


def test_surface_variants() -> None:
    assert surface_variants("front-end") == ("front-end", "front end", "frontend")
    assert surface_variants("front-end developer") == ("front-end developer", "front end developer",
                                                       "frontend developer")
    assert surface_variants("python") == ("python",)
    assert surface_variants("react native") == ("react native",)
    assert surface_variants("") == ()


# ─── bounded fuzzy matching ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b,max_distance,expected",
    [
        ("kitten", "sitting", 3, 3),
        ("kitten", "sitting", 2, None),
        ("flaw", "lawn", 2, 2),
        ("kubernets", "kubernetes", 2, 1),
        ("", "", 0, 0),
        ("abc", "", 3, 3),
        ("abc", "", 2, None),
        ("abcdef", "abcdef", 0, 0),
        ("abcdef", "abcdeg", 0, None),
    ],
)
def test_bounded_levenshtein(a: str, b: str, max_distance: int, expected) -> None:
    assert bounded_levenshtein(a, b, max_distance) == expected
    assert bounded_levenshtein(b, a, max_distance) == expected


def _levenshtein(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


@given(st.text(alphabet="abc", max_size=8), st.text(alphabet="abc", max_size=8), st.integers(0, 3))
def test_bounded_levenshtein_agrees_with_the_full_distance(a: str, b: str, max_distance: int) -> None:
    full = _levenshtein(a, b)
    assert bounded_levenshtein(a, b, max_distance) == (full if full <= max_distance else None)


def test_bounded_levenshtein_rejects_negative_bound() -> None:
    with pytest.raises(ValueError):
        bounded_levenshtein("a", "b", -1)


def test_fuzzy_log_records_reviewable_lines() -> None:
    log = FuzzyLog()
    log.record(FuzzyHit(surface="Kubernets\tcluster", canonical_id="skill.kubernetes", distance=1))
    assert log.hits == [FuzzyHit(surface="Kubernets\tcluster", canonical_id="skill.kubernetes", distance=1)]
    assert log.lines() == ["Kubernets cluster\tskill.kubernetes\t1"]


# ─── resolve ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "surface,canonical_id,method",
    [
        ("React", "skill.react", "EXACT"),
        ("  REACT  ", "skill.react", "EXACT"),
        ("Node.js", "skill.nodejs", "EXACT"),
        ("React Native", "skill.react_native", "EXACT"),
        ("\u201cAgile\u201d", "skill.agile", "EXACT"),
        ("Microsoft Excel\u00ae", "skill.excel", "EXACT"),
        ("ReactJS", "skill.react", "ALIAS"),
        ("React.js", "skill.react", "ALIAS"),
        ("NodeJS", "skill.nodejs", "ALIAS"),
        ("Postgres", "skill.postgresql", "ALIAS"),
        ("k8s", "skill.kubernetes", "ALIAS"),
        ("Golang", "skill.go", "ALIAS"),
        ("Python3", "skill.python", "ALIAS"),
        ("AWS SAA", "cred.aws_saa", "ALIAS"),
        ("AWS Certified Solutions Architect - Associate", "cred.aws_saa", "EXACT"),
    ],
)
def test_resolve_exact_and_alias(resolver: Resolver, surface: str, canonical_id: str, method: str) -> None:
    assert resolver.resolve(surface) == Resolution(surface=surface, canonical_id=canonical_id, method=method)


@pytest.mark.parametrize(
    "surface,method",
    [
        ("Front-end", "ALIAS"),
        ("front end", "ALIAS"),  # a hyphen variant of the "Front-end" alias
        ("Frontend", "ALIAS"),
        ("Front-End Development", "EXACT"),  # its "frontend development" variant is the label
    ],
)
def test_resolve_through_hyphen_variants(resolver: Resolver, surface: str, method: str) -> None:
    assert resolver.resolve(surface) == Resolution(surface=surface, canonical_id="skill.frontend", method=method)


def test_resolve_unknown_surface(resolver: Resolver) -> None:
    for surface in ("", "   ", "underwater basket weaving", "reac"):
        assert resolver.resolve(surface) == Resolution(surface=surface, canonical_id=None, method="NONE")


def test_resolve_fuzzy_hits_are_bounded_and_logged(small_graph, config) -> None:
    log = FuzzyLog()
    r = Resolver(small_graph, config, ambiguous=AMBIGUOUS, fuzzy_log=log)
    assert r.resolve("kubernets") == Resolution(surface="kubernets", canonical_id="skill.kubernetes",
                                                method="FUZZY", distance=1)
    assert r.resolve("Postgress").canonical_id == "skill.postgresql"  # closest key "postgres", not "postgresql"
    assert log.lines() == ["kubernets\tskill.kubernetes\t1", "Postgress\tskill.postgresql\t1"]


def test_resolve_never_fuzzes_short_multiword_or_exact_surfaces(small_graph, config) -> None:
    log = FuzzyLog()
    r = Resolver(small_graph, config, ambiguous=AMBIGUOUS, fuzzy_log=log)
    assert r.resolve("reac").method == "NONE"  # shorter than fuzzy_min_token_length
    assert r.resolve("Flasks").method == "NONE"  # 6 chars, but the only close key ("flask") is too short
    assert r.resolve("kubernets cluster").method == "NONE"  # not a single token
    assert r.resolve("Kubernetes").method == "EXACT"
    assert r.resolve("Postgres").method == "ALIAS"
    assert r.resolve("Terraform").method == "EXACT"
    assert log.hits == []


def test_resolve_reads_fuzzy_bounds_from_config(small_graph, config) -> None:
    taxonomy = config.scoring.taxonomy.model_copy(update={"fuzzy_min_token_length": 10})
    strict = config.model_copy(update={"scoring": config.scoring.model_copy(update={"taxonomy": taxonomy})})
    assert Resolver(small_graph, strict).resolve("kubernets").method == "NONE"  # 9 chars < 10
    taxonomy = config.scoring.taxonomy.model_copy(update={"fuzzy_max_distance": 0})
    exact_only = config.model_copy(update={"scoring": config.scoring.model_copy(update={"taxonomy": taxonomy})})
    assert Resolver(small_graph, exact_only).resolve("kubernets").method == "NONE"


def _node(node_id: str, label: str, *aliases: str, source: str = "esco") -> SkillNode:
    return SkillNode(id=node_id, label=label, kind=SkillKind.TECHNOLOGY, aliases=aliases, source=source)


def _graph(*nodes: SkillNode) -> SkillGraph:
    return SkillGraph({node.id: node for node in nodes}, {}, {}, "test")


def test_fuzzy_ties_prefer_smaller_distance_then_smallest_key(config) -> None:
    r = Resolver(_graph(_node("skill.x", "abcdef"), _node("skill.y", "abcdeg"), _node("skill.z", "abcdefgh")),
                 config)
    assert r.resolve("abcdeh").canonical_id == "skill.x"  # x and y both at 1: "abcdef" < "abcdeg"
    assert r.resolve("abcdefg").canonical_id == "skill.x"  # x, y, z all at 1
    hit = r.resolve("abcdfgh")  # z at 1, y at 2, x at 3
    assert (hit.canonical_id, hit.distance) == ("skill.z", 1)
    # The smallest key wins a tie even when it is longer than the other candidate.
    r = Resolver(_graph(_node("skill.p", "abcdefg"), _node("skill.q", "bcdefh")), config)
    assert r.resolve("bcdefg").canonical_id == "skill.p"  # both at 1; "abcdefg" < "bcdefh"


def test_fuzzy_keeps_hits_exactly_at_the_bound(config) -> None:
    # Two substitutions: two letters only the key has (x, y) and two only the query has (e, f).
    r = Resolver(_graph(_node("skill.k", "abcdxy")), config)
    assert r.resolve("abcdef") == Resolution(surface="abcdef", canonical_id="skill.k", method="FUZZY", distance=2)


@settings(max_examples=300)
@given(st.sets(st.text(alphabet="abcdefgh", min_size=4, max_size=9), min_size=1, max_size=25), st.data())
def test_fuzzy_agrees_with_a_brute_force_scan(config, labels: set[str], data) -> None:
    # The resolver prunes candidates before the DP; the pruning must never change the answer.
    # Queries are labels with up to 3 random edits, some introducing letters no label has.
    query = data.draw(st.sampled_from(sorted(labels)))
    for _ in range(data.draw(st.integers(0, 3))):
        at = data.draw(st.integers(0, len(query)))
        char = data.draw(st.sampled_from("abcdefghij"))
        operation = data.draw(st.sampled_from(["insert", "delete", "substitute"]))
        if operation == "insert":
            query = query[:at] + char + query[at:]
        else:
            query = query[:at] + (char if operation == "substitute" else "") + query[at + 1:]
    found = Resolver(_graph(*(_node(f"skill.{label}", label) for label in labels)), config).resolve(query)
    near = []
    for label in labels:
        edits = bounded_levenshtein(query, label, 2)
        if len(query) >= 6 and len(label) >= 6 and edits is not None:
            near.append((edits, label))
    if query in labels:
        assert found.method == "EXACT"
    elif near:
        edits, label = min(near)
        assert (found.method, found.canonical_id, found.distance) == ("FUZZY", f"skill.{label}", edits)
    else:
        assert found.method == "NONE"


def test_conflicting_keys_go_to_the_curated_node_then_the_smallest_id(config) -> None:
    plain = _graph(_node("skill.b", "Spark B", "Spark"), _node("skill.a", "Spark A", "Spark"))
    assert Resolver(plain, config).resolve("Spark").canonical_id == "skill.a"
    curated = _graph(_node("skill.b", "Spark B", "Spark", source="curated"), _node("skill.a", "Spark A", "Spark"))
    assert Resolver(curated, config).resolve("Spark").canonical_id == "skill.b"


def test_a_direct_claim_beats_a_hyphen_variant(config) -> None:
    r = Resolver(_graph(_node("skill.a", "Online retail", "e-commerce"), _node("skill.b", "Digital sales", "ecommerce")),
                 config)
    assert r.resolve("ecommerce").canonical_id == "skill.b"  # b lists it; a only reaches it by dropping the hyphen
    assert r.resolve("e commerce").canonical_id == "skill.a"
    assert r.resolve("e-commerce").canonical_id == "skill.a"


def test_ambiguous_keys_need_their_exact_casing_in_resolve(resolver: Resolver) -> None:
    assert resolver.resolve("Go") == Resolution(surface="Go", canonical_id="skill.go", method="EXACT")
    assert resolver.resolve(" Go ").canonical_id == "skill.go"
    assert resolver.resolve("R").canonical_id == "skill.r"
    assert resolver.resolve("Excel").canonical_id == "skill.excel"
    for surface in ("go", "GO", "r", "excel"):
        assert resolver.resolve(surface).method == "NONE", surface
    assert resolver.resolve("golang").canonical_id == "skill.go"  # only the ambiguous key is guarded


def test_ambiguous_keys_never_fuzzy_match(config) -> None:
    graph = _graph(_node("skill.spring", "Spring"), _node("skill.strings", "Strings"))
    log = FuzzyLog()
    guarded = Resolver(graph, config, ambiguous={"spring": "Spring"}, fuzzy_log=log)
    assert guarded.resolve("Sprint").method == "NONE"  # distance 1, but "spring" needs its exact casing
    # An exact key that failed its casing is a known word, not a typo: no fuzzy rescue to "strings".
    assert guarded.resolve("spring").method == "NONE"
    assert log.hits == []
    assert Resolver(graph, config).resolve("Sprint").canonical_id == "skill.spring"  # unguarded: fuzzy


# ─── extract ───────────────────────────────────────────────────────────────────────────────

TEXT = (
    "Senior engineer: React/Redux, Node.js and TypeScript; ran Kubernetes (k8s) on AWS. "
    "Built CI/CD with Docker, wrote scikit-learn models in Python3 and R."
)


def test_extract_finds_every_mention_with_exact_offsets(resolver: Resolver) -> None:
    mentions = resolver.extract(TEXT)
    assert _found(mentions) == [
        ("skill.react", "React", "EXACT"),
        ("skill.redux", "Redux", "EXACT"),
        ("skill.nodejs", "Node.js", "EXACT"),
        ("skill.typescript", "TypeScript", "EXACT"),
        ("skill.kubernetes", "Kubernetes", "EXACT"),
        ("skill.kubernetes", "k8s", "ALIAS"),
        ("skill.aws", "AWS", "ALIAS"),
        ("skill.ci_cd", "CI/CD", "EXACT"),
        ("skill.docker", "Docker", "EXACT"),
        ("skill.scikit_learn", "scikit-learn", "EXACT"),
        ("skill.python", "Python3", "ALIAS"),
        ("skill.r", "R", "EXACT"),
    ]
    for m in mentions:
        assert TEXT[m.start:m.end] == m.surface
    assert all(a.end <= b.start for a, b in zip(mentions, mentions[1:]))  # sorted, no overlaps


def test_extract_prefers_the_longest_match(resolver: Resolver) -> None:
    assert _found(resolver.extract("Shipped React Native apps.")) == [
        ("skill.react_native", "React Native", "EXACT")
    ]
    assert _found(resolver.extract("Passed the AWS SAA exam.")) == [("cred.aws_saa", "AWS SAA", "ALIAS")]
    assert _found(resolver.extract("Strong front end skills")) == [("skill.frontend", "front end", "ALIAS")]


def test_extract_splits_slash_and_hyphen_tokens_that_do_not_match_whole(resolver: Resolver) -> None:
    text = "Shipped React/Redux UIs and React-based dashboards on CI/CD with scikit-learn and React-Native."
    assert _found(resolver.extract(text)) == [
        ("skill.react", "React", "EXACT"),
        ("skill.redux", "Redux", "EXACT"),
        ("skill.react", "React", "EXACT"),
        ("skill.ci_cd", "CI/CD", "EXACT"),  # matched whole, never split
        ("skill.scikit_learn", "scikit-learn", "EXACT"),
        ("skill.react_native", "React-Native", "ALIAS"),
    ]
    assert _found(resolver.extract("Wrote C++/C# services on .NET.")) == [
        ("skill.cpp", "C++", "EXACT"),
        ("skill.csharp", "C#", "EXACT"),
        ("skill.dotnet", ".NET", "EXACT"),
    ]


def test_extract_drops_a_trailing_period_from_the_mention(resolver: Resolver) -> None:
    text = "Experience with Node.js."
    (mention,) = resolver.extract(text)
    assert (mention.canonical_id, mention.surface, mention.end) == ("skill.nodejs", "Node.js", len(text) - 1)


def test_extract_handles_unicode_dashes_and_invisible_characters(resolver: Resolver) -> None:
    assert _found(resolver.extract("Front\u2013End engineer")) == [("skill.frontend", "Front\u2013End", "ALIAS")]
    assert _found(resolver.extract("Ran Kuber\u00adnetes daily")) == [
        ("skill.kubernetes", "Kuber\u00adnetes", "EXACT")
    ]


@pytest.mark.parametrize(
    "text,surface",
    [
        ("Passed the AWS Certified Solutions Architect \u2013 Associate exam.",
         "AWS Certified Solutions Architect \u2013 Associate"),  # spaced en dash
        ("Passed the AWS Certified Solutions Architect - Associate exam.",
         "AWS Certified Solutions Architect - Associate"),  # spaced ASCII hyphen
        ("Holds the AWS\u00ae Certified Solutions Architect -- Associate credential",
         "AWS\u00ae Certified Solutions Architect -- Associate"),  # registered mark, double hyphen
    ],
)
def test_extract_joins_credential_names_across_dashes_and_marks(
    resolver: Resolver, text: str, surface: str
) -> None:
    assert _found(resolver.extract(text)) == [("cred.aws_saa", surface, "EXACT")]


def test_extract_joins_across_a_colon_and_trade_marks(resolver: Resolver) -> None:
    assert _found(resolver.extract("Earned Microsoft Certified: Azure Fundamentals in 2024.")) == [
        ("cred.azure_fundamentals", "Microsoft Certified: Azure Fundamentals", "EXACT")
    ]
    assert _found(resolver.extract("Expert in Microsoft\u00ae Excel and Microsoft\u2122 Excel")) == [
        ("skill.excel", "Microsoft\u00ae Excel", "EXACT"),
        ("skill.excel", "Microsoft\u2122 Excel", "EXACT"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Skills: React, Native modules",
        "We used React. Native apps came later.",
        "React; Native",
        "React! Native",
        "React? Native",
        "React | Native",
        "React (Native)",
        "React [Native]",
        "React {Native}",
        "React\nNative",
        "React \u2013Native",  # a dash attached to a word is not a standalone separator
    ],
)
def test_extract_does_not_join_tokens_across_hard_breaks(resolver: Resolver, text: str) -> None:
    assert _found(resolver.extract(text)) == [("skill.react", "React", "EXACT")]


def test_extract_reads_through_possessives(resolver: Resolver) -> None:
    assert _found(resolver.extract("Tuned React's rendering and Python's GC.")) == [
        ("skill.react", "React", "EXACT"),
        ("skill.python", "Python", "EXACT"),
    ]
    text = "Profiled React Native\u2019s bridge and Kubernetes\u2019 scheduler. Loved Go's channels."
    mentions = resolver.extract(text)
    assert _found(mentions) == [
        ("skill.react_native", "React Native", "EXACT"),
        ("skill.kubernetes", "Kubernetes", "EXACT"),
        ("skill.go", "Go", "EXACT"),
    ]
    assert all(text[m.start:m.end] == m.surface for m in mentions)


def test_extract_handles_decomposed_accents(config) -> None:
    r = Resolver(_graph(_node("skill.web_dev", "D\u00e9veloppement web")), config)  # NFC label
    text = "Expert en de\u0301veloppement web."  # NFD: "e" + combining acute accent
    assert r.resolve("de\u0301veloppement web").method == "EXACT"
    assert _found(r.extract(text)) == [("skill.web_dev", "de\u0301veloppement web", "EXACT")]


@pytest.mark.parametrize(
    "text,context,expected",
    [
        ("Built services in Go.", "prose", ["skill.go"]),
        ("Go to market strategy", "prose", []),  # first word of the text
        ("Shipped services. Go is our main language.", "prose", []),  # first word of a sentence
        ("Built tools in GO", "prose", []),  # wrong casing
        ("python, go, sql", "prose", ["skill.python", "skill.sql"]),
        ("python, go, sql", "list", ["skill.python", "skill.go", "skill.sql"]),
        ("Go, Python", "list", ["skill.go", "skill.python"]),
        ("Go/Python", "list", ["skill.go", "skill.python"]),
        # Slash parts (D-25): an ambiguous part needs another part that is a non-ambiguous skill,
        # then the usual rules apply, the first part having the token's position.
        ("Analysed survey data with Python/R.", "prose", ["skill.python", "skill.r"]),
        ("Built REST/GraphQL APIs in Java/Spring.", "prose",
         ["skill.rest", "skill.graphql", "skill.java", "skill.spring"]),
        ("Automated reports with Python/Excel.", "prose", ["skill.python", "skill.excel"]),
        ("Analysed data with Python/SAS/R.", "prose", ["skill.python", "skill.r"]),  # one companion is enough
        ("R/Python are my tools.", "prose", ["skill.python"]),  # "R" opens the sentence
        ("Data work in python/r", "prose", ["skill.python"]),  # wrong casing
        ("A/R and A/P", "prose", []),  # no companion skill: accounts receivable, not the language
        ("Reconciled A/R and A/P weekly.", "prose", []),
        ("Go/No-Go", "prose", []),
        ("Chaired the Go/No-Go review.", "prose", []),
        ("R/W", "prose", []),
        ("Granted R/W access to shares.", "prose", []),
        ("Spring/Summer 2027", "prose", []),
        ("Completed a Spring/Summer 2027 co-op term.", "prose", []),  # a term, not the framework
        ("Excel/Word", "prose", []),
        ("Kept the budget in Excel/Word files.", "prose", []),
        # REST, an all-capital acronym, needs no companion; Go still does, and an acronym is none.
        ("Ran Kubernetes-native Go/REST services", "prose", ["skill.kubernetes", "skill.rest"]),
        ("Skills: A/R, A/P, Excel", "list", ["skill.excel"]),  # lists need the companion too
        ("Spring/Summer 2027", "list", []),
        # Hyphen parts: only "<key>-<suffix>" compounds such as "Go-based" (D-25).
        ("Shipped Go-based microservices.", "prose", ["skill.go"]),
        ("Python/R-based tooling", "prose", ["skill.python", "skill.r"]),
        ("Shipped go-based tools", "prose", []),  # the suffix does not waive the casing
        ("Led the Go-Live of SAP.", "prose", []),  # a hyphen phrase, not the language
        ("Owned the Go-to-market plan.", "prose", []),
        ("Shipped Go-only-mode builds", "prose", []),  # three parts: not a "<key>-<suffix>" compound
        ("SAP, Go-Live support", "list", []),  # hyphen phrases stay out of lists too
        ("Go-based services", "list", ["skill.go"]),
        ("Analysed survey data in R and Python.", "prose", ["skill.r", "skill.python"]),
        ("Managed the R&D budget.", "prose", []),
        ("Strong in Excel and R programming", "prose", ["skill.excel", "skill.r"]),
        ("excel at teamwork", "prose", ["skill.teamwork"]),
    ],
)
def test_extract_guards_ambiguous_keys(resolver: Resolver, text: str, context: str, expected: list[str]) -> None:
    assert [m.canonical_id for m in resolver.extract(text, context=context)] == expected


def test_extract_never_fuzzy_matches(small_graph, config) -> None:
    log = FuzzyLog()
    r = Resolver(small_graph, config, ambiguous=AMBIGUOUS, fuzzy_log=log)
    assert r.extract("Deployed kubernets clusters") == ()
    assert log.hits == []


def test_extract_rejects_an_unknown_context(resolver: Resolver) -> None:
    with pytest.raises(ValueError):
        resolver.extract("Python", context="table")  # type: ignore[arg-type]


def test_resolution_is_deterministic_across_node_order(small_graph, config, resolver: Resolver) -> None:
    reversed_graph = SkillGraph(dict(reversed(list(small_graph.nodes.items()))), small_graph.related,
                                small_graph.idf, small_graph.version)
    other = Resolver(reversed_graph, config, ambiguous=dict(reversed(list(AMBIGUOUS.items()))))
    assert resolver.extract(TEXT) == resolver.extract(TEXT) == other.extract(TEXT)
    for surface in ("React", "front end", "kubernets", "Golang", "go", "Excel", "AWS SAA", "nothing at all"):
        assert resolver.resolve(surface) == other.resolve(surface), surface


# ─── extraction precision rules (DECISIONS D-27) ────────────────────────────────────────────


def _ids(r: Resolver, text: str, **kwargs) -> list[str]:
    return [m.canonical_id for m in r.extract(text, **kwargs)]


def test_fields_of_study_are_left_out_of_extraction_unless_asked_for(config) -> None:
    r = Resolver(_graph(_node("field.computer_science", "Computer science", "CS"), _node("skill.computer", "Computer"),
                        _node("field.physics", "Physics"), _node("skill.python", "Python")), config)
    text = "Studied computer science and physics with Python."
    assert _ids(r, text) == ["skill.python"]
    # The field consumes its words: "computer" alone does not surface instead.
    assert _ids(r, text, include_fields=True) == ["field.computer_science", "field.physics", "skill.python"]
    assert _ids(r, "Physics/Python", context="list") == ["skill.python"]  # a slash part too
    assert _ids(r, "Physics/Python", context="list", include_fields=True) == ["field.physics", "skill.python"]
    assert r.resolve("Computer science").canonical_id == "field.computer_science"  # resolve is unchanged


def test_blocked_phrases_consume_their_words_without_a_mention(config) -> None:
    graph = _graph(_node("skill.express", "Express.js", "Express"), _node("skill.c", "C"),
                   _node("skill.go", "Go"), _node("skill.express_entry_tool", "Express Entry Builder"))
    r = Resolver(graph, config, ambiguous={"express": "Express", "c": "C", "go": "Go"},
                 blocked_phrases=["American Express", "Express Entry", "Vitamin C", "Go-Live", "R&D"])
    assert _ids(r, "Analyst at American Express, Summer 2024") == []
    assert _ids(r, "Took Vitamin C daily.") == []
    assert _ids(r, "Led the Go Live of SAP.") == []  # hyphen variants of a blocked phrase are blocked too
    assert _ids(r, "Applied through Express Entry in 2023.") == []
    # The longest match wins: a longer skill name beats the blocked phrase inside it.
    assert _ids(r, "Used the Express Entry Builder daily.") == ["skill.express_entry_tool"]
    assert _ids(r, "Built the API with Express and C.") == ["skill.express", "skill.c"]
    assert _ids(r, "Express, American Express", context="list") == ["skill.express"]
    assert r.resolve("American Express").method == "NONE"
    assert r.resolve("R&D").method == "NONE"
    assert r.resolve("Express").canonical_id == "skill.express"


def test_a_blocked_phrase_wins_a_tie_with_a_key(config) -> None:
    r = Resolver(_graph(_node("skill.rnd", "Research and development", "R&D")), config, blocked_phrases=["R&D"])
    assert _ids(r, "Managed the R&D budget.") == []
    assert _ids(r, "Led research and development.") == ["skill.rnd"]


def test_automatic_cased_keys_keep_their_casing_in_lists(config) -> None:
    graph = _graph(_node("cat.tables", "Tables"), _node("skill.excel", "Excel"), _node("skill.pivot", "Pivot tables"))
    r = Resolver(graph, config, ambiguous={"excel": "Excel"}, cased={"tables": "Tables"})
    assert _ids(r, "Excel (pivot tables, VLOOKUP)", context="list") == ["skill.excel", "skill.pivot"]
    assert _ids(r, "excel, tables", context="list") == ["skill.excel"]  # curated: any casing; cased: not
    assert _ids(r, "Excel, Tables", context="list") == ["skill.excel", "cat.tables"]
    assert r.resolve("tables").method == "NONE"
    assert r.resolve("Tables").canonical_id == "cat.tables"
    # A key in both maps keeps the stricter rule.
    both = Resolver(graph, config, ambiguous={"tables": "Tables"}, cased={"tables": "Tables"})
    assert _ids(both, "tables", context="list") == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Dean's List, Fall 2023 and Spring 2024.", []),
        ("Software Developer Intern, Spring 2024 (Shopify)", []),
        ("Co-op terms: Fall and Spring", []),
        ("Available Summer, Fall, Spring", []),
        ("Completed a Spring '24 co-op.", []),
        ("Starting a Spring term at Waterloo.", []),
        ("Hired for a Spring co-op.", []),
        ("Fall & Spring semesters", []),
        ("Built REST APIs with Spring and Hibernate.", ["skill.spring", "skill.hibernate"]),
        ("Migrated to Spring Boot in 2024.", ["skill.spring_boot"]),
        ("Used Spring, 2 years.", ["skill.spring"]),  # the comma: not "Spring <year>"
    ],
)
def test_a_season_that_dates_something_is_never_a_skill(config, text: str, expected: list[str]) -> None:
    graph = _graph(_node("skill.spring", "Spring"), _node("skill.spring_boot", "Spring Boot"),
                   _node("skill.hibernate", "Hibernate"), _node("skill.summer", "Summer"))
    r = Resolver(graph, config, ambiguous={"spring": "Spring", "summer": "Summer"})
    assert _ids(r, text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("John R. Smith | Toronto, ON", []),
        ("Dr. Mary C. Jones", []),
        ("Mary C Jones, Analyst", []),
        ("Dr. R. Smith", []),
        ("J. R. R. Tolkien", []),
        ("Wrote firmware in C.", ["skill.c"]),
        ("Analysed data in R and Python.", ["skill.r", "skill.python"]),
        ("Wrote Python C Java daily", ["skill.python", "skill.c", "skill.java"]),  # neighbours are skills
        ("Programmed in C. Then moved to Java.", ["skill.c", "skill.java"]),  # "in" is no name
        ("Learned C.", ["skill.c"]),  # nothing follows: not between two names
    ],
)
def test_a_persons_initial_is_never_a_skill(config, text: str, expected: list[str]) -> None:
    graph = _graph(_node("skill.c", "C"), _node("skill.r", "R"), _node("skill.python", "Python"),
                   _node("skill.java", "Java"))
    r = Resolver(graph, config, ambiguous={"c": "C", "r": "R"})
    assert _ids(r, text) == expected


def test_a_leading_french_elision_is_read_past(config) -> None:
    graph = _graph(_node("skill.excel", "Excel"), _node("skill.aws", "AWS"), _node("skill.ml", "Machine learning",
                   "apprentissage automatique"), _node("skill.info", "Gestion de l'information"),
                   _node("skill.adapt", "Adapt to change", "s’adapter au changement"), _node("skill.dart", "Dart"))
    r = Resolver(graph, config, ambiguous={"excel": "Excel", "dart": "Dart"})
    text = "Maîtrise d'Excel et connaissance d’AWS; projets en l'apprentissage automatique."
    mentions = r.extract(text)
    assert _found(mentions) == [
        ("skill.excel", "Excel", "EXACT"),
        ("skill.aws", "AWS", "EXACT"),
        ("skill.ml", "apprentissage automatique", "ALIAS"),
    ]
    assert all(text[m.start:m.end] == m.surface for m in mentions)  # offsets point past the elision
    assert r.resolve("d'Excel").canonical_id == "skill.excel"
    assert r.resolve("L’apprentissage automatique").canonical_id == "skill.ml"
    assert r.resolve("qu'AWS").canonical_id == "skill.aws"
    assert r.resolve("d'excel").method == "NONE"  # the word after the elision keeps its casing rule
    # An elision inside a window stays, as labels have it; a label opening with one is reached either way.
    assert _ids(r, "Bonne gestion de l'information.") == ["skill.info"]
    assert _ids(r, "Capable de s'adapter au changement.") == ["skill.adapt"]
    assert r.resolve("adapter au changement").canonical_id == "skill.adapt"
    assert _ids(r, "Des objets d'art.") == []  # "d'art" is not read as Dart
    assert _ids(r, "O'Reilly books") == []  # not an elision


def test_a_dotted_abbreviations_period_does_not_end_a_window(config) -> None:
    # D-28: "P.Eng. candidate" must reach the in-progress node, never the licence "P.Eng".
    r = Resolver(_graph(_node("cred.peng", "Professional Engineer", "P.Eng"),
                        _node("cred.eit", "Engineer in Training", "P.Eng candidate")), config)
    assert _ids(r, "P.Eng. candidate with 4 years of experience.") == ["cred.eit"]
    assert _ids(r, "Licensed P.Eng. Leads the design team.") == ["cred.peng"]
    assert _ids(r, "Reports to the engineer. Candidate pool grew.") == []  # a plain word's period ends it


@pytest.mark.parametrize(
    "text,context,expected",
    [
        ("Experience with AI/ML frameworks such as PyTorch.", "prose", ["skill.ai", "skill.ml", "skill.pytorch"]),
        ("We are hiring an AI/ML Engineer Intern.", "prose", ["skill.ai", "skill.ml"]),
        ("AI/ML, NLP", "list", ["skill.ai", "skill.ml", "skill.nlp"]),
        ("ai/ml, nlp", "list", ["skill.ai", "skill.ml", "skill.nlp"]),  # lists waive the casing
        ("Built ML/AI pipelines on AWS.", "prose", ["skill.ml", "skill.ai", "skill.aws"]),
        ("Worked on BI/reporting dashboards.", "prose", ["skill.bi"]),
        ("Strong grasp of OS/networking fundamentals.", "prose", ["skill.os"]),
        ("Did ai/ml work.", "prose", []),  # prose keeps the casing
        ("AI/ML is our focus.", "prose", ["skill.ml"]),  # the first part keeps the sentence start
        ("Granted R/W access to shares.", "prose", []),  # single letters keep the companion rule
        ("Go/Excel", "prose", []),  # word-like keys too, and an acronym is no companion
        ("Shipped AI-powered search.", "prose", ["skill.ai"]),  # hyphens: still "<key>-<suffix>" only
        ("Paid bi-weekly.", "prose", []),
    ],
)
def test_all_capital_acronyms_need_no_slash_companion(config, text: str, context: str, expected: list[str]) -> None:
    graph = _graph(_node("skill.ai", "Artificial intelligence", "AI"), _node("skill.ml", "Machine learning", "ML"),
                   _node("skill.bi", "Business intelligence", "BI"), _node("skill.os", "Operating systems", "OS"),
                   _node("skill.r", "R"), _node("skill.go", "Go"), _node("skill.excel", "Excel"),
                   _node("skill.pytorch", "PyTorch"), _node("skill.nlp", "NLP"), _node("skill.aws", "AWS"))
    r = Resolver(graph, config, ambiguous={"ai": "AI", "ml": "ML", "bi": "BI", "os": "OS", "r": "R", "go": "Go",
                                           "excel": "Excel"})
    assert _ids(r, text, context=context) == expected


def test_a_key_both_a_field_and_a_skill_claim(config) -> None:
    # D-27: the skill in general text, the field in education lines; a field-only key still
    # consumes its words when fields are left out.
    graph = _graph(_node("skill.statistics", "Statistics", source="curated"),
                   _node("field.statistics", "Statistics", "statistical science", source="curated"),
                   _node("field.physics", "Physics", source="curated"), _node("skill.python", "Python"))
    r = Resolver(graph, config)
    assert _ids(r, "Applied statistics and physics with Python.") == ["skill.statistics", "skill.python"]
    assert _ids(r, "Applied statistics and physics with Python.", include_fields=True) == [
        "field.statistics", "field.physics", "skill.python"]
    assert _ids(r, "BSc in statistical science") == []  # field-only: consumed, no mention
    assert r.resolve("Statistics").canonical_id == "skill.statistics"  # resolve: the skill first
    assert r.resolve("Statistics", prefer_fields=True).canonical_id == "field.statistics"  # an education field
    assert r.resolve("statistical science").canonical_id == "field.statistics"
    assert r.resolve("Statistcs").canonical_id == "skill.statistics"  # fuzzy: the same order
    assert r.resolve("Statistcs", prefer_fields=True).canonical_id == "field.statistics"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Salary: 80,000 CAD per year", []),
        ("$25/hr CAD", []),
        ("Pay: CAD $80k", []),
        ("Compensation (CAD) is competitive.", []),
        ("Earned 52,000.00 USD", []),
        ("Proficient in CAD (SolidWorks)", ["skill.cad", "skill.solidworks"]),
        ("Designed over 10 CAD models.", ["skill.cad"]),  # a small count is no amount
        ("Built 3D CAD assemblies.", ["skill.cad"]),
        ("Used CAD/CAM programming daily.", ["skill.cad", "skill.cam"]),
        ("Drafting in CAD, $25/hr.", ["skill.cad"]),  # the rate is not next to the code
        ("Traded USD/CAD and EUR/USD currency pairs.", []),  # currency pairs
        ("Produced 1,200 CAD drawings for the plant.", ["skill.cad"]),  # a count, more words follow
        ("Updated the 2024 CAD standards manual.", ["skill.cad"]),  # a year is no amount
        ("Paid 25 CAD per hour.", []),  # a number, then a rate
        ("The budget was 80,000 CAD.", []),  # an amount, then the clause ends
        ("Salary: 80,000 CAD\nBenefits included", []),  # the line ends
        ("Paid 25.50 CAD.", []),  # cents
        ("Budget of 1.2M CAD.", []),  # a magnitude
        ("Managed a 2M CAD budget.", []),  # a money word follows
        ("Managed a CAD 2M budget.", []),  # an amount follows
        ("Experience with Fusion 360 CAD.", ["skill.cad"]),  # a version is no amount
        ("Experience with SolidWorks 2022 CAD.", ["skill.solidworks", "skill.cad"]),  # nor a year
        ("Proficient in CAD 2024.", ["skill.cad"]),  # nor a year after the code
        ("Tools: Office 365, CAD, SolidWorks", ["skill.cad", "skill.solidworks"]),  # a list comma
        ("Skills: Python 3, CAD.", ["skill.cad"]),
    ],
)
def test_a_currency_code_in_a_money_context_is_money(config, text: str, expected: list[str]) -> None:
    graph = _graph(_node("skill.cad", "CAD"), _node("skill.solidworks", "SolidWorks"), _node("skill.cam", "CAM"))
    r = Resolver(graph, config, ambiguous={"cad": "CAD"})
    assert _ids(r, text) == expected


def test_a_number_before_a_list_comma_is_no_amount(config) -> None:
    # D-27: in a skills list "365, CAD" is two items, whatever ends the clause after the code.
    graph = _graph(_node("skill.cad", "CAD"), _node("skill.solidworks", "SolidWorks"))
    r = Resolver(graph, config, ambiguous={"cad": "CAD"})
    for context in ("list", "prose"):
        assert _ids(r, "Microsoft Office 365, CAD, SolidWorks", context=context) == ["skill.cad", "skill.solidworks"]


def test_spring_of_a_year_is_a_date(config) -> None:
    r = Resolver(_graph(_node("skill.spring", "Spring")), config, ambiguous={"spring": "Spring"})
    assert _ids(r, "Graduated in the Spring of 2024.") == []
    assert _ids(r, "Built services with Spring of all kinds.") == ["skill.spring"]


def test_an_initialisms_period_joins_only_a_lower_case_word(config) -> None:
    r = Resolver(_graph(_node("cred.peng", "Professional Engineer", "P.Eng"),
                        _node("cred.eit", "Engineer in Training", "P.Eng candidate"),
                        _node("skill.dotnet", ".NET", "ASP.NET", "ASP.NET Core")), config)
    assert _ids(r, "P.Eng. candidate with four years of experience.") == ["cred.eit"]
    assert _ids(r, "Licensed P.Eng. Candidate pool screening is ongoing.") == ["cred.peng"]
    assert _found(r.extract("Built the portal in ASP.NET. Core features included search.")) == [
        ("skill.dotnet", "ASP.NET", "ALIAS")]


@pytest.mark.parametrize(
    "text,expected",
    [
        # Status is the context's business (D-28): the credential concept, whatever surrounds it.
        ("CPA (in progress)", [("cred.cpa", "CPA")]),
        ("RN (student) at Humber", [("cred.registered_nurse", "RN")]),
        ("309A Electrical Apprentice", [("cred.electrician", "309A"), ("cred.apprentice", "Electrical Apprentice")]),
        # A formal designation is read only as written, never across ":" or a dash.
        ("Applied as a CPA candidate in 2024.", [("cred.cpa_wip", "CPA candidate")]),
        ("Ideal candidate: CPA with five years.", [("cred.cpa", "CPA")]),
        ("Ideal Candidate - CPA, Toronto", [("cred.cpa", "CPA")]),
        ("CPA candidate - Toronto", [("cred.cpa_wip", "CPA candidate")]),
        ("Apprentice Electrician: 309A licence", [("cred.apprentice", "Apprentice Electrician"),
                                                   ("cred.electrician", "309A licence")]),
    ],
)
def test_credential_status_is_left_to_the_context(config, text: str, expected: list) -> None:
    graph = _graph(_node("cred.cpa", "Chartered Professional Accountant", "CPA"),
                   _node("cred.cpa_wip", "CPA candidate", "candidate CPA"),
                   _node("cred.registered_nurse", "Registered Nurse", "RN"),
                   _node("cred.electrician", "Electrician licence", "309A", "309A licence"),
                   _node("cred.apprentice", "Electrical apprentice", "Apprentice Electrician"))
    r = Resolver(graph, config, in_progress={"cred.cpa": "cred.cpa_wip", "cred.electrician": "cred.apprentice"})
    mentions = r.extract(text)
    assert [(m.canonical_id, m.surface) for m in mentions] == expected
    assert all(text[m.start:m.end] == m.surface for m in mentions)
    assert r.resolve("candidate CPA").canonical_id == "cred.cpa_wip"  # resolve reads the name as given


def test_new_settings_are_deterministic_across_input_order(config) -> None:
    graph = _graph(_node("skill.c", "C"), _node("skill.express", "Express.js", "Express"))
    a = Resolver(graph, config, ambiguous={"c": "C"}, cased={"express": "Express"},
                 blocked_phrases=["Vitamin C", "American Express"])
    b = Resolver(graph, config, ambiguous={"c": "C"}, cased={"express": "Express"},
                 blocked_phrases=["American Express", "Vitamin C"])
    text = "Vitamin C, American Express, Express and C."
    assert a.extract(text) == b.extract(text)
    assert _ids(a, text) == ["skill.express", "skill.c"]
