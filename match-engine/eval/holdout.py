"""Layer 4, the holdout (EVAL.md sections 0 and 5.3): AES-GCM labels, the CI key, the look ledger.

    python -m eval.holdout evaluate [--ref REF] [--purpose TEXT] [--proposal DIR]   (`make eval-holdout`)
    python -m eval.holdout encrypt --in labels.jsonl --out labels.enc               (the label owner)
    python -m eval.holdout keygen                                                   (once, into CI secrets)

Format and procedure: `eval/HOLDOUT.md`. In short:

* `eval/corpora/holdout/pairs/pairs.jsonl` holds the inputs (visible); `labels.enc` holds the
  outcome labels, AES-256-GCM: `MAGIC (4 bytes) | nonce (12) | ciphertext+tag`, the magic as
  associated data. The key is 32 bytes, base64 (standard or URL-safe) in env `MATCH_HOLDOUT_KEY`,
  which only CI holds.
* Without the key `evaluate` prints `HOLDOUT: skipped (no key)` and exits 0, reading nothing.
* With the key it refuses any ref that is not a release branch (`release/...`), then checks the
  look budget: 10 decrypting runs per calendar quarter, logged in
  `docs/evaluation_budget.ledger.csv` (date, ref, purpose). The row is appended before decrypting;
  an 11th look in the quarter is refused before anything is decrypted.
* It prints aggregate metrics only (NDCG@10, AUC, abstention, four-fifths), never a per-pair label.
  With `--proposal` it also scores the proposal's weights and applies EVAL 5.3 rule 4
  (dev gain > 2 x holdout gain -> OVERFIT_SUSPECTED).

Exit codes: 0 done or skipped, 1 fairness guardrail FAIL, 2 refused (not a release ref, bad key or
file), 3 look budget exhausted.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import io
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]  # match-engine/
if str(ROOT / "src") not in sys.path:  # `python -m eval.holdout` from match-engine/ without an install
    sys.path.insert(0, str(ROOT / "src"))

KEY_ENV = "MATCH_HOLDOUT_KEY"
HOLDOUT_DIR = ROOT / "eval" / "corpora" / "holdout"
LABELS = HOLDOUT_DIR / "labels.enc"
PAIRS = HOLDOUT_DIR / "pairs" / "pairs.jsonl"
LEDGER = ROOT / "docs" / "evaluation_budget.ledger.csv"
LOOKS_PER_QUARTER = 10
RELEASE_PREFIX = "release/"
MAGIC = b"MHO1"
_NONCE = 12
_FIELDS = ("date", "ref", "purpose")


class HoldoutError(RuntimeError):
    """A bad key, a corrupt or tampered file, or a refused look."""


class BudgetExhausted(HoldoutError):
    """The quarter's 10 looks are spent (EVAL 5.3 rule 3)."""


# ─── key and encryption ────────────────────────────────────────────────────────────────────


def generate_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def find_key(environ: Optional[Mapping[str, str]] = None) -> Optional[bytes]:
    """The 32-byte key from `MATCH_HOLDOUT_KEY`, or None when it is unset or blank. A value that is
    set but is not a 32-byte base64 key raises (never a silent skip)."""
    env = os.environ if environ is None else environ
    text = (env.get(KEY_ENV) or "").strip()
    if not text:
        return None
    padded = text + "=" * (-len(text) % 4)
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            key = decode(padded.encode("ascii"))
        except (binascii.Error, ValueError, UnicodeEncodeError):
            continue
        if len(key) == 32:
            return key
    raise HoldoutError(f"{KEY_ENV} is set but is not a base64 32-byte key")


def _aead(key: bytes):  # noqa: ANN202
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(key) != 32:
        raise HoldoutError("the holdout key must be 32 bytes")
    return AESGCM(key)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE)
    return MAGIC + nonce + _aead(key).encrypt(nonce, plaintext, MAGIC)


