"""Annual bias-audit artifacts from persisted traces (SPEC section 9; docs/compliance.md).

    python scripts/bias_audit.py --traces traces.jsonl --demographics demographics.csv \\
        --out-dir reports/bias_audit/2026

Inputs
    traces.jsonl       one JSON object per line: {"opaque_id": "<24 hex>", "trace": {...},
                       "digest": "<16 hex>"} where `trace` is `matcher.audit.build_trace(result)`
                       and `digest` (optional) is `matcher.audit.trace_digest(trace)`. A line that
                       cannot be read is counted as malformed; a line whose digest does not match
                       its trace is counted as an integrity failure; neither enters the statistics.
    demographics.csv   a header with an `opaque_id` column plus one or more attribute columns, one
                       row per person. Keys must be opaque ids (24 lowercase hex characters, from
                       `matcher.audit.opaque_id`), never user ids. A blank cell means the person did
                       not disclose that attribute. Each attribute is audited on its own.

Outputs (in --out-dir): bias_audit.json and bias_audit.md. For every attribute: selection rates per
group at each band threshold with the four-fifths ratios (exact fractions), score distributions per
group, and abstention per group. Output is deterministic and holds no person-level identifier.
Groups smaller than --min-group-n are named with their size only. A run with a smaller --min-group-n
than the standard 100, with --thresholds other than the band minimums, or with --attributes naming
only some attributes is marked exploratory in both reports (a dry run, not an audit artifact): each
could hide a violation. A --min-group-n above 100 is refused, because it would leave out groups that
EVAL section 3 requires to be assessed.

Exit status: 0 every check that could be assessed passed; 1 unusable input or options; 2 at least
one four-fifths violation (terminal: stop and surface it, never tune around it); 3 nothing could be
assessed because no attribute has two groups of --min-group-n people.

The artifacts describe real people in aggregate: write them outside the repository (reports/ is
ignored by git) and never commit them (DECISIONS D-04).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # run as a script: make `matcher` importable without installing it
    sys.path.insert(0, str(ROOT / "src"))

from matcher.audit import four_fifths, trace_digest  # noqa: E402
from matcher.audit.fairness import FOUR_FIFTHS, MIN_GROUP_N, FairnessReport  # noqa: E402
from matcher.config import load_config  # noqa: E402
from matcher.models import Outcome  # noqa: E402

EXIT_PASS, EXIT_ERROR, EXIT_FAIL, EXIT_NOT_ASSESSED = 0, 1, 2, 3
REPORT_VERSION = 1

_OPAQUE_ID = re.compile(r"[0-9a-f]{24}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_OUTCOMES = frozenset(outcome.value for outcome in Outcome)
_ABSTAINED = frozenset({Outcome.NEEDS_REVIEW.value, Outcome.SCORED_LOW_CONFIDENCE.value})
_BUCKETS = [f"{low}-{low + 9}" for low in range(0, 90, 10)] + ["90-100"]
_ID_COLUMN_MIN_ROWS = 10  # below this a column of distinct labels can be a real (tiny) attribute


class AuditError(Exception):
    """Input the audit must not guess about; the message is shown to the operator."""


class UsageError(AuditError):
    pass


# --- reading the inputs ------------------------------------------------------------------------


class TraceRow(NamedTuple):
    """The few fields of a persisted trace the audit reads."""

    opaque_id: str
    outcome: str
    calibrated: Optional[int]
    reasons: tuple[str, ...]
    as_of: Optional[str]
    engine_version: Optional[str]
    config_hash: Optional[str]


class TraceLoad(NamedTuple):
    rows: list[TraceRow]
    lines: int
    malformed: int
    integrity_failures: int


def _text_or_none(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


def _row_from(record: object) -> tuple[TraceRow, bool]:
    """The row in `record` and whether its digest (when it has one) matches; ValueError if unreadable."""
    if not isinstance(record, dict):
        raise ValueError("not an object")
    opaque, trace = record.get("opaque_id"), record.get("trace")
    if not isinstance(opaque, str) or not _OPAQUE_ID.fullmatch(opaque) or not isinstance(trace, dict):
        raise ValueError("needs an opaque_id and a trace")
    outcome = trace.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        raise ValueError("unknown outcome")
    calibrated = trace.get("calibrated")
    if calibrated is not None and (
        isinstance(calibrated, bool) or not isinstance(calibrated, int) or not 0 <= calibrated <= 100
    ):
        raise ValueError("calibrated must be an integer from 0 to 100")
    reasons = trace.get("reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        raise ValueError("reasons must be a list of codes")
    intact = True
    if "digest" in record:
        if not isinstance(record["digest"], str):
            raise ValueError("digest must be text")
        intact = record["digest"] == trace_digest(trace)
    as_of = trace.get("as_of")
    row = TraceRow(
        opaque_id=opaque,
        outcome=outcome,
        calibrated=calibrated,
        reasons=tuple(reasons),
        as_of=as_of if isinstance(as_of, str) and _DATE.fullmatch(as_of) else None,
        engine_version=_text_or_none(trace.get("engine_version")),
        config_hash=_text_or_none(trace.get("config_hash")),
    )
    return row, intact


def load_traces(path: Path) -> TraceLoad:
    rows: list[TraceRow] = []
    lines = malformed = integrity_failures = 0
    with path.open(encoding="utf-8-sig", errors="replace") as handle:
        for raw in handle:
            text = raw.strip()
            if not text:
                continue
            lines += 1
            try:
                row, intact = _row_from(json.loads(text))
            except (ValueError, TypeError, RecursionError):  # JSONDecodeError is a ValueError
                malformed += 1
                continue
            if intact:
                rows.append(row)
            else:
                integrity_failures += 1
    return TraceLoad(rows, lines, malformed, integrity_failures)


class Demographics(NamedTuple):
    attributes: tuple[str, ...]
    people: dict[str, dict[str, str]]  # opaque id -> attribute -> group ("" = not disclosed)


def load_demographics(path: Path) -> Demographics:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = [cell.strip() for cell in next(reader, [])]
        if "opaque_id" not in header:
            raise AuditError(
                "the demographics file must be keyed by an `opaque_id` column (never user ids); "
                f"its columns are {header or 'missing'}"
            )
        if len(set(header)) != len(header) or "" in header:
            raise AuditError("the demographics header has an empty or repeated column name")
        attributes = tuple(name for name in header if name != "opaque_id")
        if not attributes:
            raise AuditError("the demographics file needs at least one attribute column besides opaque_id")
        key_at = header.index("opaque_id")
        people: dict[str, dict[str, str]] = {}
        for number, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != len(header):
                raise AuditError(f"line {number}: expected {len(header)} columns, found {len(row)}")
            key = row[key_at].strip()
            if not _OPAQUE_ID.fullmatch(key):
                raise AuditError(
                    f"line {number}: the key is not an opaque id (24 lowercase hex characters); "
                    "demographics are keyed by opaque id, never by user id"
                )
            values = {name: row[i].strip() for i, name in enumerate(header) if i != key_at}
            if people.setdefault(key, values) != values:
                raise AuditError(f"line {number}: an opaque id appears twice with different values")
    for name in attributes:
        labels = [person[name] for person in people.values() if person[name]]
        if len(labels) >= _ID_COLUMN_MIN_ROWS and len(set(labels)) == len(labels):
            raise AuditError(
                f"column {name!r} gives every person a different value: it looks like an identifier, "
                "not a demographic attribute"
            )
    return Demographics(attributes, people)


# --- the statistics ----------------------------------------------------------------------------


def _share(part: int, whole: int) -> float:
    return round(part / whole, 6)


def _group_details(members: list[TraceRow]) -> dict:
    withheld = [m for m in members if m.outcome in _ABSTAINED]
    scores = sorted(m.calibrated for m in members if m.calibrated is not None)
    histogram = dict.fromkeys(_BUCKETS, 0)
    for score in scores:
        histogram[_BUCKETS[min(score // 10, 9)]] += 1
    return {
        "people": len({m.opaque_id for m in members}),
        "traces": len(members),
        "outcomes": dict(sorted(Counter(m.outcome for m in members).items())),
        "abstention": {
            "count": len(withheld),
            "rate": _share(len(withheld), len(members)),
            "reasons": dict(sorted(Counter(r for m in withheld for r in m.reasons).items())),
        },
        "scores": {
            "n": len(scores),
            "mean": round(sum(scores) / len(scores), 2) if scores else None,
            "median": statistics.median(scores) if scores else None,
            "min": scores[0] if scores else None,
            "max": scores[-1] if scores else None,
            "histogram": histogram,
        },
    }


def _check(report: FairnessReport) -> dict:
    data = report.model_dump(mode="json")
    data["assessed"] = report.assessed
    if data["min_ratio"] is not None:
        data["min_ratio"] = round(data["min_ratio"], 6)
    for group in data["groups"]:
        group["rate"] = round(group["rate"], 6)
        group["ratio"] = round(group["ratio"], 6)
    return data


def _attribute_report(
    rows: list[TraceRow],
    demographics: Demographics,
    attribute: str,
    thresholds: Sequence[int],
    min_group_n: int,
) -> dict:
    grouped: dict[str, list[TraceRow]] = {}
    undisclosed = no_record = 0
    for row in rows:
        person = demographics.people.get(row.opaque_id)
        if person is None:
            no_record += 1
        elif not person[attribute]:
            undisclosed += 1
        else:
            grouped.setdefault(person[attribute], []).append(row)
    reports = {}
    for threshold in thresholds:
        records = [
            (row.opaque_id, label, row.calibrated is not None and row.calibrated >= threshold)
            for label, members in grouped.items()
            for row in members
        ]
        reports[threshold] = four_fifths(records, min_group_n=min_group_n)
    reference = reports[thresholds[0]]  # which groups are large enough does not depend on the threshold
    described = {group.group for group in reference.groups}
    return {
        "coverage": {
            "traces": len(rows),
            "with_group": sum(len(members) for members in grouped.values()),
            "undisclosed": undisclosed,
            "no_record": no_record,
        },
        "groups": {label: _group_details(grouped[label]) for label in sorted(described)},
        "suppressed": [{"group": g.group, "people": g.n_people} for g in reference.excluded],
        "selection": {str(threshold): _check(reports[threshold]) for threshold in thresholds},
    }


def _deviations(
    demographics: Demographics,
    attributes: Sequence[str],
    thresholds: Sequence[int],
    standard_thresholds: Sequence[int],
    min_group_n: int,
) -> list[str]:
    """How this run differs from the standard audit. Any difference makes it a dry run: an audit
    artifact must not be produced with a smaller group floor, other thresholds or fewer attributes,
    because each could hide a violation (a failure is terminal; nobody tunes around it)."""
    found = []
    if min_group_n < MIN_GROUP_N:
        found.append(f"the minimum group size is {min_group_n}, below the standard {MIN_GROUP_N}")
    if list(thresholds) != list(standard_thresholds):
        found.append(
            f"the thresholds are {', '.join(map(str, thresholds))}, not the band minimums "
            f"{', '.join(map(str, standard_thresholds))}"
        )
    if set(attributes) != set(demographics.attributes):
        found.append(f"only {len(set(attributes))} of {len(demographics.attributes)} attributes were audited")
    return found


def build_report(
    load: TraceLoad,
    demographics: Demographics,
    attributes: Sequence[str],
    thresholds: Sequence[int],
    min_group_n: int,
    standard_thresholds: Sequence[int],
) -> dict:
    rows = load.rows
    dated = sorted(row.as_of for row in rows if row.as_of)
    deviations = _deviations(demographics, attributes, thresholds, standard_thresholds, min_group_n)
    report_attributes = {
        name: _attribute_report(rows, demographics, name, thresholds, min_group_n)
        for name in sorted(attributes)
    }
    violations = []
    assessed = False
    for name, attribute in report_attributes.items():
        for threshold in thresholds:
            check = attribute["selection"][str(threshold)]
            assessed = assessed or check["assessed"]
            if not check["passed"]:
                violations.append(
                    {
                        "attribute": name,
                        "threshold": threshold,
                        "min_ratio": check["min_ratio"],
                        "min_ratio_exact": check["min_ratio_exact"],
                    }
                )
    return {
        "report_version": REPORT_VERSION,
        "status": "FAIL" if violations else ("PASS" if assessed else "NOT_ASSESSED"),
        "parameters": {
            "min_group_n": min_group_n,
            "standard_min_group_n": MIN_GROUP_N,
            "exploratory": bool(deviations),
            "deviations": deviations,
            "thresholds": list(thresholds),
            "ratio_threshold": f"{FOUR_FIFTHS.numerator}/{FOUR_FIFTHS.denominator}",
        },
        "inputs": {
            "traces": {
                "lines": load.lines,
                "usable": len(rows),
                "malformed": load.malformed,
                "integrity_failures": load.integrity_failures,
                "people": len({row.opaque_id for row in rows}),
                "as_of_first": dated[0] if dated else None,
                "as_of_last": dated[-1] if dated else None,
                "engine_versions": sorted({row.engine_version for row in rows if row.engine_version}),
                "config_hashes": sorted({row.config_hash for row in rows if row.config_hash}),
            },
            "demographics": {
                "people": len(demographics.people),
                "attributes": sorted(demographics.attributes),
            },
        },
        "attributes": report_attributes,
        "violations": violations,
    }


# --- the markdown ------------------------------------------------------------------------------


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|")


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return lines + [""]


def _people(count: int) -> str:
    return f"{count} person" if count == 1 else f"{count} people"


def _number(value: object) -> str:
    return "-" if value is None else str(value)


def render_markdown(report: dict) -> str:
    parameters, inputs = report["parameters"], report["inputs"]
    traces = inputs["traces"]
    low_n = parameters["min_group_n"]
    out = ["# Bias audit", "", f"Status: {report['status'].replace('_', ' ')}", ""]
    if parameters["exploratory"]:
        out += [
            f"> Exploratory run: {'; '.join(parameters['deviations'])}. A dry run, not an audit artifact.",
            "",
        ]
    out += ["## Violations", ""]
    if report["violations"]:
        out += [
            f"- `{v['attribute']}`, calibrated score >= {v['threshold']}: lowest ratio "
            f"{v['min_ratio_exact']} ({v['min_ratio']:.4f}), below {parameters['ratio_threshold']}."
            for v in report["violations"]
        ]
    elif report["status"] == "NOT_ASSESSED":
        out.append(
            f"None found, but nothing could be assessed: no attribute has two groups of at least "
            f"{low_n} people."
        )
    else:
        out.append("None.")
    out += [
        "",
        "## Inputs",
        "",
        f"- Traces: {traces['lines']} lines, {traces['usable']} usable "
        f"({traces['malformed']} malformed, {traces['integrity_failures']} failed their digest), "
        f"{traces['people']} people.",
        f"- Scored between {_number(traces['as_of_first'])} and {_number(traces['as_of_last'])}; "
        f"engine versions {', '.join(traces['engine_versions']) or '-'}; "
        f"config hashes {', '.join(traces['config_hashes']) or '-'}.",
        f"- Demographics: {inputs['demographics']['people']} people; attributes "
        f"{', '.join(inputs['demographics']['attributes'])}.",
        f"- Thresholds on the calibrated score: {', '.join(str(t) for t in parameters['thresholds'])}; "
        f"minimum group size {low_n} people.",
        "",
        "## Method",
        "",
        "- A trace is selected at threshold T when its calibrated score >= T. A trace with no "
        "calibrated score (needs review, gate failure) is never selected but stays in its group's "
        "denominator, so a group that is more often left unscored shows a lower rate.",
        "- A group's rate is selected traces divided by its traces: every scored pair of every person "
        "counts.",
        f"- A group is described and assessed only with at least {low_n} people (distinct opaque ids), "
        "not traces. Smaller groups are named with their size and nothing else.",
        f"- A group's ratio is its rate divided by the highest assessed rate, compared with "
        f"{parameters['ratio_threshold']} in exact fractions. A ratio of exactly "
        f"{parameters['ratio_threshold']} passes.",
        "- Withheld means no percentage was shown: needs review, or a low-confidence score. A gate "
        "failure is an ineligibility finding, not a withheld score.",
        "- Groups come from a separate, voluntary demographics file keyed by opaque id. People with no "
        "record, or who left a field blank, are counted as such and left out of that attribute.",
        "",
    ]
    for name, attribute in report["attributes"].items():
        out += _attribute_markdown(name, attribute, parameters)
    out += [
        "## Limits and review",
        "",
        "This report measures differences in outcomes between groups. It does not explain them, and it "
        "covers only the people who chose to share demographic data. A failed check is terminal: stop and "
        "surface it, and do not tune around it, move the threshold or drop the failing group. This is a "
        "starting checklist, not legal advice; an employment attorney should review the audit procedure "
        "before any employer-facing use (SPEC section 9, docs/compliance.md).",
        "",
    ]
    return "\n".join(out)


def _attribute_markdown(name: str, attribute: dict, parameters: dict) -> list[str]:
    coverage = attribute["coverage"]
    ratio_threshold = parameters["ratio_threshold"]
    out = [
        f"## {name}",
        "",
        f"{coverage['with_group']} of {coverage['traces']} traces belong to a person with a recorded group "
        f"({coverage['undisclosed']} undisclosed, {coverage['no_record']} with no demographic record).",
        "",
    ]
    groups = attribute["groups"]
    if attribute["suppressed"]:
        small = ", ".join(f"{s['group']} ({_people(s['people'])})" for s in attribute["suppressed"])
        out += [f"Groups below the minimum size, not described: {small}.", ""]
    if not groups:
        return out + ["No group is large enough to describe.", ""]
    out += ["### Groups", ""]
    out += _table(
        ["Group", "People", "Traces", "Withheld", "With score", "Mean", "Median"],
        [
            [
                label,
                g["people"],
                g["traces"],
                g["abstention"]["count"],
                g["scores"]["n"],
                _number(g["scores"]["mean"]),
                _number(g["scores"]["median"]),
            ]
            for label, g in groups.items()
        ],
    )
    for threshold, check in attribute["selection"].items():
        out += [f"### Selected at calibrated score >= {threshold}", ""]
        if not check["assessed"]:
            out += [f"Not assessed: fewer than two groups reach {check['min_group_n']} people.", ""]
            continue
        out += _table(
            ["Group", "People", "Traces", "Selected", "Rate", "Ratio", f"Meets {ratio_threshold}"],
            [
                [
                    g["group"],
                    g["n_people"],
                    g["n_records"],
                    g["selected"],
                    f"{g['rate_exact']} ({g['rate']:.4f})",
                    f"{g['ratio_exact']} ({g['ratio']:.4f})",
                    "yes" if g["meets"] else "NO",
                ]
                for g in check["groups"]
            ],
        )
        verdict = "passes" if check["passed"] else f"FAILS (below {ratio_threshold})"
        reference = check["reference_group"] or "none, nobody was selected"
        out += [
            f"Lowest ratio {check['min_ratio_exact']} ({check['min_ratio']:.4f}), which {verdict}. "
            f"Reference group: {reference}.",
            "",
        ]
    out += ["### Score distribution (calibrated)", ""]
    out += _table(
        ["Group", *_BUCKETS],
        [[label, *(g["scores"]["histogram"][bucket] for bucket in _BUCKETS)] for label, g in groups.items()],
    )
    out += ["### Abstention", ""]
    out += _table(
        ["Group", "Withheld", "Rate", "Reasons"],
        [
            [
                label,
                g["abstention"]["count"],
                f"{g['abstention']['rate']:.4f}",
                ", ".join(f"{code}: {n}" for code, n in g["abstention"]["reasons"].items()) or "-",
            ]
            for label, g in groups.items()
        ],
    )
    return out


# --- command line ------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would exit 2, which here means "violation found"
        raise UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="bias_audit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--traces", required=True, type=Path, help="persisted traces, JSON lines")
    parser.add_argument(
        "--demographics", required=True, type=Path, help="demographics CSV keyed by opaque_id"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=ROOT / "reports" / "bias_audit", help="where to write"
    )
    parser.add_argument(
        "--min-group-n",
        type=int,
        default=MIN_GROUP_N,
        help=f"people per group, at most {MIN_GROUP_N} (default {MIN_GROUP_N})",
    )
    parser.add_argument(
        "--thresholds", help="calibrated-score thresholds, e.g. 85,70,55 (default: band minimums)"
    )
    parser.add_argument("--attributes", help="attribute columns to audit, comma separated (default: all)")
    return parser


def _thresholds(text: Optional[str]) -> list[int]:
    if text is None:
        return [rule.min for rule in load_config().bands.bands if rule.min > 0]
    try:
        values = {int(part) for part in text.split(",")}
    except ValueError:
        raise UsageError(f"--thresholds must be whole numbers separated by commas, got {text!r}") from None
    if not values or any(not 1 <= value <= 100 for value in values):
        raise UsageError("--thresholds must be between 1 and 100")
    return sorted(values, reverse=True)


def _summary(report: dict, out_dir: Path) -> str:
    lines = [f"bias audit: {report['status'].replace('_', ' ')}"]
    lines += [
        f"  {v['attribute']}, calibrated score >= {v['threshold']}: lowest ratio {v['min_ratio_exact']}"
        for v in report["violations"]
    ]
    lines += [f"wrote {out_dir / 'bias_audit.json'}", f"wrote {out_dir / 'bias_audit.md'}"]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    if args.min_group_n < 1:
        raise UsageError("--min-group-n must be at least 1")
    if args.min_group_n > MIN_GROUP_N:
        raise UsageError(
            f"--min-group-n cannot exceed {MIN_GROUP_N}: a higher floor would leave out groups that "
            "EVAL section 3 requires to be assessed"
        )
    standard_thresholds = _thresholds(None)
    thresholds = _thresholds(args.thresholds)
    demographics = load_demographics(args.demographics)
    attributes = (
        [a.strip() for a in args.attributes.split(",")] if args.attributes else list(demographics.attributes)
    )
    unknown = [a for a in attributes if a not in demographics.attributes]
    if unknown:
        raise UsageError(
            f"unknown attribute(s) {unknown}; the demographics file has {sorted(demographics.attributes)}"
        )
    load = load_traces(args.traces)
    if not load.rows:
        raise AuditError(
            f"no usable traces in {args.traces} ({load.lines} lines, {load.malformed} malformed, "
            f"{load.integrity_failures} failed their digest)"
        )
    report = build_report(load, demographics, attributes, thresholds, args.min_group_n, standard_thresholds)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "bias_audit.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (args.out_dir / "bias_audit.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
    print(_summary(report, args.out_dir))
    return {"FAIL": EXIT_FAIL, "NOT_ASSESSED": EXIT_NOT_ASSESSED}.get(report["status"], EXIT_PASS)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(_parser().parse_args(argv))
    except AuditError as exc:
        print(f"bias_audit: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"bias_audit: cannot read or write {exc.filename or 'a file'}: {exc.strerror}", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
