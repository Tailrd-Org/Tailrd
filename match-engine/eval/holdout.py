"""Layer 4, the holdout (EVAL.md sections 0 and 5.3): AES-GCM labels, the CI key, the look ledger.

    python -m eval.holdout evaluate [--ref REF] [--sha SHA] [--purpose TEXT] [--proposal DIR]
                                                                                    (`make eval-holdout`)
    python -m eval.holdout encrypt --in labels.jsonl --out labels.enc               (the label owner)
    python -m eval.holdout keygen                                                   (once, into CI secrets)

Format and procedure: `eval/HOLDOUT.md`. In short:

* `eval/corpora/holdout/pairs/pairs.jsonl` holds the inputs; `labels.enc` holds the outcome labels,
  AES-256-GCM: `MAGIC (4 bytes) | nonce (12) | ciphertext+tag`, the magic as associated data. The
  key is 32 bytes, base64 (standard or URL-safe, nothing else) in env `MATCH_HOLDOUT_KEY`, an
  environment secret only the `holdout` CI job holds.
* Without the key `evaluate` prints `HOLDOUT: skipped (no key)` and exits 0, reading nothing.
* With the key it refuses any ref that is not a release branch (`release/...`), then claims a look
  in the durable ledger: the `holdout-ledger` branch on the remote, which every release branch
  shares. The claim reads that branch fresh, refuses the 11th look of the calendar quarter (UTC)
  and a second look at the same commit SHA, and pushes the new `date,ref,sha,purpose` row (a
  fast-forward push, so a concurrent claim is rejected). Only after the push succeeds are the labels
  decrypted. If the remote cannot be read or the push fails, nothing is decrypted.
* It prints aggregate metrics only (NDCG@10, AUC, abstention, four-fifths), never a per-pair value
  and never the text of a malformed input (only its index and the exception type). With
  `--proposal` it also scores the proposal's weights and applies EVAL 5.3 rule 4.

`--local-ledger PATH` uses a CSV instead of the branch (tests and dry runs); it is refused inside
GitHub Actions. Nothing here writes under `config/` or `eval/corpora/holdout/`.

Exit codes: 0 done or skipped, 1 fairness guardrail FAIL, 2 refused (not a release ref, bad key,
unreadable ledger, failed push, missing or malformed file), 3 the quarter's budget is spent or this
commit was already evaluated.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import io
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]  # match-engine/
if str(ROOT / "src") not in sys.path:  # `python -m eval.holdout` from match-engine/ without an install
    sys.path.insert(0, str(ROOT / "src"))

KEY_ENV = "MATCH_HOLDOUT_KEY"
CONFIG_DIR = ROOT / "config"
HOLDOUT_DIR = ROOT / "eval" / "corpora" / "holdout"
LABELS = HOLDOUT_DIR / "labels.enc"
PAIRS = HOLDOUT_DIR / "pairs" / "pairs.jsonl"
LEDGER = ROOT / "docs" / "evaluation_budget.ledger.csv"  # format reference; the durable ledger is the branch
LEDGER_BRANCH = "holdout-ledger"
LEDGER_FILE = "evaluation_budget.ledger.csv"  # its path on the ledger branch
LOOKS_PER_QUARTER = 10
RELEASE_PREFIX = "release/"
MAGIC = b"MHO1"
_NONCE = 12
FIELDS = ("date", "ref", "sha", "purpose")
_STANDARD = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_URLSAFE = re.compile(r"[A-Za-z0-9_-]+={0,2}")


class HoldoutError(RuntimeError):
    """A bad key, a corrupt or tampered file, an unreachable ledger, or a refused look."""


class BudgetExhausted(HoldoutError):
    """The quarter's 10 looks are spent (EVAL 5.3 rule 3)."""


class AlreadyLooked(HoldoutError):
    """This commit was already evaluated (EVAL 0: at most once per release candidate)."""


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _inside(path: Path, folder: Path) -> bool:
    path, folder = Path(path).resolve(), Path(folder).resolve()
    return path == folder or folder in path.parents


# ─── key and encryption ────────────────────────────────────────────────────────────────────