def decrypt(blob: bytes, key: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag

    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + _NONCE + 16:
        raise HoldoutError("not a holdout labels file")
    nonce = blob[len(MAGIC):len(MAGIC) + _NONCE]
    try:
        return _aead(key).decrypt(nonce, blob[len(MAGIC) + _NONCE:], MAGIC)
    except InvalidTag as exc:
        raise HoldoutError("decryption failed: wrong key or tampered file") from exc


# ─── the look ledger ───────────────────────────────────────────────────────────────────────


def quarter(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


class Ledger:
    """`docs/evaluation_budget.ledger.csv`: one row per decrypting run."""

    def __init__(self, path: Path = LEDGER) -> None:
        self.path = Path(path)

    def rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def looks_in_quarter(self, day: date) -> int:
        q = quarter(day)
        return sum(1 for r in self.rows() if r.get("date") and quarter(date.fromisoformat(r["date"])) == q)

    def record(self, day: date, ref: str, purpose: str) -> None:
        for value in (ref, purpose):
            if "\n" in value or "\r" in value:
                raise ValueError("ledger fields are single-line")
        new = not self.path.exists() or self.path.stat().st_size == 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        if new:
            writer.writerow(_FIELDS)
        writer.writerow((day.isoformat(), ref, purpose))
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(buffer.getvalue())


def open_labels(key: bytes, *, ref: str, purpose: str, labels_path: Path = LABELS, ledger_path: Path = LEDGER,
                today: Optional[date] = None) -> bytes:
    """Decrypt the labels: one look. Refuses past the quarter's budget, records the look first."""
    today = today or date.today()
    ledger = Ledger(ledger_path)
    used = ledger.looks_in_quarter(today)
    if used >= LOOKS_PER_QUARTER:
        year, q = quarter(today)
        raise BudgetExhausted(f"holdout look budget exhausted: {used} of {LOOKS_PER_QUARTER} looks used in "
                              f"{year} Q{q} (EVAL 5.3); nothing was decrypted")
    blob = Path(labels_path).read_bytes()
    ledger.record(today, ref, purpose)
    return decrypt(blob, key)


# ─── evaluation ────────────────────────────────────────────────────────────────────────────


def _current_ref(environ: Mapping[str, str]) -> str:
    for name in ("GITHUB_REF_NAME", "GITHUB_HEAD_REF"):
        if environ.get(name):
            return environ[name]
    try:
        return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def evaluate(labels_text: str, pairs_path: Path, *, proposal: Optional[Path] = None) -> int:
    """Score the holdout pairs and print aggregate metrics only. Returns the exit code."""
    from eval.metrics import auc_roc, bootstrap_ci, ndcg_at_k
    from eval.metrics import abstention_stats
    from eval.suites import fairness_guardrail
    from eval.tune import proposal as proposals
    from eval.tune.search import EngineEvaluator
    from matcher.models import DimensionName, JobPosting, Resume

    labels = {row["pair_id"]: row for row in _jsonl(labels_text)}
    items = []
    for row in _jsonl(Path(pairs_path).read_text(encoding="utf-8")):
        label = labels.get(row["pair_id"])
        if label is None:
            continue
        outcome = str(label.get("outcome") or "").lower()
        advanced = bool(label.get("advanced")) or outcome in ("advanced", "hired")
        items.append((row["posting_id"], row["pair_id"], Resume.model_validate(row["resume"]),
                      JobPosting.model_validate(row["posting"]), date.fromisoformat(row["as_of"]),
                      3 if outcome == "hired" else 2 if advanced else 0, advanced, label.get("group")))
    evaluator = EngineEvaluator("holdout", items, synthetic_only=False)
    current = {d.value: evaluator.engine.config.weights.of(d) for d in DimensionName}
    pairs = evaluator.pairs(current, checked=True)
    ndcg = bootstrap_ci(pairs, lambda ps: ndcg_at_k(ps, 10), n_resamples=1000, seed=0)
    fairness = fairness_guardrail(pairs)
    print(f"HOLDOUT: {len(pairs)} labelled pairs over {len({p.posting_id for p in pairs})} postings")
    print(f"HOLDOUT: NDCG@10 {ndcg.point} [{ndcg.low}, {ndcg.high}]; AUC {auc_roc(pairs)}; "
          f"abstention {abstention_stats(pairs)['rate']}")
    print(f"HOLDOUT: four-fifths {fairness.passed} ({fairness.value}, {fairness.detail})")
    code = 1 if fairness.passed is False else 0
    if proposal is not None:
        meta = json.loads((Path(proposal) / "proposal.json").read_text(encoding="utf-8"))
        proposed_pairs = evaluator.pairs(meta["proposed"], checked=True)
        before, after = ndcg_at_k(pairs, 10), ndcg_at_k(proposed_pairs, 10)
        gain = None if before is None or after is None else after - before
        decision = proposals.accept_rules(meta["dev_gain"], holdout_gain=gain,
                                          synthetic_only=bool(meta["synthetic_only"]),
                                          fairness_assessed=bool(meta["fairness_assessed"]))
        print("HOLDOUT: proposal " + str(proposal))
        for line in decision.lines():
            print(f"HOLDOUT: {line}")
        if fairness_guardrail(proposed_pairs).passed is False:
            print("HOLDOUT: FAIL: the proposal fails the four-fifths guardrail (terminal)")
            code = 1
    return code


# ─── CLI ───────────────────────────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eval.holdout", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ev = sub.add_parser("evaluate", help="layer 4 (CI, release branches only)")
    ev.add_argument("--labels", default=str(LABELS))
    ev.add_argument("--pairs", default=str(PAIRS))
    ev.add_argument("--ledger", default=str(LEDGER))
    ev.add_argument("--ref", default=None, help="the git ref being evaluated (default: from CI or git)")
    ev.add_argument("--purpose", default="release candidate evaluation")
    ev.add_argument("--proposal", default=None, help="proposals/<date>-tune folder to check (EVAL 5.3 rule 4)")
    enc = sub.add_parser("encrypt", help="encrypt a labels JSONL with the key in MATCH_HOLDOUT_KEY")
    enc.add_argument("--in", dest="source", required=True)
    enc.add_argument("--out", required=True)
    sub.add_parser("keygen", help="print a fresh base64 key (store it only in CI secrets)")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, environ: Optional[Mapping[str, str]] = None,
         today: Optional[date] = None) -> int:
    args = _parser().parse_args(argv)
    env = os.environ if environ is None else environ
    if args.command == "keygen":
        print(generate_key())
        return 0
    try:
        key = find_key(env)
    except HoldoutError as exc:
        print(f"HOLDOUT: refused: {exc}")
        return 2
    if args.command == "encrypt":
        if key is None:
            print(f"HOLDOUT: refused: {KEY_ENV} is not set")
            return 2
        Path(args.out).write_bytes(encrypt(Path(args.source).read_bytes(), key))
        print(f"HOLDOUT: wrote {args.out}")
        return 0
    if key is None:
        print("HOLDOUT: skipped (no key)")
        return 0
    ref = args.ref or _current_ref(env)
    if not ref.startswith(RELEASE_PREFIX):
        print(f"HOLDOUT: refused: the holdout decrypts only on release branches ({RELEASE_PREFIX}...), not {ref!r}")
        return 2
    try:
        labels_text = open_labels(key, ref=ref, purpose=args.purpose, labels_path=Path(args.labels),
                                  ledger_path=Path(args.ledger), today=today).decode("utf-8")
    except BudgetExhausted as exc:
        print(f"HOLDOUT: refused: {exc}")
        return 3
    except (HoldoutError, OSError) as exc:
        print(f"HOLDOUT: refused: {exc}")
        return 2
    print(f"HOLDOUT: look recorded in {args.ledger} ({today or date.today()}, {ref}, {args.purpose})")
    return evaluate(labels_text, Path(args.pairs), proposal=Path(args.proposal) if args.proposal else None)


if __name__ == "__main__":
    raise SystemExit(main())
