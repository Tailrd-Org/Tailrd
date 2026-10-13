"""Holdout tooling (Task 8.1, EVAL sections 0 and 5.3): AES-GCM labels, the key, the look ledger.

Every test works in a tmp dir with a key generated here. Nothing reads or writes
`eval/corpora/holdout/`.
"""

from __future__ import annotations

import base64
import csv
import json
import subprocess
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




def test_find_key_rejects_junk_characters(key: bytes) -> None:
    text = base64.b64encode(key).decode("ascii")
    for junk in (text[:8] + "!!**" + text[8:], text[:20] + "\n" + text[20:], text[:10] + " " + text[10:]):
        with pytest.raises(holdout.HoldoutError):
            holdout.find_key({holdout.KEY_ENV: junk})


# ─── the ledgers ───────────────────────────────────────────────────────────────────────────

SHA = "a" * 40


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _labels(tmp_path: Path, key: bytes, rows: list[dict] | None = None) -> Path:
    rows = rows if rows is not None else [{"pair_id": "h1", "outcome": "advanced", "advanced": True}]
    path = tmp_path / "labels.enc"
    path.write_bytes(holdout.encrypt("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"), key))
    return path


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    """A bare repository standing in for origin; the ledger branch lives there."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-q", cwd=bare)
    return bare


def _work(tmp_path: Path, name: str = "work") -> Path:
    work = tmp_path / name
    work.mkdir()
    _git("init", "-q", cwd=work)
    return work


def _git_ledger(tmp_path: Path, remote: Path, name: str = "work") -> "holdout.GitLedger":
    return holdout.GitLedger(_work(tmp_path, name), remote=str(remote))


def _never(*_a, **_k):  # noqa: ANN202
    raise AssertionError("decrypt must not be called")


def test_every_decrypting_run_appends_a_ledger_row(tmp_path: Path, key: bytes) -> None:
    ledger = holdout.Ledger(tmp_path / "ledger.csv")
    labels = _labels(tmp_path, key)
    plain = holdout.open_labels(key, ref="release/1.1", sha=SHA, purpose="release candidate", labels_path=labels,
                                ledger=ledger, today=TODAY)
    assert b"h1" in plain
    assert _rows(tmp_path / "ledger.csv") == [
        {"date": "2026-10-13", "ref": "release/1.1", "sha": SHA, "purpose": "release candidate"}]
    holdout.open_labels(key, ref="release/1.2", sha="b" * 40, purpose="rc2", labels_path=labels, ledger=ledger,
                        today=TODAY)
    assert len(_rows(tmp_path / "ledger.csv")) == 2


def test_a_second_look_at_the_same_commit_is_refused(tmp_path: Path, key: bytes, monkeypatch) -> None:
    """EVAL 0: at most once per release candidate (a re-run or a re-push of the same SHA)."""
    ledger = holdout.Ledger(tmp_path / "ledger.csv")
    labels = _labels(tmp_path, key)
    holdout.open_labels(key, ref="release/1", sha=SHA, purpose="rc", labels_path=labels, ledger=ledger, today=TODAY)
    monkeypatch.setattr(holdout, "decrypt", _never)
    with pytest.raises(holdout.AlreadyLooked):
        holdout.open_labels(key, ref="release/1", sha=SHA, purpose="re-run", labels_path=labels, ledger=ledger,
                            today=TODAY)
    assert len(_rows(tmp_path / "ledger.csv")) == 1


def test_refuses_past_ten_looks_in_the_quarter(tmp_path: Path, key: bytes, monkeypatch) -> None:
    ledger = holdout.Ledger(tmp_path / "ledger.csv")
    labels = _labels(tmp_path, key)
    ledger.record(date(2026, 9, 30), "release/0.9", "0" * 40, "last quarter")  # Q3: does not count
    for i in range(10):
        ledger.record(date(2026, 10, 1 + i), f"release/1.{i}", f"{i:040d}", "rc")
    assert ledger.looks_in_quarter(TODAY) == 10
    monkeypatch.setattr(holdout, "decrypt", _never)
    with pytest.raises(holdout.BudgetExhausted):
        holdout.open_labels(key, ref="release/2.0", sha=SHA, purpose="one more", labels_path=labels, ledger=ledger,
                            today=TODAY)
    assert len(_rows(tmp_path / "ledger.csv")) == 11  # the refused look is not recorded


def test_next_quarter_has_a_fresh_budget(tmp_path: Path) -> None:
    ledger = holdout.Ledger(tmp_path / "ledger.csv")
    for i in range(10):
        ledger.record(date(2026, 10, 1 + i), "release/1", f"{i:040d}", "rc")
    assert ledger.looks_in_quarter(date(2027, 1, 2)) == 0
    assert holdout.LOOKS_PER_QUARTER == 10


def test_ledger_quotes_commas_and_rejects_newlines(tmp_path: Path) -> None:
    ledger = holdout.Ledger(tmp_path / "l.csv")
    ledger.record(TODAY, "release/1", SHA, "purpose, with comma")
    assert _rows(tmp_path / "l.csv")[0]["purpose"] == "purpose, with comma"  # csv-quoted, not split
    with pytest.raises(ValueError):
        ledger.record(TODAY, "release/1", SHA, "two\nlines")


def test_a_malformed_ledger_row_fails_closed(tmp_path: Path, key: bytes, capsys: pytest.CaptureFixture[str],
                                             monkeypatch) -> None:
    path = tmp_path / "ledger.csv"
    path.write_text("date,ref,sha,purpose\nnot-a-date,release/1,x,rc\n", encoding="utf-8")
    with pytest.raises(holdout.HoldoutError):
        holdout.Ledger(path).looks_in_quarter(TODAY)
    monkeypatch.setattr(holdout, "decrypt", _never)
    code = holdout.main(["evaluate", "--labels", str(_labels(tmp_path, key)), "--local-ledger", str(path),
                         "--ref", "release/1", "--sha", SHA], environ=_env(key), today=TODAY)
    assert code == 2 and "refused" in capsys.readouterr().out


def test_committed_ledger_mirror_has_the_header() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "evaluation_budget.ledger.csv"
    assert path.read_text(encoding="utf-8").splitlines()[0] == "date,ref,sha,purpose"


# ─── the durable ledger (a branch every release branch reads) ──────────────────────────────


def test_git_ledger_records_by_pushing_and_another_checkout_sees_it(tmp_path: Path, remote: Path) -> None:
    first = _git_ledger(tmp_path, remote, "a")
    assert first.rows() == []  # the branch does not exist yet
    first.record(TODAY, "release/1", SHA, "rc")
    second = _git_ledger(tmp_path, remote, "b")  # a fresh checkout, as on the next CI run
    assert second.rows() == [{"date": "2026-10-13", "ref": "release/1", "sha": SHA, "purpose": "rc"}]
    second.record(TODAY, "release/2", "b" * 40, "rc")
    assert first.looks_in_quarter(TODAY) == 2 and first.has_sha("b" * 40)


def test_durable_count_of_ten_refuses_even_with_an_empty_local_csv(
        tmp_path: Path, remote: Path, key: bytes, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    seed = _git_ledger(tmp_path, remote, "seed")
    for i in range(10):
        seed.record(date(2026, 10, 1 + i), f"release/1.{i}", f"{i:040d}", "rc")
    local = tmp_path / "evaluation_budget.ledger.csv"
    local.write_text("date,ref,sha,purpose\n", encoding="utf-8")  # the checkout's mirror: empty
    work = _work(tmp_path, "ci")
    monkeypatch.setattr(holdout, "decrypt", _never)
    code = holdout.main(["evaluate", "--labels", str(_labels(tmp_path, key)), "--ledger-repo", str(work),
                         "--ledger-remote", str(remote), "--ref", "release/2", "--sha", SHA],
                        environ=_env(key), today=TODAY)
    assert code == 3 and "budget" in capsys.readouterr().out.lower()
    assert len(_git_ledger(tmp_path, remote, "check").rows()) == 10  # the refused look was not recorded


def test_unreachable_ledger_refuses_and_decrypts_nothing(tmp_path: Path, key: bytes,
                                                         capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    monkeypatch.setattr(holdout, "decrypt", _never)
    code = holdout.main(["evaluate", "--labels", str(_labels(tmp_path, key)), "--ledger-repo",
                         str(_work(tmp_path)), "--ledger-remote", str(tmp_path / "no-such-remote.git"),
                         "--ref", "release/2", "--sha", SHA], environ=_env(key), today=TODAY)
    assert code == 2 and "refused" in capsys.readouterr().out


def test_rejected_push_refuses_and_decrypts_nothing(tmp_path: Path, remote: Path, key: bytes, monkeypatch) -> None:
    ledger = _git_ledger(tmp_path, remote)
    monkeypatch.setattr(holdout, "decrypt", _never)

    def failing_push(*_a, **_k):  # noqa: ANN202
        raise holdout.HoldoutError("push rejected")

    monkeypatch.setattr(ledger, "_push", failing_push)
    with pytest.raises(holdout.HoldoutError):
        holdout.open_labels(key, ref="release/1", sha=SHA, purpose="rc", labels_path=_labels(tmp_path, key),
                            ledger=ledger, today=TODAY)


def test_local_ledger_is_refused_inside_github_actions(tmp_path: Path, key: bytes,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    code = holdout.main(["evaluate", "--labels", str(_labels(tmp_path, key)), "--local-ledger",
                         str(tmp_path / "l.csv"), "--ref", "release/1", "--sha", SHA],
                        environ={**_env(key), "GITHUB_ACTIONS": "true"}, today=TODAY)
    assert code == 2 and "durable" in capsys.readouterr().out
    assert not (tmp_path / "l.csv").exists()


# ─── the CLI (`make eval-holdout`) ─────────────────────────────────────────────────────────


def test_without_a_key_it_skips_and_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nowhere" / "labels.enc"
    code = holdout.main(["evaluate", "--labels", str(missing), "--local-ledger", str(tmp_path / "ledger.csv")],
                        environ={}, today=TODAY)
    assert code == 0
    assert "HOLDOUT: skipped (no key)" in capsys.readouterr().out
    assert not (tmp_path / "ledger.csv").exists()


def test_refuses_outside_a_release_branch(tmp_path: Path, key: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    labels = _labels(tmp_path, key)
    code = holdout.main(["evaluate", "--labels", str(labels), "--local-ledger", str(tmp_path / "ledger.csv"),
                         "--ref", "feat/x", "--sha", SHA], environ=_env(key), today=TODAY)
    assert code == 2 and "release" in capsys.readouterr().out
    assert not (tmp_path / "ledger.csv").exists()


def test_exhausted_budget_exits_3(tmp_path: Path, key: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = holdout.Ledger(tmp_path / "ledger.csv")
    for i in range(10):
        ledger.record(date(2026, 10, 1 + i), "release/1", f"{i:040d}", "rc")
    code = holdout.main(["evaluate", "--labels", str(_labels(tmp_path, key)), "--local-ledger",
                         str(tmp_path / "ledger.csv"), "--ref", "release/2", "--sha", SHA],
                        environ=_env(key), today=TODAY)
    assert code == 3 and "budget" in capsys.readouterr().out.lower()


def test_encrypt_command_round_trips(tmp_path: Path, key: bytes) -> None:
    plain = tmp_path / "labels.jsonl"
    plain.write_text('{"pair_id": "h1", "outcome": "rejected", "advanced": false}\n', encoding="utf-8")
    out = tmp_path / "labels.enc"
    assert holdout.main(["encrypt", "--in", str(plain), "--out", str(out)], environ=_env(key), today=TODAY) == 0
    assert holdout.decrypt(out.read_bytes(), key) == plain.read_bytes()


def test_cli_never_writes_under_config_or_the_holdout_folder(tmp_path: Path, key: bytes, monkeypatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    # The guarded folders default to the real ones; the test points them at tmp copies so that a broken
    # guard can never damage config/ or create anything under eval/corpora/holdout/.
    assert holdout.CONFIG_DIR == holdout.ROOT / "config"
    config = tmp_path / "config"
    config.mkdir()
    (config / "weights.yaml").write_bytes((holdout.ROOT / "config" / "weights.yaml").read_bytes())
    monkeypatch.setattr(holdout, "CONFIG_DIR", config)
    monkeypatch.setattr(holdout, "HOLDOUT_DIR", tmp_path / "holdout")
    before = (config / "weights.yaml").read_bytes()
    plain = tmp_path / "labels.jsonl"
    plain.write_text("{}\n", encoding="utf-8")
    for target in (config / "weights.yaml", config / "new.enc", tmp_path / "holdout" / "labels.enc"):
        assert holdout.main(["encrypt", "--in", str(plain), "--out", str(target)], environ=_env(key),
                            today=TODAY) == 2
    assert holdout.main(["evaluate", "--labels", str(_labels(tmp_path, key)), "--local-ledger",
                         str(config / "weights.yaml"), "--ref", "release/1", "--sha", SHA],
                        environ=_env(key), today=TODAY) == 2
    assert (config / "weights.yaml").read_bytes() == before
    assert not (config / "new.enc").exists() and not (tmp_path / "holdout").exists()
    assert "refused" in capsys.readouterr().out


def test_default_paths_and_utc_date() -> None:
    from datetime import datetime, timezone

    assert holdout.HOLDOUT_DIR.parts[-3:] == ("eval", "corpora", "holdout")
    assert holdout.LEDGER.name == "evaluation_budget.ledger.csv" and holdout.LEDGER.parent.name == "docs"
    assert holdout.LEDGER_BRANCH == "holdout-ledger"
    assert holdout.utc_today() == datetime.now(timezone.utc).date()


def test_makefile_eval_holdout_runs_the_cli() -> None:
    text = (holdout.ROOT / "Makefile").read_text(encoding="utf-8")
    block = text.split("\neval-holdout:\n", 1)[1].split("\n\n", 1)[0]
    assert "-m eval.holdout evaluate" in block


def test_golden_layer4_still_reports_skipped_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from eval.suites import golden

    monkeypatch.delenv(holdout.KEY_ENV, raising=False)
    result = golden.run_holdout()
    assert result.status == "skipped" and golden.HOLDOUT_SKIPPED in result.notes


# ─── evaluation on a tmp holdout built from synthetic pairs (no real text) ─────────────────

MARKER = "zz-label-marker-7731"


def _tmp_holdout(tmp_path: Path, key: bytes, n: int = 12) -> tuple[Path, Path, list]:
    from eval.suites import corpus_id_map, corpus_pairs, synthetic_corpus

    corpus = synthetic_corpus()
    chosen = corpus_pairs(corpus, corpus_id_map())[:n]
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text("".join(json.dumps({
        "pair_id": f"hx{i}", "posting_id": cp.posting_id, "as_of": corpus.as_of.isoformat(),
        "resume": cp.resume.model_dump(mode="json"), "posting": cp.posting.model_dump(mode="json")}) + "\n"
        for i, cp in enumerate(chosen)), encoding="utf-8")
    labels = _labels(tmp_path, key, [{"pair_id": f"hx{i}", "note": MARKER,
                                      "outcome": "advanced" if cp.profile.fit_level >= 2 else "rejected"}
                                     for i, cp in enumerate(chosen)])
    return pairs, labels, chosen


def test_evaluate_prints_aggregates_only_and_leaks_nothing(
        tmp_path: Path, key: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    pairs, labels, _ = _tmp_holdout(tmp_path, key)
    current = {"required_skills": 0.35, "preferred_skills": 0.10, "experience": 0.15, "seniority": 0.10,
               "domain": 0.10, "education": 0.10, "tools": 0.10}
    folder = tmp_path / "2026-10-13-tune"
    folder.mkdir()
    (folder / "proposal.json").write_text(json.dumps({
        "proposed": {**current, "required_skills": 0.30, "tools": 0.15}, "current": current,
        "dev_gain": {"point": 0.05, "low": 0.02, "high": 0.08}, "synthetic_only": True,
        "fairness_assessed": False}), encoding="utf-8")
    ledger = tmp_path / "ledger.csv"
    key_text = _env(key)[holdout.KEY_ENV]
    code = holdout.main(["evaluate", "--labels", str(labels), "--pairs", str(pairs), "--local-ledger", str(ledger),
                         "--ref", "release/1.0", "--sha", SHA, "--purpose", "test", "--proposal", str(folder)],
                        environ=_env(key), today=TODAY)
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert code == 0
    assert "NDCG@10" in out and "Rule 4" in out and "Verdict: " in out
    assert "hx1" not in out  # never a per-pair line
    assert MARKER not in out and key_text not in out
    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert MARKER.encode() not in data and key_text.encode() not in data, path
    assert _rows(ledger) == [{"date": "2026-10-13", "ref": "release/1.0", "sha": SHA, "purpose": "test"}]


def test_a_malformed_pair_prints_only_its_index_and_type(tmp_path: Path, key: bytes,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    pairs, labels, _ = _tmp_holdout(tmp_path, key, n=3)
    rows = [json.loads(line) for line in pairs.read_text(encoding="utf-8").splitlines()]
    rows[1]["resume"] = {"roles": "SECRET-RESUME-TEXT-991"}  # invalid for the model
    pairs.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    code = holdout.main(["evaluate", "--labels", str(labels), "--pairs", str(pairs), "--local-ledger",
                         str(tmp_path / "l.csv"), "--ref", "release/1", "--sha", SHA], environ=_env(key), today=TODAY)
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert code == 2
    assert "pair 1" in out and "ValidationError" in out
    assert "SECRET-RESUME-TEXT-991" not in out and "input_value" not in out