def generate_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def find_key(environ: Optional[Mapping[str, str]] = None) -> Optional[bytes]:
    """The 32-byte key from `MATCH_HOLDOUT_KEY`, or None when it is unset or blank. A value that is
    set but is not exactly a base64 (standard or URL-safe) 32-byte key raises: never a silent skip,
    and no stray character is ever ignored."""
    env = os.environ if environ is None else environ
    text = (env.get(KEY_ENV) or "").strip()
    if not text:
        return None
    padded = text + "=" * (-len(text) % 4)
    key: Optional[bytes] = None
    try:
        if _STANDARD.fullmatch(text):
            key = base64.b64decode(padded, validate=True)
        elif _URLSAFE.fullmatch(text):
            key = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        key = None
    if key is None or len(key) != 32:
        raise HoldoutError(f"{KEY_ENV} is set but is not a base64 32-byte key")
    return key


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


# ─── the look ledgers ──────────────────────────────────────────────────────────────────────


def quarter(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


def _row(day: date, ref: str, sha: str, purpose: str) -> tuple[str, ...]:
    for value in (ref, sha, purpose):
        if "\n" in value or "\r" in value:
            raise ValueError("ledger fields are single-line")
    return day.isoformat(), ref, sha, purpose


def _csv(rows: Sequence[Sequence[str]], *, header: bool) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if header:
        writer.writerow(FIELDS)
    writer.writerows(rows)
    return buffer.getvalue()


def _parse(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


class _LedgerBase:
    """One row per decrypting run. `claim` is the only path to a look: it reads the rows once, checks
    the quarter's budget and the commit, and appends the row before anything is decrypted."""

    def rows(self) -> list[dict[str, str]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _append(self, row: tuple[str, ...]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def looks_in_quarter(self, day: date, rows: Optional[list[dict[str, str]]] = None) -> int:
        rows = self.rows() if rows is None else rows
        q = quarter(day)
        count = 0
        for index, row in enumerate(rows):
            try:
                when = date.fromisoformat(row.get("date") or "")
            except ValueError:
                raise HoldoutError(f"ledger row {index + 1} has no valid date; refusing (fail closed)") from None
            count += quarter(when) == q
        return count

    def has_sha(self, sha: str, rows: Optional[list[dict[str, str]]] = None) -> bool:
        rows = self.rows() if rows is None else rows
        return any(r.get("sha") == sha for r in rows)

    def record(self, day: date, ref: str, sha: str, purpose: str) -> None:
        """Append without checks (seeding, migrations). Looks go through `claim`."""
        row = _row(day, ref, sha, purpose)
        self.rows()
        self._append(row)

    def claim(self, day: date, ref: str, sha: str, purpose: str) -> None:
        row = _row(day, ref, sha, purpose)
        if not sha:
            raise HoldoutError("no commit SHA to record; refusing")
        rows = self.rows()
        used = self.looks_in_quarter(day, rows)
        if used >= LOOKS_PER_QUARTER:
            year, q = quarter(day)
            raise BudgetExhausted(f"holdout look budget exhausted: {used} of {LOOKS_PER_QUARTER} looks used in "
                                  f"{year} Q{q} (EVAL 5.3); nothing was decrypted")
        if self.has_sha(sha, rows):
            raise AlreadyLooked(f"commit {sha} was already evaluated on the holdout (EVAL 0: once per release "
                                "candidate); nothing was decrypted")
        self._append(row)


class Ledger(_LedgerBase):
    """A CSV file: tests and local dry runs. CI never uses it (refused under GitHub Actions)."""

    def __init__(self, path: Path = LEDGER) -> None:
        self.path = Path(path)

    def rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        return _parse(self.path.read_text(encoding="utf-8"))

    def _append(self, row: tuple[str, ...]) -> None:
        new = not self.path.exists() or self.path.stat().st_size == 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(_csv([row], header=new))


class GitLedger(_LedgerBase):
    """The durable ledger: `LEDGER_FILE` on branch `LEDGER_BRANCH` of `remote`, shared by every release
    branch. Reads fetch the branch fresh; an append is a commit on the fetched tip pushed as a
    fast-forward (never forced), so two concurrent claims cannot both succeed. Plumbing only: the
    checkout's working tree and index are never touched."""

    _IDENTITY = {"GIT_AUTHOR_NAME": "match-engine holdout ledger", "GIT_AUTHOR_EMAIL": "holdout-ledger@invalid",
                 "GIT_COMMITTER_NAME": "match-engine holdout ledger",
                 "GIT_COMMITTER_EMAIL": "holdout-ledger@invalid"}

    def __init__(self, repo: Path, *, remote: str = "origin", branch: str = LEDGER_BRANCH,
                 filename: str = LEDGER_FILE) -> None:
        self.repo = Path(repo)
        self.remote = remote
        self.branch = branch
        self.filename = filename
        self._tip: Optional[str] = None
        self._rows: list[dict[str, str]] = []

    def _git(self, *args: str, data: Optional[bytes] = None) -> subprocess.CompletedProcess:
        env = {**os.environ, **self._IDENTITY, "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(["git", *args], cwd=self.repo, input=data, capture_output=True, env=env, check=False)

    def _out(self, *args: str, data: Optional[bytes] = None) -> str:
        done = self._git(*args, data=data)
        if done.returncode != 0:
            raise HoldoutError(f"ledger: git {args[0]} failed (exit {done.returncode}); refusing (fail closed)")
        return done.stdout.decode("utf-8")

    def rows(self) -> list[dict[str, str]]:
        found = self._git("ls-remote", "--exit-code", "--heads", self.remote, f"refs/heads/{self.branch}")
        if found.returncode == 2:  # the branch does not exist yet: the first look ever
            self._tip, self._rows = None, []
            return []
        if found.returncode != 0:
            raise HoldoutError(f"ledger: cannot read branch {self.branch!r} on the remote; refusing (fail closed)")
        tip = found.stdout.decode("utf-8").split()[0]
        self._out("fetch", "--no-tags", "-q", self.remote, f"refs/heads/{self.branch}")
        if self._out("rev-parse", "FETCH_HEAD").strip() != tip:
            raise HoldoutError("ledger: the branch moved while it was read; refusing (retry the job)")
        text = self._out("show", f"{tip}:{self.filename}")
        self._tip, self._rows = tip, _parse(text)
        return list(self._rows)

    def _append(self, row: tuple[str, ...]) -> None:
        rows = [tuple(r.get(f, "") for f in FIELDS) for r in self._rows] + [row]
        blob = self._out("hash-object", "-w", "--stdin", data=_csv(rows, header=True).encode("utf-8")).strip()
        tree = self._out("mktree", data=f"100644 blob {blob}\t{self.filename}\n".encode("utf-8")).strip()
        parent = ["-p", self._tip] if self._tip else []
        message = f"holdout look: {row[0]} {row[1]} {row[2]}"
        commit = self._out("commit-tree", tree, *parent, "-m", message).strip()
        self._push(commit)
        self._tip, self._rows = commit, _parse(_csv(rows, header=True))

    def _push(self, commit: str) -> None:
        self._out("push", "-q", self.remote, f"{commit}:refs/heads/{self.branch}")


def open_labels(key: bytes, *, ref: str, sha: str, purpose: str, ledger: _LedgerBase, labels_path: Path = LABELS,
                today: Optional[date] = None) -> bytes:
    """Decrypt the labels: one look, claimed durably first. Nothing is decrypted when the claim is
    refused or cannot be recorded."""
    today = today or utc_today()
    blob = Path(labels_path).read_bytes()
    ledger.claim(today, ref, sha, purpose)
    return decrypt(blob, key)


# ─── evaluation ────────────────────────────────────────────────────────────────────────────


def _from_env_or_git(environ: Mapping[str, str], names: Sequence[str], git_args: Sequence[str]) -> str:
    for name in names:
        if environ.get(name):
            return environ[name]
    try:
        return subprocess.run(["git", *git_args], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _jsonl(text: str, what: str) -> list[dict]:
    out = []
    for index, line in enumerate(line for line in text.splitlines() if line.strip()):
        try:
            out.append(json.loads(line))
        except ValueError as exc:
            raise HoldoutError(f"{what} {index} is malformed ({type(exc).__name__})") from None
    return out


def evaluate(labels_text: str, pairs_path: Path, *, proposal: Optional[Path] = None) -> int:
    """Score the holdout pairs and print aggregate metrics only. Returns the exit code. A malformed
    row raises `HoldoutError` naming its index and exception type, never its content."""
    from eval.metrics import abstention_stats, auc_roc, bootstrap_ci, ndcg_at_k
    from eval.suites import fairness_guardrail
    from eval.tune import proposal as proposals
    from eval.tune.search import EngineEvaluator
    from matcher.models import DimensionName, JobPosting, Resume

    labels = {}
    for index, row in enumerate(_jsonl(labels_text, "label")):
        try:
            labels[str(row["pair_id"])] = row
        except (KeyError, TypeError) as exc:
            raise HoldoutError(f"label {index} is malformed ({type(exc).__name__})") from None
    items = []
    for index, row in enumerate(_jsonl(Path(pairs_path).read_text(encoding="utf-8"), "pair")):
        try:
            label = labels.get(str(row["pair_id"]))
            if label is None:
                continue
            outcome = str(label.get("outcome") or "").lower()
            advanced = bool(label.get("advanced")) or outcome in ("advanced", "hired")
            items.append((row["posting_id"], row["pair_id"], Resume.model_validate(row["resume"]),
                          JobPosting.model_validate(row["posting"]), date.fromisoformat(row["as_of"]),
                          3 if outcome == "hired" else 2 if advanced else 0, advanced, label.get("group")))
        except Exception as exc:  # noqa: BLE001 - pydantic messages echo input text: report the type only
            raise HoldoutError(f"pair {index} is malformed ({type(exc).__name__})") from None
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
    ev.add_argument("--ref", default=None, help="the git ref being evaluated (default: from CI or git)")
    ev.add_argument("--sha", default=None, help="the commit being evaluated (default: GITHUB_SHA or git HEAD)")
    ev.add_argument("--purpose", default="release candidate evaluation")
    ev.add_argument("--proposal", default=None, help="proposals/<date>-tune folder to check (EVAL 5.3 rule 4)")
    ev.add_argument("--ledger-repo", default=str(ROOT), help="a git checkout to run the ledger commands in")
    ev.add_argument("--ledger-remote", default="origin")
    ev.add_argument("--ledger-branch", default=LEDGER_BRANCH)
    ev.add_argument("--local-ledger", default=None, help="a CSV ledger instead of the branch (never in CI)")
    enc = sub.add_parser("encrypt", help="encrypt a labels JSONL with the key in MATCH_HOLDOUT_KEY")
    enc.add_argument("--in", dest="source", required=True)
    enc.add_argument("--out", required=True)
    sub.add_parser("keygen", help="print a fresh base64 key (store it only as a `holdout` environment secret)")
    return parser


def _guarded(path: Path) -> bool:
    return _inside(path, CONFIG_DIR) or _inside(path, HOLDOUT_DIR)


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
        if _guarded(Path(args.out)):
            print("HOLDOUT: refused: --out may not be under config/ or eval/corpora/holdout/")
            return 2
        Path(args.out).write_bytes(encrypt(Path(args.source).read_bytes(), key))
        print(f"HOLDOUT: wrote {args.out}")
        return 0
    if key is None:
        print("HOLDOUT: skipped (no key)")
        return 0
    ref = args.ref or _from_env_or_git(env, ("GITHUB_REF_NAME", "GITHUB_HEAD_REF"), ("rev-parse", "--abbrev-ref", "HEAD"))
    if not ref.startswith(RELEASE_PREFIX):
        print(f"HOLDOUT: refused: the holdout decrypts only on release branches ({RELEASE_PREFIX}...), not {ref!r}")
        return 2
    ledger: _LedgerBase
    if args.local_ledger:
        if str(env.get("GITHUB_ACTIONS", "")).lower() == "true":
            print("HOLDOUT: refused: CI must use the durable ledger branch, not --local-ledger")
            return 2
        if _guarded(Path(args.local_ledger)):
            print("HOLDOUT: refused: --local-ledger may not be under config/ or eval/corpora/holdout/")
            return 2
        ledger = Ledger(Path(args.local_ledger))
    else:
        ledger = GitLedger(Path(args.ledger_repo), remote=args.ledger_remote, branch=args.ledger_branch)
    sha = args.sha or _from_env_or_git(env, ("GITHUB_SHA",), ("rev-parse", "HEAD"))
    day = today or utc_today()
    try:
        labels_text = open_labels(key, ref=ref, sha=sha, purpose=args.purpose, labels_path=Path(args.labels),
                                  ledger=ledger, today=day).decode("utf-8")
    except (BudgetExhausted, AlreadyLooked) as exc:
        print(f"HOLDOUT: refused: {exc}")
        return 3
    except (HoldoutError, OSError, ValueError) as exc:
        print(f"HOLDOUT: refused: {exc if isinstance(exc, HoldoutError) else type(exc).__name__}")
        return 2
    print(f"HOLDOUT: look recorded ({day}, {ref}, {sha}, {args.purpose})")
    try:
        return evaluate(labels_text, Path(args.pairs), proposal=Path(args.proposal) if args.proposal else None)
    except HoldoutError as exc:
        print(f"HOLDOUT: refused: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - never echo a message that may quote holdout data
        print(f"HOLDOUT: failed ({type(exc).__name__})")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
