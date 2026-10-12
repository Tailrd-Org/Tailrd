"""Normalised IDF and the common words from Tailrd's postings (Task 2.3; SPEC §3.1; DECISIONS D-21).

Run from match-engine/ after scripts/build_taxonomy.py and scripts/pull_corpus.py:

    .venv/Scripts/python.exe scripts/compute_idf.py [--corpus FILE] [--taxonomy DIR] [--min-word-df N]

Reads every posting of the corpus (corpus_stats.py: title + description decoded from HTML, through
`Resolver.extract`), counts each skill once per posting (df) and writes, through `write_artifacts`
(nodes and edges unchanged, every other artifact kept as it was):

    idf.json            normalize_idf(df, N) over the N postings read (D-21); n_postings = N
    common_words.json   every normalised word used in at least N postings (default 20) that does not
                        resolve exactly, most frequent first: `resolve` never fuzzy-matches them
    MANIFEST.json       the "corpus" source line (file hash, size, dates)
    SOURCES.md          the "Corpus statistics" section

Then run scripts/compute_pmi.py on the same corpus.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import corpus_stats  # noqa: E402
from matcher.config import load_config  # noqa: E402
from matcher.graph.idf import normalize_idf  # noqa: E402
from matcher.graph.loader import load_resolver  # noqa: E402


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=corpus_stats.DEFAULT_CORPUS)
    parser.add_argument("--taxonomy", type=Path, default=corpus_stats.DEFAULT_TAXONOMY)
    parser.add_argument("--min-word-df", type=int, default=corpus_stats.COMMON_WORD_MIN_DF,
                        help="postings a word needs to be a common word (default %(default)s)")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    config = load_config()
    resolver = load_resolver(args.taxonomy, config)
    postings = corpus_stats.read_postings(args.corpus)
    counts = corpus_stats.count_corpus(resolver, postings)
    if counts.n == 0:
        raise SystemExit(f"compute_idf: {args.corpus} holds no postings")
    idf = normalize_idf(counts.df, counts.n)
    words = corpus_stats.common_words(resolver, counts.word_df, min_df=args.min_word_df)
    summary = corpus_stats.summarize(args.corpus, postings)
    corpus_stats.rewrite_taxonomy(
        args.taxonomy, idf=idf, n_postings=counts.n,
        extra={corpus_stats.COMMON_WORDS_JSON: {"format": 1, "words": words}},
        sources={"corpus": corpus_stats.source_line(summary)},
    )
    corpus_stats.write_sources_section(args.taxonomy, summary, counts, config.scoring.taxonomy)
    print(f"idf for {len(idf):,} ids over {counts.n:,} postings ({counts.skill_postings:,} name a skill); "
          f"{len(words):,} common words; {time.perf_counter() - started:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
