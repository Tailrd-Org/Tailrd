"""Config loads, validates, and holds the spec's numbers (BUILD Milestone 1 gate)."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest
import yaml

from matcher.config import (
    WEIGHT_MAX,
    WEIGHT_MIN,
    ConfigError,
    combined_hash,
    default_config_dir,
    load_config,
    replace_weights,
)
from matcher.models import Band, DimensionName, VerbClass


def test_weights_sum_to_one(config) -> None:
    total = math.fsum(config.weights.of(d) for d in DimensionName)
    assert math.isclose(total, 1.0, abs_tol=1e-9)
    for d in DimensionName:
        assert WEIGHT_MIN <= config.weights.of(d) <= WEIGHT_MAX


def test_spec_weights(config) -> None:
    expected = {"required_skills": 0.35, "preferred_skills": 0.10, "experience": 0.15, "seniority": 0.10,
                "domain": 0.10, "education": 0.10, "tools": 0.10}
    assert {d.value: config.weights.of(d) for d in DimensionName} == expected


def _copy_config(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(default_config_dir(), target)
    return target


def _edit_weights(root: Path, **changes: float) -> None:
    path = root / "weights.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["dimensions"].update(changes)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_weights_not_summing_to_one_fail_at_load(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    _edit_weights(root, tools=0.11)
    with pytest.raises(ConfigError, match="sum"):
        load_config(root)


def test_weight_bounds_enforced(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    _edit_weights(root, required_skills=0.47, tools=0.02, preferred_skills=0.06)
    with pytest.raises(ConfigError):
        load_config(root)


def test_missing_dimension_fails(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    path = root / "weights.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["dimensions"]["tools"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="missing"):
        load_config(root)


def test_missing_file_fails(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    (root / "gates.yaml").unlink()
    with pytest.raises(ConfigError, match="missing"):
        load_config(root)


def test_hash_is_stable_and_ignores_formatting(tmp_path: Path, config) -> None:
    assert load_config().content_hash == config.content_hash
    root = _copy_config(tmp_path)
    path = root / "scoring.yaml"
    path.write_text("# a new comment\n\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert load_config(root).content_hash == config.content_hash


def test_hash_changes_with_values(config) -> None:
    moved = replace_weights(config, {**{d.value: config.weights.of(d) for d in DimensionName},
                                     "required_skills": 0.30, "preferred_skills": 0.15})
    assert moved.content_hash != config.content_hash
    assert moved.weights.of(DimensionName.REQUIRED_SKILLS) == 0.30
    assert combined_hash(config, "graph-a") != combined_hash(config, "graph-b")


def test_replace_weights_validates(config) -> None:
    with pytest.raises(ValueError):
        replace_weights(config, {d.value: 0.2 for d in DimensionName})


@pytest.mark.parametrize(
    "score,band",
    [(100, Band.STRONG), (85, Band.STRONG), (84, Band.GOOD), (70, Band.GOOD), (69, Band.PARTIAL),
     (55, Band.PARTIAL), (54, Band.LIMITED), (0, Band.LIMITED)],
)
def test_bands(config, score: int, band: Band) -> None:
    assert config.bands.band_for(score) is band


def test_verbs_match_spec(config) -> None:
    v = config.verbs
    spec = {
        VerbClass.OWNERSHIP: (1.00, ["architected", "designed", "built", "owned", "led", "founded", "created",
                                     "spearheaded", "established", "pioneered", "drove"]),
        VerbClass.EXECUTION: (0.85, ["developed", "implemented", "engineered", "delivered", "migrated", "automated",
                                     "optimized", "deployed", "maintained", "scaled"]),
        VerbClass.PARTICIPATION: (0.65, ["contributed", "collaborated", "participated", "worked", "used", "utilized"]),
        VerbClass.ASSISTANCE: (0.45, ["assisted", "supported", "helped", "shadowed", "observed"]),
        VerbClass.EXPOSURE: (0.30, ["exposed", "familiar", "aware", "coursework in", "basic knowledge"]),
    }
    for cls, (mult, verbs) in spec.items():
        assert v.multiplier(cls) == mult
        for verb in verbs:
            assert verb in v.classes[cls].verbs
    assert v.multiplier(VerbClass.NO_VERB) == 0.70
    assert v.quantified_bonus == 1.15 and v.quantified_cap == 1.0
    assert "for" not in v.coordinating_conjunctions  # DECISIONS D-34a


def test_negation_matches_spec(config) -> None:
    n = config.negation
    assert n.window_tokens == 6
    assert n.hard_negation.m == 0.0 and n.aspirational.m == 0.15
    assert {"not", "no", "without", "lacking", "never", "no experience"} <= set(n.hard_negation.terms)
    assert all(isinstance(t, str) for t in n.hard_negation.terms)  # YAML must not turn "no" into False
    assert {"seeking", "aspiring", "looking to", "interested in", "eager to learn", "currently learning",
            "studying", "hope to", "want to"} <= set(n.aspirational.terms)


def test_evidence_grammar_config(config) -> None:
    # DECISIONS D-34c, D-34d, D-34e
    v, hard = config.verbs, config.negation.hard_negation
    assert v.defer_to_earlier == ("using", "utilizing", "utilising", "leverage", "leverages", "leveraged",
                                  "leveraging")
    leverage = {"leverage", "leverages", "leveraged", "leveraging"}
    assert leverage <= set(v.classes[VerbClass.PARTICIPATION].verbs)  # a no-op unless they are verbs
    assert v.noun_entries == ("exposure", "familiarity", "awareness")
    assert set(v.noun_entry_followers) == {"of", "to", "with", "in"}
    assert {".", ";", ",", "!", "?"} == set(v.clause_punctuation)  # "!" and "?" end sentences (D-34b)
    assert {"react", "java", "python", "node", "windows", "excel", "office"} <= set(v.version_names)
    assert "go" not in v.version_names  # "go" is an ordinary verb in lower case
    ruled = {"experience", "knowledge", "prior", "previous", "professional", "formal", "hands-on",
             "working", "real-world", "familiarity", "exposure", "background", "expertise", "skills",
             "proficiency", "familiar", "proficient", "experienced", "comfortable", "skilled", "used",
             "worked", "using", "with", "in", "of", "on", "at", "to", "any", "the", "a", "an",
             # fix round 2
             "direct", "practical", "commercial", "industry", "production", "real", "extensive",
             "significant", "relevant", "much", "very", "really", "fully", "currently", "previously",
             "before", "once", "too"}
    assert ruled <= set(hard.scope_fillers)
    assert all(isinstance(f, str) for f in hard.scope_fillers)  # YAML must not turn "on" into True
    assert hard.exceptions == ("not only",)


def test_noun_entries_must_be_lexicon_entries(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    path = root / "verbs.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["noun_entries"].append("synergy")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="synergy"):
        load_config(root)


def test_scoring_constants_match_spec(config) -> None:
    s = config.scoring
    assert (s.evidence.SKILLS_BLOB, s.evidence.ROLE_BULLET, s.evidence.ROLE_BULLET_QUANTIFIED,
            s.evidence.ROLE_TITLE, s.evidence.CERTIFICATION) == (0.65, 0.85, 1.00, 1.00, 1.00)
    assert (s.recency.decay_per_year, s.recency.floor) == (0.15, 0.55)
    assert (s.relations.exact, s.relations.child, s.relations.parent) == (1.0, 0.85, 0.40)
    assert (s.relations.sibling_min, s.relations.sibling_max) == (0.40, 0.80)
    assert s.relations.provisional_multiplier == 0.80
    assert (s.critical_miss.idf_threshold, s.critical_miss.m_threshold) == (0.70, 0.20)
    assert (s.critical_miss.cap_one, s.critical_miss.cap_two_or_more) == (0.70, 0.55)
    assert (s.experience.knee_ratio, s.experience.value_at_knee, s.experience.internship_weight) == (0.5, 0.60, 0.5)
    assert (s.seniority.step, s.seniority.cross_track_penalty) == (0.28, 0.20)
    assert s.seniority.penalize_overqualification is False  # DECISIONS D-39
    assert (s.domain.exact, s.domain.same_3digit, s.domain.same_2digit, s.domain.unrelated) == (1.0, 0.70, 0.40, 0.15)
    assert (s.education.meets, s.education.one_below, s.education.two_or_more_below) == (1.0, 0.55, 0.20)
    assert s.education.no_requirement == 1.0
    assert (s.education.field_multiplier_min, s.education.cert_bonus_each, s.education.cert_bonus_cap) == (0.7, 0.1, 0.3)
    assert s.posting.max_weighted_requirements == 25
    assert [tuple(p) for p in s.confidence.requirement_count_curve] == [(1, 0.40), (5, 0.85), (10, 1.00)]
    assert (s.confidence.scored_threshold, s.confidence.low_confidence_threshold) == (0.80, 0.60)
    assert s.confidence.parse_quality == {"A": 1.0, "B": 0.8, "C": 0.55}
    assert s.intervals.display_spread == 6
    assert (s.taxonomy.fuzzy_max_distance, s.taxonomy.fuzzy_min_token_length) == (2, 6)
    assert (s.taxonomy.pmi_threshold, s.taxonomy.max_out_degree) == (0.40, 20)
    assert s.taxonomy.coverage_alert_threshold == 0.92
    assert (s.taxonomy.auto_promote_min_postings, s.taxonomy.auto_promote_window_days,
            s.taxonomy.review_queue_size) == (50, 30, 50)
    assert s.calibration.min_reference_n == 30
    assert (s.ingest.max_experience_tokens, s.ingest.ocr_min_confidence) == (40000, 0.60)
    assert (s.ingest.max_pages, s.ingest.resume_likelihood_threshold) == (100, 0.5)


@pytest.mark.parametrize("key,value", [("max_pages", 0), ("max_pages", 2.5), ("resume_likelihood_threshold", 1.5),
                                       ("resume_likelihood_threshold", -0.1)])
def test_ingest_limits_are_validated(tmp_path: Path, key: str, value: float) -> None:
    root = _copy_config(tmp_path)
    path = root / "scoring.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["ingest"][key] = value
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(root)


def test_gates_policy(config) -> None:
    g = config.gates
    assert g.location.mismatch_status == "WARN"  # soft by SPEC §4
    assert g.work_authorization.unknown_status == "WARN"  # DECISIONS D-60
    assert "cred.registered_nurse" in g.license.professional_credential_ids
    assert g.security_clearance.obtainable_status == "WARN"
    assert g.security_clearance.active_missing_status == "FAIL"


def test_engine_version_is_semver(config) -> None:
    parts = config.engine_version.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
