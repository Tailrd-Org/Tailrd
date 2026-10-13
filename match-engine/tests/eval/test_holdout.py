"""Holdout tooling (Task 8.1, EVAL sections 0 and 5.3): AES-GCM labels, the key, the look ledger.

Every test works in a tmp dir with a key generated here. Nothing reads or writes
`eval/corpora/holdout/`.
"""

from __future__ import annotations

import base64
import csv
import json
from datetime import date
from pathlib import Path

import pytest

from eval import holdout

TODAY = date(2026, 10, 13)  # Q4 2026


@pytest.fixture()
def key() -> bytes:
    return base64.b64decode(holdout.generate_key())


def _env(key: bytes) -> dict[str, str]:
    return {holdout.KEY_ENV: base64.b64encode(key).decode("ascii")}


# ─── encryption ────────────────────────────────────────────────────────────────────────────


def test_generated_key_is_256_bits(key: bytes) -> None:
    assert len(key) == 32


def test_round_trip(key: bytes) -> None:
    plain = b'{"pair_id": "h1", "outcome": "advanced"}\n'
    blob = holdout.encrypt(plain, key)
    assert plain not in blob and blob.startswith(holdout.MAGIC)
    assert holdout.decrypt(blob, key) == plain


def test_encryption_is_randomised(key: bytes) -> None:
    assert holdout.encrypt(b"x", key) != holdout.encrypt(b"x", key)  # fresh nonce each time


def test_wrong_key_and_tampering_fail_loudly(key: bytes) -> None:
    blob = holdout.encrypt(b"labels", key)
    with pytest.raises(holdout.HoldoutError):
        holdout.decrypt(blob, base64.b64decode(holdout.generate_key()))
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(holdout.HoldoutError):
        holdout.decrypt(tampered, key)
    with pytest.raises(holdout.HoldoutError):
        holdout.decrypt(b"not a holdout file", key)


def test_find_key(key: bytes) -> None:
    assert holdout.find_key({}) is None
    assert holdout.find_key({holdout.KEY_ENV: "   "}) is None
    assert holdout.find_key(_env(key)) == key
    urlsafe = {holdout.KEY_ENV: base64.urlsafe_b64encode(key).decode("ascii")}
    assert holdout.find_key(urlsafe) == key
    with pytest.raises(holdout.HoldoutError):
        holdout.find_key({holdout.KEY_ENV: base64.b64encode(b"short").decode("ascii")})


def test_key_env_name_is_the_ci_secret() -> None:
    assert holdout.KEY_ENV == "MATCH_HOLDOUT_KEY"


# ─── the ledger ────────────────────────────────────────────────────────────────────────────


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _labels(tmp_path: Path, key: bytes, rows: list[dict] | None = None) -> Path:
    rows = rows if rows is not None else [{"pair_id": "h1", "outcome": "advanced", "advanced": True}]
    path = tmp_path / "labels.enc"
    path.write_bytes(holdout.encrypt("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"), key))
    return path


def test_every_decrypting_run_appends_a_ledger_row(tmp_path: Path, key: bytes) -> None:
    ledger = tmp_path / "ledger.csv"
    labels = _labels(tmp_path, key)
    plain = holdout.open_labels(key, ref="release/1.1", purpose="release candidate", labels_path=labels,
                                ledger_path=ledger, today=TODAY)
    assert b"h1" in plain
    assert _rows(ledger) == [{"date": "2026-10-13", "ref": "release/1.1", "purpose": "release candidate"}]
    holdout.open_labels(key, ref="release/1.1", purpose="rc2", labels_path=labels, ledger_path=ledger, today=TODAY)
    assert len(_rows(ledger)) == 2


def test_refuses_past_ten_looks_in_the_quarter(tmp_path: Path, key: bytes) -> None:
    ledger = tmp_path / "ledger.csv"
    labels = _labels(tmp_path, key)
    holdout.Ledger(ledger).record(date(2026, 9, 30), "release/0.9", "last quarter")  # Q3: does not count
    for i in range(10):
        holdout.Ledger(ledger).record(date(2026, 10, 1 + i), f"release/1.{i}", "rc")
    assert holdout.Ledger(ledger).looks_in_quarter(TODAY) == 10
    with pytest.raises(holdout.BudgetExhausted):
        holdout.open_labels(key, ref="release/2.0", purpose="one more", labels_path=labels, ledger_path=ledger,
                            today=TODAY)
    assert len(_rows(ledger)) == 11  # the refused look is not recorded and nothing was decrypted


def test_next_quarter_has_a_fresh_budget(tmp_path: Path, key: bytes) -> None:
    ledger = tmp_path / "ledger.csv"
    for i in range(10):
        holdout.Ledger(ledger).record(date(2026, 10, 1 + i), "release/1", "rc")
    assert holdout.Ledger(ledger).looks_in_quarter(date(2027, 1, 2)) == 0
    assert holdout.LOOKS_PER_QUARTER == 10


