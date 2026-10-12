"""Shared set-up for the posting parser tests (Task 6.5): the committed taxonomy, the parse config
and a `parse` helper. Postings in the tests are invented; none is scraped text (DECISIONS D-04)."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Any

from matcher.config import load_config
from matcher.graph.loader import load_graph, load_resolver
from matcher.models import JobPosting, JobRequirement, Location
from matcher.parse.config import load_parse_config
from matcher.parse.posting import PostingMeta, parse_posting
from matcher.parse.taxonomy_extras import load_title_index

TAXONOMY = Path(__file__).resolve().parents[1] / "data" / "taxonomy"
AS_OF = dt.date(2026, 10, 1)
TORONTO = Location(country="CA", region="ON", city="Toronto")
SEATTLE = Location(country="US", region="WA", city="Seattle")


@lru_cache(maxsize=1)
def env() -> dict[str, Any]:
    config = load_config()
    parse_config = load_parse_config()
    return {
        "config": config,
        "graph": load_graph(TAXONOMY),
        "resolver": load_resolver(TAXONOMY, config),
        "parse_config": parse_config,
        "titles": load_title_index(TAXONOMY, parse_config=parse_config),
    }


def parse(description: str, title: str = "Software Developer", *, as_of: dt.date = AS_OF, **meta: Any) -> JobPosting:
    e = env()
    meta.setdefault("locations", (TORONTO,))
    return parse_posting(title, description, meta=PostingMeta(**meta), as_of=as_of, resolver=e["resolver"],
                         graph=e["graph"], config=e["config"], parse_config=e["parse_config"], titles=e["titles"])


def reqs(posting: JobPosting) -> dict[str, JobRequirement]:
    return {r.skill_id: r for r in posting.requirements}


def necessities(posting: JobPosting) -> dict[str, str]:
    return {r.skill_id: r.necessity.value for r in posting.requirements}
