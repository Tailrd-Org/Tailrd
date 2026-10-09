"""Builds tests/fixtures/graph_small/: a ~40-node skill graph for the engine tests (Task 2.1).

The committed JSON files are this module's output, written through `write_artifacts` so no hash
is ever typed by hand; `tests/test_graph_loader.py::test_committed_fixture_is_current` fails when
they drift from this source. Regenerate from match-engine/ with:

    ./.venv/Scripts/python.exe tests/fixtures/build_graph_small.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":  # run as a script: make `matcher` importable without installing it
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from matcher.graph.idf import normalize_idf  # noqa: E402
from matcher.graph.loader import write_artifacts  # noqa: E402
from matcher.graph.model import SkillNode  # noqa: E402
from matcher.models import SkillKind  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "graph_small"
N_POSTINGS = 1000
SOURCES = {"fixture": "hand-built synthetic graph (tests/fixtures/build_graph_small.py)"}

_TECH = SkillKind.TECHNOLOGY
_METHOD = SkillKind.METHODOLOGY
_DOMAIN = SkillKind.DOMAIN
_TOOL = SkillKind.TOOL
_SOFT = SkillKind.SOFT
_CRED = SkillKind.CREDENTIAL


def _n(
    node_id: str,
    label: str,
    kind: SkillKind,
    *parents: str,
    aliases: tuple[str, ...] = (),
    source: str = "esco",
    release_year: int | None = None,
    provisional: bool = False,
) -> SkillNode:
    return SkillNode(
        id=node_id,
        label=label,
        kind=kind,
        parents=parents,
        aliases=aliases,
        source=source,
        release_year=release_year,
        provisional=provisional,
    )


NODES: tuple[SkillNode, ...] = (
    # JavaScript family: javascript <- js_framework <- {react, vue, angular, svelte}; react <- {nextjs, react_native}
    _n("skill.javascript", "JavaScript", _TECH, aliases=("JS", "ECMAScript"), release_year=1995),
    _n("skill.js_framework", "JavaScript framework", _TECH, "skill.javascript", source="curated"),
    _n("skill.react", "React", _TECH, "skill.js_framework", aliases=("ReactJS", "React.js"), release_year=2013),
    _n("skill.vue", "Vue.js", _TECH, "skill.js_framework", aliases=("Vue", "VueJS"), release_year=2014),
    _n("skill.angular", "Angular", _TECH, "skill.js_framework", aliases=("AngularJS",), release_year=2010),
    _n("skill.svelte", "Svelte", _TECH, "skill.js_framework", source="provisional", provisional=True,
       release_year=2016),
    _n("skill.nextjs", "Next.js", _TECH, "skill.react", aliases=("NextJS",), release_year=2016),
    _n("skill.react_native", "React Native", _TECH, "skill.react", aliases=("React-Native",), release_year=2015),
    _n("skill.redux", "Redux", _TECH, "skill.javascript", release_year=2015),
    _n("skill.typescript", "TypeScript", _TECH, "skill.javascript", aliases=("TS",), release_year=2012),
    _n("skill.nodejs", "Node.js", _TECH, "skill.javascript", aliases=("NodeJS", "Node"), release_year=2009),
    # Python family
    _n("skill.python", "Python", _TECH, aliases=("Python 3", "Python3"), release_year=1991),
    _n("skill.django", "Django", _TECH, "skill.python", release_year=2005),
    _n("skill.flask", "Flask", _TECH, "skill.python", release_year=2010),
    _n("skill.pandas", "pandas", _TECH, "skill.python", release_year=2008),
    _n("skill.scikit_learn", "scikit-learn", _TECH, "skill.python", aliases=("sklearn",), release_year=2007),
    # SQL family
    _n("skill.sql", "SQL", _TECH),
    _n("skill.postgresql", "PostgreSQL", _TECH, "skill.sql", aliases=("Postgres",)),
    _n("skill.mysql", "MySQL", _TECH, "skill.sql"),
    # Cloud and infrastructure
    _n("skill.aws", "Amazon Web Services", _TECH, aliases=("AWS",), release_year=2006),
    _n("skill.kubernetes", "Kubernetes", _TOOL, aliases=("k8s",), release_year=2014),
    _n("skill.docker", "Docker", _TOOL, source="onet", release_year=2013),
    _n("skill.terraform", "Terraform", _TOOL, source="onet", release_year=2014),
    # Languages with short or ambiguous names
    _n("skill.go", "Go", _TECH, aliases=("Golang",), release_year=2009),
    _n("skill.r", "R", _TECH, aliases=("R programming",)),
    _n("skill.cpp", "C++", _TECH),
    _n("skill.csharp", "C#", _TECH, aliases=("C Sharp",)),
    _n("skill.dotnet", ".NET", _TECH, aliases=("dotnet",)),
    # Practices, tools, domains
    _n("skill.ci_cd", "CI/CD", _METHOD),
    _n("skill.agile", "Agile", _METHOD),
    _n("skill.scrum", "Scrum", _METHOD, "skill.agile"),
    _n("skill.git", "Git", _TOOL, source="onet"),
    _n("skill.excel", "Microsoft Excel", _TOOL, aliases=("Excel", "MS Excel"), source="onet"),
    _n("skill.machine_learning", "Machine learning", _DOMAIN, aliases=("ML",)),
    _n("skill.frontend", "Frontend development", _DOMAIN, aliases=("Front-end",), source="curated"),
    # Soft skills
    _n("skill.communication", "Communication", _SOFT, aliases=("Communication skills",)),
    _n("skill.teamwork", "Teamwork", _SOFT, aliases=("Collaboration",)),
    _n("skill.leadership", "Leadership", _SOFT),
    # Credentials (DECISIONS D-13)
    _n("cred.aws_saa", "AWS Certified Solutions Architect \u2013 Associate", _CRED,
       aliases=("AWS Solutions Architect Associate", "AWS SAA"), source="curated"),
    _n("cred.registered_nurse", "Registered Nurse", _CRED, aliases=("RN",), source="curated"),
    # A parent cycle (bad source data the traversal must survive): a -> b -> c -> a
    _n("skill.cycle_a", "Cycle A", _DOMAIN, "skill.cycle_b"),
    _n("skill.cycle_b", "Cycle B", _DOMAIN, "skill.cycle_c"),
    _n("skill.cycle_c", "Cycle C", _DOMAIN, "skill.cycle_a"),
)


def _symmetric(*pairs: tuple[str, str, float]) -> dict[str, dict[str, float]]:
    edges: dict[str, dict[str, float]] = {}
    for a, b, weight in pairs:
        edges.setdefault(a, {})[b] = weight
        edges.setdefault(b, {})[a] = weight
    return edges


# Related edges, stored in both directions like `build_edges` output.
EDGES: dict[str, dict[str, float]] = _symmetric(
    ("skill.react", "skill.vue", 0.65),  # the hand-curated one (SPEC §3 example)
    ("skill.react", "skill.nextjs", 0.80),
    ("skill.kubernetes", "skill.docker", 0.70),
    ("skill.terraform", "skill.aws", 0.55),
)

# Synthetic document frequencies over N_POSTINGS postings; every other node is "unseen".
DF: dict[str, int] = {
    "skill.communication": 400,
    "skill.teamwork": 350,
    "skill.javascript": 300,
    "skill.python": 260,
    "skill.sql": 240,
    "skill.react": 220,
    "skill.leadership": 200,
    "skill.excel": 180,
    "skill.git": 160,
    "skill.aws": 150,
    "skill.typescript": 140,
    "skill.agile": 130,
    "skill.docker": 120,
    "skill.nodejs": 110,
    "skill.scrum": 90,
    "skill.machine_learning": 75,
    "skill.postgresql": 70,
    "skill.kubernetes": 60,
    "skill.mysql": 55,
    "skill.angular": 50,
    "skill.vue": 45,
    "skill.pandas": 40,
    "skill.django": 35,
    "skill.terraform": 30,
    "skill.go": 28,
    "skill.nextjs": 25,
    "skill.flask": 20,
    "skill.r": 15,
    "cred.aws_saa": 8,
    "cred.registered_nurse": 5,
}


def build(target: Path = FIXTURE_DIR) -> None:
    """Write the fixture artifacts (uncompressed nodes.json, so diffs stay readable)."""
    write_artifacts(
        target,
        NODES,
        normalize_idf(DF, N_POSTINGS),
        EDGES,
        n_postings=N_POSTINGS,
        sources=SOURCES,
        compress=False,
    )


if __name__ == "__main__":
    build()