def test_ledger_rejects_commas_and_newlines_in_fields(tmp_path: Path) -> None:
    holdout.Ledger(tmp_path / "l.csv").record(TODAY, "release/1", "purpose, with comma")
    assert _rows(tmp_path / "l.csv")[0]["purpose"] == "purpose, with comma"  # csv-quoted, not split
    with pytest.raises(ValueError):
        holdout.Ledger(tmp_path / "l.csv").record(TODAY, "release/1", "two\nlines")


def test_committed_ledger_has_the_header() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "evaluation_budget.ledger.csv"
    assert path.read_text(encoding="utf-8").splitlines()[0] == "date,ref,purpose"


# ─── the CLI (`make eval-holdout`) ─────────────────────────────────────────────────────────


def test_without_a_key_it_skips_and_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nowhere" / "labels.enc"
    code = holdout.main(["evaluate", "--labels", str(missing), "--ledger", str(tmp_path / "ledger.csv")],
                        environ={}, today=TODAY)
    assert code == 0
    assert "HOLDOUT: skipped (no key)" in capsys.readouterr().out
    assert not (tmp_path / "ledger.csv").exists()


def test_refuses_outside_a_release_branch(tmp_path: Path, key: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    labels = _labels(tmp_path, key)
    code = holdout.main(["evaluate", "--labels", str(labels), "--ledger", str(tmp_path / "ledger.csv"),
                         "--ref", "feat/x"], environ=_env(key), today=TODAY)
    assert code == 2 and "release" in capsys.readouterr().out
    assert not (tmp_path / "ledger.csv").exists()


def test_exhausted_budget_exits_nonzero(tmp_path: Path, key: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "ledger.csv"
    for i in range(10):
        holdout.Ledger(ledger).record(date(2026, 10, 1 + i), "release/1", "rc")
    code = holdout.main(["evaluate", "--labels", str(_labels(tmp_path, key)), "--ledger", str(ledger),
                         "--ref", "release/2"], environ=_env(key), today=TODAY)
    assert code == 3 and "budget" in capsys.readouterr().out.lower()


def test_encrypt_command_round_trips(tmp_path: Path, key: bytes) -> None:
    plain = tmp_path / "labels.jsonl"
    plain.write_text('{"pair_id": "h1", "outcome": "rejected", "advanced": false}\n', encoding="utf-8")
    out = tmp_path / "labels.enc"
    assert holdout.main(["encrypt", "--in", str(plain), "--out", str(out)], environ=_env(key), today=TODAY) == 0
    assert holdout.decrypt(out.read_bytes(), key) == plain.read_bytes()


def test_default_paths_are_the_holdout_folder_and_the_docs_ledger() -> None:
    assert holdout.HOLDOUT_DIR.parts[-3:] == ("eval", "corpora", "holdout")
    assert holdout.LEDGER.name == "evaluation_budget.ledger.csv" and holdout.LEDGER.parent.name == "docs"


def test_golden_layer4_still_reports_skipped_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from eval.suites import golden

    monkeypatch.delenv(holdout.KEY_ENV, raising=False)
    result = golden.run_holdout()
    assert result.status == "skipped" and golden.HOLDOUT_SKIPPED in result.notes


def test_evaluate_scores_a_tmp_holdout_and_prints_aggregates_only(
        tmp_path: Path, key: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    """A tiny holdout built from synthetic pairs (no real text), in tmp."""
    from eval.suites import corpus_id_map, corpus_pairs, synthetic_corpus

    corpus = synthetic_corpus()
    chosen = corpus_pairs(corpus, corpus_id_map())[:12]
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text("".join(json.dumps({
        "pair_id": f"hx{i}", "posting_id": cp.posting_id, "as_of": corpus.as_of.isoformat(),
        "resume": cp.resume.model_dump(mode="json"), "posting": cp.posting.model_dump(mode="json")}) + "\n"
        for i, cp in enumerate(chosen)), encoding="utf-8")
    labels = _labels(tmp_path, key, [{"pair_id": f"hx{i}", "outcome": "advanced" if cp.profile.fit_level >= 2
                                      else "rejected"} for i, cp in enumerate(chosen)])
    current = {"required_skills": 0.35, "preferred_skills": 0.10, "experience": 0.15, "seniority": 0.10,
               "domain": 0.10, "education": 0.10, "tools": 0.10}
    folder = tmp_path / "2026-10-13-tune"
    folder.mkdir()
    (folder / "proposal.json").write_text(json.dumps({
        "proposed": {**current, "required_skills": 0.30, "tools": 0.15}, "current": current,
        "dev_gain": {"point": 0.05, "low": 0.02, "high": 0.08}, "synthetic_only": True,
        "fairness_assessed": False}), encoding="utf-8")
    ledger = tmp_path / "ledger.csv"
    code = holdout.main(["evaluate", "--labels", str(labels), "--pairs", str(pairs), "--ledger", str(ledger),
                         "--ref", "release/1.0", "--purpose", "test", "--proposal", str(folder)],
                        environ=_env(key), today=TODAY)
    out = capsys.readouterr().out
    assert code == 0
    assert "NDCG@10" in out and "Rule 4" in out and "Verdict: " in out
    assert "hx1" not in out  # never a per-pair line
    assert _rows(ledger) == [{"date": "2026-10-13", "ref": "release/1.0", "purpose": "test"}]
