"""
The Tailrd adapter for the deterministic match engine (match-engine/, package ``matcher``).

Every job score Tailrd shows comes from here (Task 9.2 switched every producer and reader): no AI is
called, waited on or paid for (DECISIONS D-80..D-85, match-engine/docs/DECISIONS.md). This module
is the only place the backend touches the engine (D-01: it puts ``match-engine/src`` on
``sys.path``).

What it does
    ``get_engine()``            the process-wide engine, built lazily once: config, skill graph,
                                resolver, parse config, title and MOS indexes, the verified
                                calibration panel and the combined config hash.
    ``resume_for_profile``      a stored résumé (the document, else its raw text; D-80) plus the
                                saved work authorisation / location answers -> ``ParsedResume``.
    ``postings_for``            jobs -> ``JobPosting``, cached in ``scraped_jobs.match_terms`` and
                                recomputed when the parser version or the description length
                                changes (D-84). ``posting_for_job`` does one ORM row.
    ``fingerprint_for`` /
    ``current_fingerprints``    "fit<major>:" + sha256(canonical Resume JSON + combined hash)
                                (D-82). Old AI and "local1:" rows are never current.
    ``bank_scores``             scores every job without a current row, stores the score columns
                                and the audit trace (match_traces, keyed by an opaque id).
    ``refresh_calibration``     per-family / global references from banked raw scores (D-50).
    ``explain_job``             one full result (explanation, interval, counterfactual).
    ``build_shadow_engine`` /
    ``shadow_scores``           EVAL section 6 shadow scoring (FIT_SHADOW_CONFIG, D-150): the same
                                résumé and postings scored with a candidate config, in memory.
    ``score_order``             the dialect-portable "NULL scores last" ordering (D-86).
    ``score_document``          the tailoring flow (Task 9.3): one résumé document against one
                                posting (``posting_for_job`` or, for pasted text,
                                ``posting_from_text``), on demand and never banked; with
                                ``requirement_coverage`` and ``reason_sentences`` for display.
    ``engine_ready``            True once this process has built the engine. Request paths read
                                banked rows through ``fit_reads`` (no engine import, D-107) and
                                call ``bank_scores`` only when this is True.

The date a score is "as of" is the first day of the current UTC month (``fit_as_of``, D-87): the
parsed résumé depends on it ("Present" and the months of experience), and a daily date would
re-fingerprint, and so re-score, every user every day.

Secrets: ``FIT_TRACE_SALT`` (at least 16 bytes) is read only by the two trace writers,
``bank_scores`` and ``delete_user_traces``. Banking refuses to run without it: a score is never
persisted without its trace (compliance.md section 3).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy import func

MATCH_ENGINE_ROOT = Path(__file__).resolve().parents[2] / "match-engine"
_ENGINE_SRC = str(MATCH_ENGINE_ROOT / "src")
if _ENGINE_SRC not in sys.path:  # D-01: the hyphenated directory can never shadow `matcher`
    sys.path.insert(0, _ENGINE_SRC)

from matcher.audit import build_trace, opaque_id, trace_digest  # noqa: E402
from matcher.calibrate.percentile import PercentileCalibrator  # noqa: E402
from matcher.calibrate.reference import (  # noqa: E402
    GLOBAL_KEY,
    PanelError,
    ReferenceDistribution,
    build_reference,
    load_panel,
)
from matcher.config import EngineConfig, combined_hash, load_config  # noqa: E402
from matcher.errors import Flag  # noqa: E402
from matcher.explain.labels import flag_sentence, skill_label  # noqa: E402
from matcher.graph.loader import load_graph, load_resolver, load_resolver_settings  # noqa: E402
from matcher.graph.model import SkillGraph  # noqa: E402
from matcher.models import (  # noqa: E402
    JobPosting,
    Location,
    MatchResult,
    Outcome,
    Polarity,
    Resume,
    ScoreOptions,
    SkillKind,
    WorkAuth,
    canonical_json,
)
from matcher.normalize.resolver import Resolver  # noqa: E402
from matcher.parse.config import ParseConfig, load_parse_config  # noqa: E402
from matcher.parse.posting import PostingMeta, parse_posting  # noqa: E402
from matcher.parse.resume import ParsedResume, ResumeResources, resume_from_document  # noqa: E402
from matcher.parse.taxonomy_extras import MosIndex, TitleIndex, load_mos_index, load_title_index  # noqa: E402
from matcher.score.coverage import REQ_MATCH, REQ_MISS, REQ_PARTIAL  # noqa: E402
from matcher.score.engine import review_result, score  # noqa: E402

from backend.services import profile_facts  # noqa: E402
from backend.services.fit_reads import (  # noqa: E402  (the engine-free half: D-107)
    DEFAULT_SETTINGS_LOCATION,
    PROFILE_FIELD_LIMIT,
    clearance_answer as _clearance_answer,
    digest as _digest,
    document_of as _document,
    engine_ready,
    input_key,
    profile_fields,
    remember_fingerprint,
    score_order,
    settings_for as _settings_for,
)

logger = logging.getLogger(__name__)

TAXONOMY_DIR = MATCH_ENGINE_ROOT / "data" / "taxonomy"
CALIBRATION_DIR = MATCH_ENGINE_ROOT / "data" / "calibration"

#: The "Security clearance" answer that means an active clearance of unknown country and level.
ACTIVE_CLEARANCE_ANSWER = "active clearance"
#: Countries whose authorisation Tailrd asks about explicitly (plus the residence).
_COUNTRIES_OF_INTEREST = ("US", "CA")

_TRACE_SALT_ENV = "FIT_TRACE_SALT"
_MIN_SALT_BYTES = 16
_FINGERPRINT_LEN = 64  # job_match_scores.resume_fingerprint is VARCHAR(64)
_BULK_OPTIONS = ScoreOptions(with_explanation=False)
_STRICT_OPTIONS = ScoreOptions(strict=True, with_explanation=False)
_DETAIL_OPTIONS = ScoreOptions(with_explanation=True, with_interval=True, with_counterfactual=True)
_POSTING_BATCH = 200
_LIVE_PREFIX = "live-"


class FitEngineError(RuntimeError):
    """The engine's committed data is missing or does not match what it pins (fails loudly)."""


class FitTraceSaltError(RuntimeError):
    """FIT_TRACE_SALT is missing or shorter than 16 bytes: no score may be banked without a trace."""


# ─── The engine ──────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class FitEngine:
    """Everything the engine needs, built once per process (``get_engine``)."""

    config: EngineConfig
    graph: SkillGraph
    resolver: Resolver
    parse_config: ParseConfig
    titles: TitleIndex
    mos: MosIndex
    resume_resources: ResumeResources
    panel: tuple[ReferenceDistribution, ...]
    panel_version: str
    combined_hash: str
    build_seconds: float

    @property
    def major(self) -> str:
        return self.config.engine_version.split(".")[0]

    @property
    def min_reference_n(self) -> int:
        return self.config.scoring.calibration.min_reference_n

    def posting_version(self, as_of: dt.date) -> str:
        """The posting cache version (D-84): the engine code, the parse tables, the config and
        graph, and the year (a requirement's "years since release" clamp reads it)."""
        material = "|".join((self.config.engine_version, self.parse_config.version, self.combined_hash,
                             str(as_of.year)))
        return "pv1-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


_ENGINE: Optional[FitEngine] = None
_ENGINE_LOCK = threading.Lock()
# A build failure is remembered for this long, so one sweep (every chunk, the calibration refresh
# and the drift report) does not pay a full rebuild per call when an artifact is broken.
BUILD_FAILURE_TTL_S = 300.0
_BUILD_FAILURE: Optional[tuple[float, FitEngineError]] = None
_monotonic = time.monotonic


def get_engine() -> FitEngine:
    """The process-wide engine, built on first use (thread-safe). Cold build: see build_engine.
    A failed build raises FitEngineError, and the same error again for BUILD_FAILURE_TTL_S."""
    global _ENGINE, _BUILD_FAILURE
    engine = _ENGINE
    if engine is not None:
        return engine
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        failure = _BUILD_FAILURE
        if failure is not None and _monotonic() - failure[0] < BUILD_FAILURE_TTL_S:
            raise failure[1]
        try:
            _ENGINE = build_engine()
        except FitEngineError as exc:
            _BUILD_FAILURE = (_monotonic(), exc)
            raise
        _BUILD_FAILURE = None
        return _ENGINE


def build_engine(root: Path = MATCH_ENGINE_ROOT) -> FitEngine:
    """Load and verify every engine artifact. Raises FitEngineError (never degrades) when one is
    missing, unreadable or does not match its MANIFEST: a half-built engine would score with the
    wrong data. Every loader error (ConfigError, ArtifactError, PanelError, OSError, ValueError) is
    converted, so a caller sees one error type and the sweep reports engine_unavailable (I-1)."""
    try:
        return _build_engine(root)
    except FitEngineError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:  # ConfigError/ArtifactError/PanelError are ValueErrors
        raise FitEngineError(f"{type(exc).__name__}: {exc}") from exc


def _build_engine(root: Path) -> FitEngine:
    started = time.perf_counter()
    taxonomy = root / "data" / "taxonomy"
    config = load_config(root / "config")
    parse_config = load_parse_config(root / "config" / "parse")
    graph = load_graph(taxonomy)
    resolver = load_resolver(taxonomy, config)
    titles = load_title_index(taxonomy, parse_config=parse_config)
    mos = load_mos_index(taxonomy)
    settings = load_resolver_settings(taxonomy)
    resources = ResumeResources(
        titles=titles,
        mos=mos,
        in_progress=dict(settings.get("in_progress", {})),
        credentials=frozenset(node_id for node_id, node in graph.nodes.items() if node.kind is SkillKind.CREDENTIAL),
        parse_config=parse_config,
    )
    panel, panel_version = load_verified_panel(root / "data" / "calibration")
    elapsed = time.perf_counter() - started
    logger.info("fit_engine: engine %s built in %.2fs", config.engine_version, elapsed)
    return FitEngine(config=config, graph=graph, resolver=resolver, parse_config=parse_config, titles=titles,
                     mos=mos, resume_resources=resources, panel=panel, panel_version=panel_version,
                     combined_hash=combined_hash(config, graph.version), build_seconds=elapsed)


def load_verified_panel(calibration_dir: Path) -> tuple[tuple[ReferenceDistribution, ...], str]:
    """The committed reference panel, after checking its bytes and version against MANIFEST.json."""
    try:
        manifest = json.loads((calibration_dir / "MANIFEST.json").read_text(encoding="utf-8"))
        pinned = manifest["panel"]
        path = calibration_dir / pinned["file"]
        expected_sha, expected_version = pinned["sha256"], pinned["version"]
        data = path.read_bytes()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FitEngineError(f"calibration MANIFEST in {calibration_dir} is unreadable: {exc!r}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha:
        raise FitEngineError(f"panel sha256 {actual} does not match MANIFEST {expected_sha}")
    try:
        panel = load_panel(path)
    except PanelError as exc:
        raise FitEngineError(str(exc)) from exc
    versions = {ref.version for ref in panel}
    if versions != {expected_version}:
        raise FitEngineError(f"panel version {sorted(versions)} does not match MANIFEST {expected_version!r}")
    return panel, expected_version


def fit_as_of(today: Optional[dt.date] = None) -> dt.date:
    """The date every score is as of: the first day of the current UTC month (D-87)."""
    day = today or dt.datetime.now(dt.timezone.utc).date()
    return day.replace(day=1)


# ─── Résumé (D-80, D-81) ─────────────────────────────────────────────────────────────────────

def _profile_location(fields: Mapping[str, str]) -> Optional[Location]:
    """The candidate's place from the profile facts: a country only when the profile settles it
    (high confidence), the region and city when stated. None when nothing is known (D-64: unknown
    is never a mismatch)."""
    facts = profile_facts.location_facts(fields)
    country = facts.country.value.code if facts.country is not None and facts.country.confidence == "high" else None
    region = facts.region.value.code if facts.region is not None else None
    if region is not None and country is None:
        country = facts.region.value.country
    city = facts.city.value if facts.city is not None else None
    if not (country or region or city):
        return None
    try:
        return Location(country=country, region=region, city=city[:64] if city else None)
    except ValueError:  # an unusual spelling the model refuses: drop the city, keep the rest
        try:
            return Location(country=country, region=region)
        except ValueError:
            return None


def _profile_work_auth(fields: Mapping[str, str], clearance: str, residence: Optional[str],
                       config: EngineConfig) -> Optional[WorkAuth]:
    """The work-authorisation facts (DECISIONS D-61 inputs) from the saved answers.

    authorised countries: those where ``authorized_in`` says True (US, CA, the residence and any
    country the answer names). needs_sponsorship: from ``needs_sponsorship_in`` over the countries
    of interest the person is not known to be authorised in; True when any says True, False when
    all say False, else None. Citizenships are never inferred here (a stated citizenship that
    misses a posting's country could fail a gate, and the profile cannot say "only"). An answer
    of "Active clearance" is the configured unknown-level token (D-63). Nothing stated: None.
    """
    auth = profile_facts.work_auth_facts(fields)
    candidates = list(dict.fromkeys([*_COUNTRIES_OF_INTEREST, *([residence] if residence else []),
                                     *auth.by_country.keys()]))
    authorized = []
    for code in candidates:
        fact = profile_facts.authorized_in(auth, code, residence)
        if fact is not None and fact.value is True:
            authorized.append(code)
    interest = [code for code in dict.fromkeys([*_COUNTRIES_OF_INTEREST, *([residence] if residence else [])])
                if code not in authorized]
    needs: Optional[bool] = None
    answers = [profile_facts.needs_sponsorship_in(auth, code, residence) for code in interest]
    known = [fact.value for fact in answers if fact is not None]
    if any(value is True for value in known):
        needs = True
    elif known and len(known) == len(answers):
        needs = False
    clearances: tuple[str, ...] = ()
    if clearance.strip().lower() == ACTIVE_CLEARANCE_ANSWER:
        clearances = (config.gates.security_clearance.unknown_level_token,)
    if not authorized and needs is None and not clearances:
        return None
    return WorkAuth(authorized_countries=tuple(sorted(authorized)), needs_sponsorship=needs, clearances=clearances)


def _merged_work_auth(from_profile: Optional[WorkAuth], from_resume: Optional[WorkAuth]) -> Optional[WorkAuth]:
    """The profile's answers first; the résumé's own statements fill what the profile leaves out."""
    if from_profile is None:
        return from_resume
    if from_resume is None:
        return from_profile
    return WorkAuth(
        authorized_countries=tuple(sorted({*from_profile.authorized_countries, *from_resume.authorized_countries})),
        needs_sponsorship=(from_profile.needs_sponsorship if from_profile.needs_sponsorship is not None
                           else from_resume.needs_sponsorship),
        citizenships=from_resume.citizenships,
        clearances=tuple(dict.fromkeys([*from_profile.clearances, *from_resume.clearances])),
    )


_PARSE_CACHE: "OrderedDict[str, ParsedResume]" = OrderedDict()
_PARSE_CACHE_SIZE = 64
_PARSE_CACHE_LOCK = threading.Lock()


def resume_for_profile(profile: Any, settings: Any, *, engine: Optional[FitEngine] = None,
                       as_of: Optional[dt.date] = None, text_fallback: bool = True) -> ParsedResume:
    """The engine résumé for a stored ``ResumeProfileDB`` and the user's ``UserSettings`` (or None).

    D-80: the stored document when it has experience / education / projects / skills content,
    else ``raw_text`` through the engine's own parser. Work authorisation and location come from
    the saved answers (profile_facts, the extension's reading); the résumé's own statements fill
    what the answers leave unknown. Never raises: an empty profile is a ParsedResume with
    reasons (NOT_A_RESUME) and no résumé. Results are memoised per input (a feed request reads
    the fingerprint on every page).

    D-152 (amends D-80): a stored profile that was never edited after upload
    (``content_updated_at`` unset) and whose document roles all lost their dates is scored from
    ``raw_text`` when the text parse has a dated role and covers at least 80 % of the document's
    skills. ``text_fallback=False`` (the tailoring flow, ``resume_for_document``) never does.
    """
    engine = engine or get_engine()
    day = as_of or fit_as_of()
    document = _document(profile)
    raw_text = getattr(profile, "raw_text", None)
    fields = profile_fields(profile, settings)
    clearance = _clearance_answer(settings)
    allow_text = text_fallback and getattr(profile, "content_updated_at", None) is None
    key = _digest([engine.combined_hash, engine.parse_config.version, day.isoformat(), document, raw_text or "",
                   fields, clearance, allow_text])
    with _PARSE_CACHE_LOCK:
        cached = _PARSE_CACHE.get(key)
        if cached is not None:
            _PARSE_CACHE.move_to_end(key)
            return cached
    parsed = _parse_resume(document, raw_text, fields, clearance, engine, day, text_fallback=allow_text)
    with _PARSE_CACHE_LOCK:
        _PARSE_CACHE[key] = parsed
        while len(_PARSE_CACHE) > _PARSE_CACHE_SIZE:
            _PARSE_CACHE.popitem(last=False)
    return parsed


_ROLE_DATE_KEYS = ("start_date", "end_date")
_ROLE_CONTENT_KEYS = ("title", "company")


def _document_roles_undated(document: Mapping[str, Any]) -> bool:
    """True when the stored document has at least one experience role and no role has a start or
    an end date: the shape of a résumé extracted before 2026-07-09 (commit 1062972), whose dates
    were dropped at ingest although its raw text holds them (D-152)."""
    roles = [item for item in (document.get("experience") or ()) if isinstance(item, Mapping) and (
        any(isinstance(item.get(key), str) and item.get(key).strip() for key in _ROLE_CONTENT_KEYS)
        or any(isinstance(b, str) and b.strip() for b in (item.get("bullets") or ())))]
    if not roles:
        return False
    return not any(isinstance(item.get(key), str) and item.get(key).strip()
                   for item in roles for key in _ROLE_DATE_KEYS)


def _skill_ids(parsed: ParsedResume) -> frozenset[str]:
    """Every affirmed resolved skill id of a parsed résumé (roles, projects, skills, credentials)."""
    resume = parsed.resume
    if resume is None:
        return frozenset()
    refs = [*resume.skills_blob, *resume.certifications,
            *(ref for role in resume.roles for ref in role.skills),
            *(ref for project in resume.projects for ref in project.skills)]
    return frozenset(ref.canonical_id for ref in refs if ref.polarity is Polarity.AFFIRMED)


#: D-152 guard: the text parse must name at least this share of the document parse's skills.
_TEXT_SKILL_COVERAGE = 0.8


def _text_covers_document(document_parse: ParsedResume, text_parse: ParsedResume) -> bool:
    """True when the raw-text parse is not thinner than the document: its resolved skills cover at
    least 80 % of the document parse's (or the document resolves none)."""
    wanted = _skill_ids(document_parse)
    if not wanted:
        return True
    return len(wanted & _skill_ids(text_parse)) >= _TEXT_SKILL_COVERAGE * len(wanted)


def _parse_resume(document: dict[str, Any], raw_text: Optional[str], fields: Mapping[str, str], clearance: str,
                  engine: FitEngine, as_of: dt.date, *, text_fallback: bool = True) -> ParsedResume:
    parsed = resume_from_document(document, as_of=as_of, resolver=engine.resolver, config=engine.config,
                                  parse_config=engine.parse_config,
                                  raw_text=raw_text if isinstance(raw_text, str) else None,
                                  resources=engine.resume_resources)
    if (text_fallback and _document_roles_undated(document) and isinstance(raw_text, str)
            and raw_text.strip()):
        # D-80 amended (D-152): a never-edited legacy document that lost every date (pre-2026-07-09
        # extraction) is scored from the raw text instead, when the engine's text path finds a dated
        # role there and the text is not thinner than the document (skill coverage). The whole parse
        # is swapped, never dates copied across, so roles cannot be mismatched.
        from_text = resume_from_document({}, as_of=as_of, resolver=engine.resolver, config=engine.config,
                                         parse_config=engine.parse_config, raw_text=raw_text,
                                         resources=engine.resume_resources)
        if (from_text.resume is not None and any(role.dated for role in from_text.resume.roles)
                and _text_covers_document(parsed, from_text)):
            parsed = from_text
    if parsed.resume is None:
        return parsed
    place = _profile_location(fields)
    residence = profile_facts.residence_of(profile_facts.location_facts(fields))
    work_auth = _merged_work_auth(_profile_work_auth(fields, clearance, residence, engine.config),
                                  parsed.resume.work_auth)
    locations = (place,) if place is not None else parsed.resume.locations
    data = parsed.resume.model_dump()
    data["work_auth"] = None if work_auth is None else work_auth.model_dump()
    data["locations"] = [loc.model_dump() for loc in locations]
    try:
        resume = Resume.model_validate(data)
    except ValueError:
        logger.warning("fit_engine: profile facts refused by the Resume model; keeping the parsed facts")
        return parsed
    return parsed.model_copy(update={"resume": resume})


# ─── Fingerprints (D-82) ─────────────────────────────────────────────────────────────────────


def fingerprint_for(parsed: ParsedResume, *, engine: Optional[FitEngine] = None,
                    material: Optional[str] = None) -> str:
    """"fit<major>:" + sha256(canonical Resume JSON + combined hash), cut to 64 characters.

    A résumé that could not be parsed has no canonical JSON: its fingerprint hashes its reasons
    and ``material`` (a digest of the stored input), so a NEEDS_REVIEW row is current until the
    input or the config changes, and is not rescored on every sweep."""
    engine = engine or get_engine()
    if parsed.resume is not None:
        body = canonical_json(parsed.resume)
    else:
        body = "review|" + ",".join(flag.value for flag in parsed.reasons) + "|" + (material or "")
    prefix = f"fit{engine.major}:"
    digest = hashlib.sha256((body + engine.combined_hash).encode("utf-8")).hexdigest()
    return (prefix + digest)[:_FINGERPRINT_LEN]


def _input_material(profile: Any, settings: Any) -> str:
    return input_key(profile, settings)


def _fingerprint(profile: Any, settings: Any, parsed: ParsedResume, engine: FitEngine) -> str:
    material = None if parsed.resume is not None else _input_material(profile, settings)
    return fingerprint_for(parsed, engine=engine, material=material)


def current_fingerprints(profile: Any, settings: Any = None, *, engine: Optional[FitEngine] = None) -> list[str]:
    """The ONE fingerprint whose stored score is current for this résumé and settings. Old AI
    (sha256) and "local1:" rows are never in it, so they are stale everywhere (D-82)."""
    engine = engine or get_engine()
    parsed = resume_for_profile(profile, settings, engine=engine)
    return [_fingerprint(profile, settings, parsed, engine)]


# ─── Postings (D-84) ─────────────────────────────────────────────────────────────────────────


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _country_code(name: Any, fallback: str) -> Optional[str]:
    if isinstance(name, str) and name.strip():
        found = profile_facts.countries_in(name)
        if len(found) == 1:
            return found[0].code
        if len(name.strip()) == 2 and name.strip().isalpha():
            return name.strip().upper()
    return fallback or None


def _job_locations(locations_json: Any, country: Any) -> tuple[Location, ...]:
    """``locations_json`` items ({city, region, region_name, country (full name)}) as engine places.
    "Remote" is no city. A row without items falls back to its country column (US / CA)."""
    fallback = country.strip().upper() if isinstance(country, str) and len(country.strip()) == 2 else ""
    places: list[Location] = []
    for item in locations_json if isinstance(locations_json, list) else ():
        if not isinstance(item, dict):
            continue
        city = item.get("city") if isinstance(item.get("city"), str) else ""
        if city.strip().lower() == "remote":
            city = ""
        region = item.get("region") if isinstance(item.get("region"), str) else ""
        try:
            place = Location(country=_country_code(item.get("country"), fallback),
                             region=region.strip().upper()[:20] or None, city=city.strip()[:64] or None)
        except ValueError:
            continue
        if (place.country or place.region or place.city) and place not in places:
            places.append(place)
        if len(places) >= 50:
            break
    if not places and fallback:
        places.append(Location(country=fallback))
    return tuple(places)


def posting_meta(job: Any) -> PostingMeta:
    """``PostingMeta`` from a job's columns (any object or row with these attribute names)."""
    company_description = _text(getattr(job, "company_description", ""), 20000)
    if company_description.lstrip().startswith("{"):  # a JSON blob some sources stored here
        company_description = ""
    family = getattr(job, "role_category", None)
    return PostingMeta(
        company=_text(getattr(job, "company", ""), 2000),
        company_description=company_description,
        locations=_job_locations(getattr(job, "locations_json", None), getattr(job, "country", None)),
        work_type=_text(getattr(job, "work_type", ""), 50),
        employment_type=_text(getattr(job, "employment_type", ""), 200),
        experience_level=_text(getattr(job, "experience_level", ""), 200),
        job_family=str(family)[:200] if isinstance(family, str) and family.strip() else None,
    )


def _parse_job(job: Any, engine: FitEngine, as_of: dt.date) -> tuple[JobPosting, bool]:
    """(posting, cacheable). A parser fault is an INTERNAL_ERROR review, never cached."""
    try:
        posting = parse_posting(getattr(job, "title", "") or "", getattr(job, "description", "") or "",
                                meta=posting_meta(job), as_of=as_of, resolver=engine.resolver, graph=engine.graph,
                                config=engine.config, parse_config=engine.parse_config, titles=engine.titles)
        return posting, True
    except Exception as exc:  # the parser is meant to be total; a fault must not stop a sweep
        logger.error("fit_engine: posting parse failed for job %s: %s", getattr(job, "id", "?"),
                     type(exc).__name__)
        return JobPosting(flags=(Flag.INTERNAL_ERROR,)), False


def _cache_entry(posting: JobPosting, version: str, description_length: int) -> dict[str, Any]:
    return {"v": version, "dl": description_length, "posting": json.loads(canonical_json(posting))}


def _cached(terms: Any, version: str, description_length: int) -> Optional[JobPosting]:
    if not (isinstance(terms, dict) and terms.get("v") == version and terms.get("dl") == description_length):
        return None
    try:
        return JobPosting.model_validate(terms.get("posting"))
    except ValueError:
        return None


def posting_for_job(job: Any, *, engine: Optional[FitEngine] = None,
                    as_of: Optional[dt.date] = None) -> JobPosting:
    """One ORM ``ScrapedJob``'s posting: its cache when current, else parsed and written back to
    ``job.match_terms`` (the caller commits)."""
    engine = engine or get_engine()
    day = as_of or fit_as_of()
    version = engine.posting_version(day)
    description = getattr(job, "description", "") or ""
    hit = _cached(getattr(job, "match_terms", None), version, len(description))
    if hit is not None:
        return hit
    posting, cacheable = _parse_job(job, engine, day)
    if cacheable:
        job.match_terms = _cache_entry(posting, version, len(description))
    return posting


def posting_keys(db, job_ids: Sequence[int], *, engine: Optional[FitEngine] = None,
                 as_of: Optional[dt.date] = None) -> dict[int, str]:
    """{job_id: "<posting cache version>:<description length>"}: which posting a banked score was
    computed from (``job_match_scores.posting_key``). The length is read in SQL; no description
    is transferred. The version covers the engine, the parse tables and the config (D-89)."""
    from backend.db.models import ScrapedJob

    engine = engine or get_engine()
    version = engine.posting_version(as_of or fit_as_of())
    ids = list(dict.fromkeys(int(job_id) for job_id in job_ids))
    out: dict[int, str] = {}
    for start in range(0, len(ids), _POSTING_BATCH):
        chunk = ids[start:start + _POSTING_BATCH]
        for job_id, length in (db.query(ScrapedJob.id, func.coalesce(func.length(ScrapedJob.description), 0))
                               .filter(ScrapedJob.id.in_(chunk)).all()):
            out[job_id] = f"{version}:{int(length or 0)}"
    return out


def postings_for(db, job_ids: Sequence[int], *, engine: Optional[FitEngine] = None,
                 as_of: Optional[dt.date] = None) -> dict[int, JobPosting]:
    """Postings for these jobs. The cache check reads ``match_terms`` and ``length(description)``
    in SQL; a description is transferred only for a job that must be parsed again. Recomputed
    entries are written to ``match_terms`` and the caller's session is COMMITTED (any pending
    change the caller holds is committed with them). Unknown ids are left out."""
    from backend.db.models import ScrapedJob

    engine = engine or get_engine()
    day = as_of or fit_as_of()
    version = engine.posting_version(day)
    ids = list(dict.fromkeys(int(job_id) for job_id in job_ids))
    out: dict[int, JobPosting] = {}
    stale: list[int] = []
    for start in range(0, len(ids), _POSTING_BATCH):
        chunk = ids[start:start + _POSTING_BATCH]
        rows = (
            db.query(ScrapedJob.id, ScrapedJob.match_terms, func.coalesce(func.length(ScrapedJob.description), 0))
            .filter(ScrapedJob.id.in_(chunk))
            .all()
        )
        for job_id, terms, length in rows:
            hit = _cached(terms, version, int(length or 0))
            if hit is not None:
                out[job_id] = hit
            else:
                stale.append(job_id)
    columns = (ScrapedJob.id, ScrapedJob.title, ScrapedJob.description, ScrapedJob.company,
               ScrapedJob.company_description, ScrapedJob.locations_json, ScrapedJob.country, ScrapedJob.work_type,
               ScrapedJob.employment_type, ScrapedJob.experience_level, ScrapedJob.role_category)
    for start in range(0, len(stale), _POSTING_BATCH):
        chunk = stale[start:start + _POSTING_BATCH]
        for row in db.query(*columns).filter(ScrapedJob.id.in_(chunk)).all():
            posting, cacheable = _parse_job(row, engine, day)
            out[row.id] = posting
            if cacheable:
                entry = _cache_entry(posting, version, len(row.description or ""))
                db.query(ScrapedJob).filter(ScrapedJob.id == row.id).update(
                    {ScrapedJob.match_terms: entry}, synchronize_session=False
                )
        db.commit()
    return out


# ─── Calibration references (D-50) ───────────────────────────────────────────────────────────


def _live_version(engine: FitEngine, when: dt.datetime, groups: Mapping[tuple[str, str], Any]) -> str:
    """"live-<config hash>-<UTC timestamp>-<content digest>": the config the scores came from, a
    sortable time, and a digest that tells two refreshes with different scores apart."""
    content = _digest([[scope, key, sorted(raws), sorted(set(fps))] for (scope, key), (raws, fps) in
                       sorted(groups.items())])
    return f"{_LIVE_PREFIX}{engine.combined_hash}-{when.strftime('%Y%m%d%H%M%S')}-{content[:8]}"


_LIVE_CACHE: dict[str, Any] = {"version": None, "refs": []}
_LIVE_CACHE_LOCK = threading.Lock()


def live_references(db, *, engine: Optional[FitEngine] = None) -> list[ReferenceDistribution]:
    """The stored live references built under the current config hash (others are ignored).

    Egress: the samples (up to 5,000 floats per group) are read only when the stored version
    differs from the one this process holds; otherwise one short version query answers."""
    from backend.db.models import MatchCalibration

    engine = engine or get_engine()
    prefix = f"{_LIVE_PREFIX}{engine.combined_hash}-"
    newest = (
        db.query(MatchCalibration.version)
        .filter(MatchCalibration.version.like(prefix + "%"))
        .order_by(MatchCalibration.version.desc())
        .first()
    )
    version = newest[0] if newest else None
    with _LIVE_CACHE_LOCK:
        if version == _LIVE_CACHE["version"]:
            return list(_LIVE_CACHE["refs"])
    refs = _load_live(db, version) if version is not None else []
    with _LIVE_CACHE_LOCK:
        _LIVE_CACHE["version"], _LIVE_CACHE["refs"] = version, refs
    return list(refs)


def _load_live(db, version: str) -> list[ReferenceDistribution]:
    from backend.db.models import MatchCalibration

    out: list[ReferenceDistribution] = []
    for row in db.query(MatchCalibration).filter(MatchCalibration.version == version).all():
        try:
            out.append(ReferenceDistribution(scope=row.scope, key=row.key, n_candidates=row.n_candidates,
                                             n_pairs=row.n_pairs, samples=tuple(row.samples or ()),
                                             version=row.version))
        except ValueError:
            logger.warning("fit_engine: unusable calibration row %s/%s skipped", row.scope, row.key)
    return out


def refresh_calibration(db, *, engine: Optional[FitEngine] = None,
                        now: Optional[dt.datetime] = None) -> dict[str, Any]:
    """Rebuild the per-family and global references from banked raw scores (D-50).

    Reads every current-engine row with a raw score (the panel builder's rule: every outcome that
    produced one) on an active job. A candidate is a PERSON (D-50 note): keyed by a sha256 of the
    user id, counted and never stored, and each user contributes only the rows under their newest
    fingerprint (the one with the latest ``scored_at``), so an edited résumé neither counts twice
    nor leaves its old scores in the samples. Every group is stored; the calibrator uses one only
    from 30 candidates up, so until then the shipped panel stays in charge. Returns a summary of
    {n_candidates, n_pairs, in_charge} per family and for the global group.
    """
    from backend.db.models import JobMatchScore, MatchCalibration, ScrapedJob
    from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES

    engine = engine or get_engine()
    when = now or dt.datetime.utcnow()
    rows = (
        db.query(JobMatchScore.user_id, JobMatchScore.raw_score, JobMatchScore.resume_fingerprint,
                 JobMatchScore.scored_at, ScrapedJob.role_category, ScrapedJob.listing_status)
        .join(ScrapedJob, ScrapedJob.id == JobMatchScore.job_id)
        .filter(
            JobMatchScore.raw_score.isnot(None),
            JobMatchScore.config_hash == engine.combined_hash,
            JobMatchScore.resume_fingerprint.like(f"fit{engine.major}:%"),
        )
        .all()
    )
    newest: dict[int, tuple[dt.datetime, str]] = {}
    for user_id, _, fingerprint, scored_at, _, _ in rows:
        mark = (scored_at or dt.datetime.min, fingerprint)
        if user_id not in newest or mark > newest[user_id]:
            newest[user_id] = mark
    groups: dict[tuple[str, str], tuple[list[float], list[str]]] = defaultdict(lambda: ([], []))
    for user_id, raw, fingerprint, _, family, status in rows:
        if fingerprint != newest[user_id][1] or status in HIDDEN_LISTING_STATUSES:
            continue
        person = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()
        keys = [("global", GLOBAL_KEY)]
        if isinstance(family, str) and family.strip():
            keys.append(("family", family.strip()[:200]))
        for key in keys:
            groups[key][0].append(float(raw))
            groups[key][1].append(person)
    version = _live_version(engine, when, groups)
    references = [build_reference(scope, key, raws, fps, version)
                  for (scope, key), (raws, fps) in sorted(groups.items())]

    db.query(MatchCalibration).filter(MatchCalibration.scope.in_(("family", "global"))).delete(
        synchronize_session=False
    )
    for ref in references:
        db.add(MatchCalibration(scope=ref.scope, key=ref.key, n_candidates=ref.n_candidates, n_pairs=ref.n_pairs,
                                samples=list(ref.samples), version=ref.version, created_at=when))
    db.commit()
    summary: dict[str, Any] = {"version": version, "family": {}, "global": None}
    for ref in references:
        entry = {"n_candidates": ref.n_candidates, "n_pairs": ref.n_pairs,
                 "in_charge": ref.n_candidates >= engine.min_reference_n}
        if ref.scope == "global":
            summary["global"] = entry
        else:
            summary["family"][ref.key] = entry
    return summary


def _calibrator(db, engine: FitEngine) -> PercentileCalibrator:
    return PercentileCalibrator([*live_references(db, engine=engine), *engine.panel],
                                min_reference_n=engine.min_reference_n)


# ─── Scoring and banking ─────────────────────────────────────────────────────────────────────


def _score_pair(parsed: ParsedResume, posting: JobPosting, calibrator: PercentileCalibrator, engine: FitEngine,
                as_of: dt.date, options: ScoreOptions, job_id: Any) -> MatchResult:
    if parsed.resume is None:
        return review_result(parsed.reasons, config=engine.config, graph=engine.graph, as_of=as_of,
                             flags=parsed.flags)
    family = posting.job_family
    bound = calibrator.bind(posting_key=None, family=str(family) if family else None)
    result = score(parsed.resume, posting, as_of=as_of, config=engine.config, graph=engine.graph,
                   calibration=bound, options=options)
    if result.outcome is Outcome.NEEDS_REVIEW and Flag.INTERNAL_ERROR in result.reasons:
        # The engine is deterministic: replay once in strict mode to learn the exception class.
        try:
            score(parsed.resume, posting, as_of=as_of, config=engine.config, graph=engine.graph,
                  calibration=bound, options=_STRICT_OPTIONS)
        except Exception as exc:
            logger.error("fit_engine: INTERNAL_ERROR on job %s: %s", job_id, type(exc).__name__)
    return result


def stored_score(result: MatchResult) -> Optional[int]:
    """The integer stored in ``job_match_scores.score``: the displayed score of a SCORED result,
    else NULL. Every other outcome shows no number, so it must never be sorted, averaged or
    thresholded as one (D-86)."""
    return result.display_score if result.outcome is Outcome.SCORED else None


def _trace_salt() -> bytes:
    """FIT_TRACE_SALT as bytes. Read ONLY by the trace writers (bank_scores, delete_user_traces)."""
    salt = os.environ.get(_TRACE_SALT_ENV, "").encode("utf-8")
    if len(salt) < _MIN_SALT_BYTES:
        raise FitTraceSaltError(f"{_TRACE_SALT_ENV} must be set to at least {_MIN_SALT_BYTES} bytes")
    return salt


def subject_id(user_id: int, salt: bytes) -> str:
    return opaque_id(str(user_id), salt)


def bank_scores(db, user_id: int, profile: Any, job_ids: Sequence[int], *,
                engine: Optional[FitEngine] = None, as_of: Optional[dt.date] = None,
                postings_out: Optional[dict[int, JobPosting]] = None) -> dict[int, MatchResult]:
    """Score every job in ``job_ids`` that has no current row for this résumé and store it.

    One parsed résumé, many postings (``ScoreOptions(with_explanation=False)``). Each stored row
    carries the score (``stored_score``: NULL unless SCORED), the raw score, band, outcome,
    confidence, engine version and config hash; its trace goes to ``match_traces`` under the
    user's opaque id. A row is current only when both its fingerprint and its ``posting_key``
    (``posting_keys``: the posting cache version and the description length) match, so a
    description backfill or edit, a parse-table change and an engine fault (stored with a NULL
    key) all rescore. A stale row (an older résumé, config, posting, or an AI / local score) is
    overwritten in place. The fingerprint is remembered for these inputs (match_fingerprints,
    D-107), so a request path that never builds the engine can still tell current rows apart.
    Commits the session (the posting cache first, then the scores). Returns {job_id: MatchResult}
    for the jobs scored now. When ``postings_out`` is a dict, the posting each of those jobs was
    scored with is put in it (the shadow pass reuses them: ``shadow_scores``).
    Raises FitTraceSaltError before writing anything when FIT_TRACE_SALT is unusable.
    """
    from backend.db.models import JobMatchScore, MatchTrace

    salt = _trace_salt()
    engine = engine or get_engine()
    day = as_of or fit_as_of()
    ids = list(dict.fromkeys(int(job_id) for job_id in job_ids))
    if not ids:
        return {}
    settings = _settings_for(db, user_id)
    parsed = resume_for_profile(profile, settings, engine=engine, as_of=day)
    fingerprint = _fingerprint(profile, settings, parsed, engine)

    keys = posting_keys(db, ids, engine=engine, as_of=day)
    existing = {row.job_id: row for row in
                db.query(JobMatchScore).filter(JobMatchScore.user_id == user_id, JobMatchScore.job_id.in_(ids)).all()}
    todo = [job_id for job_id in ids if job_id in keys and not (
        job_id in existing
        and existing[job_id].resume_fingerprint == fingerprint
        and existing[job_id].posting_key == keys[job_id]
    )]
    key = input_key(profile, settings)
    if not todo:
        remember_fingerprint(db, user_id, key, fingerprint, day)
        if db.new or db.dirty:
            db.commit()
        return {}
    postings = postings_for(db, todo, engine=engine, as_of=day)
    calibrator = _calibrator(db, engine)
    subject = subject_id(user_id, salt)
    traces = {row.job_id: row for row in
              db.query(MatchTrace).filter(MatchTrace.subject_id == subject, MatchTrace.job_id.in_(todo)).all()}
    now = dt.datetime.utcnow()
    results: dict[int, MatchResult] = {}
    for job_id in todo:
        posting = postings.get(job_id)
        if posting is None:  # the job no longer exists
            continue
        result = _score_pair(parsed, posting, calibrator, engine, day, _BULK_OPTIONS, job_id)
        results[job_id] = result
        if postings_out is not None:
            postings_out[job_id] = posting
        values = {
            "score": stored_score(result),
            "raw_score": result.raw_score,
            "band": result.band.value if result.band is not None else None,
            "outcome": result.outcome.value,
            "confidence": result.confidence.value if result.confidence is not None else None,
            "engine_version": result.engine_version[:20],
            "config_hash": result.config_hash[:20],
            "resume_fingerprint": fingerprint,
            # NULL for an engine fault, so the row is never current and the next sweep retries it.
            "posting_key": None if Flag.INTERNAL_ERROR in result.reasons else keys[job_id],
            "scored_at": now,
        }
        row = existing.get(job_id)
        if row is None:
            db.add(JobMatchScore(user_id=user_id, job_id=job_id, **values))
        else:
            for name, value in values.items():
                setattr(row, name, value)
        trace = build_trace(result)
        trace_values = {"trace": trace, "trace_digest": trace_digest(trace),
                        "engine_version": result.engine_version[:20], "config_hash": result.config_hash[:20],
                        "scored_at": now}
        trace_row = traces.get(job_id)
        if trace_row is None:
            db.add(MatchTrace(subject_id=subject, job_id=job_id, **trace_values))
        else:
            for name, value in trace_values.items():
                setattr(trace_row, name, value)
    remember_fingerprint(db, user_id, key, fingerprint, day)
    db.commit()
    return results


# ─── Shadow scoring (EVAL section 6, D-150) ─────────────────────────────────────────────────

#: Names a candidate config directory (the layout of match-engine/config). When set, the cron
#: sweep scores every chunk a second time with it and reports the difference; nothing of the
#: shadow pass is ever persisted.
SHADOW_CONFIG_ENV = "FIT_SHADOW_CONFIG"


def shadow_config_dir() -> Optional[Path]:
    """The FIT_SHADOW_CONFIG directory, or None when the variable is unset or blank."""
    value = os.environ.get(SHADOW_CONFIG_ENV, "").strip()
    return Path(value) if value else None


def build_shadow_engine(config_dir: Path, *, live: Optional[FitEngine] = None) -> FitEngine:
    """The live engine with its ``EngineConfig`` (weights, gates, bands, verbs, negation, scoring)
    read from ``config_dir`` instead, and a resolver built for that config.

    Everything else is the live engine's: the skill graph and taxonomy, the parse tables, the
    title and MOS indexes and the calibration panel (D-150). Raises FitEngineError, never
    degrades, when the directory or any of its files is missing or invalid."""
    live = live or get_engine()
    started = time.perf_counter()
    root = Path(config_dir)
    if not root.is_dir():
        raise FitEngineError(f"{SHADOW_CONFIG_ENV} is not a directory: {root.name or root}")
    try:
        config = load_config(root)
        resolver = load_resolver(TAXONOMY_DIR, config)
    except Exception as exc:  # ConfigError, ArtifactError, OSError: the shadow is simply unusable
        raise FitEngineError(f"{type(exc).__name__}: {exc}") from exc
    elapsed = time.perf_counter() - started
    logger.info("fit_engine: shadow config %s built in %.2fs", config.content_hash, elapsed)
    return dataclasses.replace(live, config=config, resolver=resolver,
                               combined_hash=combined_hash(config, live.graph.version), build_seconds=elapsed)


def shadow_scores(db, user_id: int, profile: Any, postings: Mapping[int, JobPosting], *, shadow: FitEngine,
                  engine: Optional[FitEngine] = None, as_of: Optional[dt.date] = None) -> dict[int, MatchResult]:
    """Score ``postings`` (the ones the live pass just used, ``bank_scores(postings_out=...)``)
    against this résumé with the shadow engine, in memory. Writes nothing: no score row, no
    trace, no posting cache, no fingerprint (it only reads the user's settings and the live
    calibration references, so the shadow and live display scores share one calibrator and their
    difference is the config's alone). Postings are the live parse; the résumé is parsed again
    with the shadow config (D-150)."""
    engine = engine or get_engine()
    day = as_of or fit_as_of()
    settings = _settings_for(db, user_id)
    parsed = resume_for_profile(profile, settings, engine=shadow, as_of=day)
    calibrator = _calibrator(db, engine)
    return {job_id: _score_pair(parsed, posting, calibrator, shadow, day, _BULK_OPTIONS, job_id)
            for job_id, posting in postings.items()}


def delete_user_traces(db, user_id: int) -> int:
    """Delete a user's traces (account deletion): the subject id is recomputed from the salt,
    since traces never hold the user id. Returns how many rows were deleted."""
    from backend.db.models import MatchTrace

    subject = subject_id(user_id, _trace_salt())
    count = db.query(MatchTrace).filter(MatchTrace.subject_id == subject).delete(synchronize_session=False)
    db.commit()
    return count


# ─── The detail view ─────────────────────────────────────────────────────────────────────────


# ─── Tailoring (Task 9.3, D-83, D-123) ───────────────────────────────────────────────────────
#
# The tailoring flow scores a résumé DOCUMENT (the original, then the rewrite) against one parsed
# posting, on demand. Nothing is banked, so no trace is written and FIT_TRACE_SALT is not read.

_TAILOR_OPTIONS = ScoreOptions(with_explanation=True)
_TAILOR_SUGGEST_OPTIONS = ScoreOptions(with_explanation=True, with_counterfactual=True)
_COVERAGE_DIMENSIONS = ("required_skills", "preferred_skills")

#: The longest pasted description parsed (D-123): parse cost grows with length.
MAX_PASTED_DESCRIPTION = 20_000
#: At most this many missing requirements are suggested for a one-click rewrite (D-123).
MAX_SUGGESTED = 10
_TEXT_POSTINGS: "OrderedDict[str, JobPosting]" = OrderedDict()
_TEXT_POSTING_CACHE_SIZE = 64
_TEXT_POSTINGS_LOCK = threading.Lock()


def reset_text_postings() -> None:
    """Drop every cached pasted posting (tests, and anything that needs a cold parse)."""
    with _TEXT_POSTINGS_LOCK:
        _TEXT_POSTINGS.clear()


def posting_from_text(title: str, company: str, description: str, *, engine: Optional[FitEngine] = None,
                      as_of: Optional[dt.date] = None) -> JobPosting:
    """A posting given only as pasted text (the extension, on a live application page: no job row).

    Parsed by the engine directly, with the company as its only metadata. Memoised per input
    (title, company, description, engine and parse version, month): one tailoring journey asks for
    the same posting two or three times in a minute. A parser fault is an INTERNAL_ERROR review,
    never cached."""
    engine = engine or get_engine()
    day = as_of or fit_as_of()
    # The routes refuse longer input (schemas/tailor.py); this cut only bounds any other caller.
    title, company = _text(title, 2000), _text(company, 2000)
    description = (description or "")[:MAX_PASTED_DESCRIPTION]
    key = _digest([engine.posting_version(day), title, company, description])
    with _TEXT_POSTINGS_LOCK:
        hit = _TEXT_POSTINGS.get(key)
        if hit is not None:
            _TEXT_POSTINGS.move_to_end(key)
            return hit
    posting, cacheable = _parse_job(SimpleNamespace(title=title, description=description, company=company),
                                    engine, day)
    if cacheable:
        with _TEXT_POSTINGS_LOCK:
            _TEXT_POSTINGS[key] = posting
            while len(_TEXT_POSTINGS) > _TEXT_POSTING_CACHE_SIZE:
                _TEXT_POSTINGS.popitem(last=False)
    return posting


@dataclasses.dataclass(frozen=True)
class _DocumentProfile:
    """A résumé document in the stored-profile shape ``resume_for_profile`` reads (D-80)."""

    location: Any = None
    summary: Any = None
    experience: Any = None
    education: Any = None
    projects: Any = None
    skills: Any = None
    technologies: Any = None
    custom_sections: Any = None
    raw_text: Optional[str] = None


def _document_profile(document: Mapping[str, Any], raw_text: Optional[str]) -> _DocumentProfile:
    names = [field.name for field in dataclasses.fields(_DocumentProfile) if field.name != "raw_text"]
    return _DocumentProfile(**{name: document.get(name) for name in names}, raw_text=raw_text)


def resume_for_document(document: Mapping[str, Any], settings: Any, *, raw_text: Optional[str] = None,
                        engine: Optional[FitEngine] = None, as_of: Optional[dt.date] = None) -> ParsedResume:
    """The engine résumé for a document in the stored-profile shape (``location``, ``summary``,
    ``experience``, ``education``, ``projects``, ``skills``, ``technologies``, ``custom_sections``;
    see ``resume_document.document_to_record``), read exactly as ``resume_for_profile`` reads a
    stored profile: the same résumé the feed scores when the document is the profile's own, except
    that the D-152 raw-text date fallback never applies: the tailoring flow scores the documents
    themselves (an original and its rewrite), so ``raw_text`` is read only when a document has no
    content (D-80)."""
    return resume_for_profile(_document_profile(document, raw_text), settings, engine=engine, as_of=as_of,
                              text_fallback=False)


def score_document(db, user_id: Optional[int], document: Mapping[str, Any], posting: JobPosting, *,
                   raw_text: Optional[str] = None, with_suggestions: bool = False,
                   engine: Optional[FitEngine] = None, as_of: Optional[dt.date] = None) -> MatchResult:
    """One résumé document against one posting, with its explanation; never banked (no trace).

    The user's saved answers (work authorisation, location) are read as the feed reads them, and
    the calibration is the feed's (live references, then the panel), so the tailoring "before"
    of a stored résumé is the number its job card shows. ``with_suggestions`` adds the engine's
    counterfactual (``explanation.to_improve``), which costs extra scoring runs."""
    engine = engine or get_engine()
    day = as_of or fit_as_of()
    settings = _settings_for(db, user_id) if db is not None and user_id is not None else None
    parsed = resume_for_document(document, settings, raw_text=raw_text, engine=engine, as_of=day)
    calibrator = (_calibrator(db, engine) if db is not None
                  else PercentileCalibrator(list(engine.panel), min_reference_n=engine.min_reference_n))
    options = _TAILOR_SUGGEST_OPTIONS if with_suggestions else _TAILOR_OPTIONS
    return _score_pair(parsed, posting, calibrator, engine, day, options, "tailor")


@dataclasses.dataclass(frozen=True)
class RequirementCoverage:
    """The skill requirements of a result: display labels, and the engine's coverage as 0-100."""

    matched: tuple[str, ...]
    missing: tuple[str, ...]
    percent: Optional[int]
    suggested: tuple[str, ...] = ()


def _requirement_label(graph: SkillGraph, fact: Any) -> str:
    """One requirement group as a person reads it: its best member, "A or B" for an OR-group."""
    raw = fact.detail.get("members")
    members = [part for part in raw.split(",") if part] if isinstance(raw, str) else []
    if len(members) > 1:
        return " or ".join(skill_label(graph, member) for member in members)
    return skill_label(graph, fact.subject)


def requirement_coverage(result: MatchResult, *, engine: Optional[FitEngine] = None) -> RequirementCoverage:
    """The required and preferred skill requirements of ``result`` (required first, each in the
    engine's group order). A requirement the résumé meets (match quality at least 0.5, the
    engine's REQ_MATCH) is matched; a partial or absent one is missing. ``percent`` is the
    engine's own coverage over both: sum(idf x weight x m) / sum(idf x weight), as 0-100; None
    when no requirement carries weight (a review or a failed gate has no dimensions).
    ``suggested`` is what a one-click rewrite weaves in by default: the REQUIRED groups at
    REQ_MISS, by best member, at most ``MAX_SUGGESTED`` (partial matches and preferred skills
    stay chips only)."""
    engine = engine or get_engine()
    matched: list[str] = []
    missing: list[str] = []
    suggested: list[str] = []
    numerator = denominator = 0.0
    for name in _COVERAGE_DIMENSIONS:
        dimension = result.dimensions.get(name)
        for fact in dimension.facts if dimension is not None else ():
            if fact.code not in (REQ_MATCH, REQ_PARTIAL, REQ_MISS):
                continue  # CRITICAL_MISS repeats a group already listed
            label = _requirement_label(engine.graph, fact)
            target = matched if fact.code == REQ_MATCH else missing
            if label not in matched and label not in missing:
                target.append(label)
            # What a one-click rewrite is asked to weave in: a REQUIRED skill the résumé does not
            # show at all, by its best member (an OR-group's "A or B" is not a term to write).
            best = skill_label(engine.graph, fact.subject)
            if (name == "required_skills" and fact.code == REQ_MISS and best not in suggested
                    and len(suggested) < MAX_SUGGESTED):
                suggested.append(best)
            weight = float(fact.detail.get("idf") or 0.0) * float(fact.detail.get("weight_multiplier") or 0.0)
            numerator += weight * float(fact.value or 0.0)
            denominator += weight
    percent = max(0, min(100, round(100 * numerator / denominator))) if denominator > 0 else None
    return RequirementCoverage(matched=tuple(matched), missing=tuple(missing), percent=percent,
                               suggested=tuple(suggested))


def reason_sentences(result: MatchResult) -> list[str]:
    """The candidate-facing sentence of each of ``result``'s reasons (why it is not SCORED)."""
    return list(dict.fromkeys(flag_sentence(flag) for flag in result.reasons))


def commit_posting_cache(db, job: Any) -> None:
    """Persist a posting cache entry ``posting_for_job`` just wrote to an ORM job."""
    try:
        dirty = job in db and db.is_modified(job)
    except Exception:  # not an ORM instance: nothing was written
        return
    if dirty:
        db.commit()


def explain_job(db, user: Any, job: Any, *, engine: Optional[FitEngine] = None,
                as_of: Optional[dt.date] = None) -> Optional[dict[str, Any]]:
    """One full result for the job detail view: explanation, interval and counterfactual, as JSON.

    ``user`` is a User or a user id; the résumé is the one the sweep scores (D-81, the newest with
    text). None when the user has no such résumé. No score is stored, but a fresh posting cache
    entry is, and the caller's session is COMMITTED for it. The keys are
    ``MatchResult``'s plus ``fingerprint``; consumers key on ``outcome`` (compliance.md section 2:
    only SCORED shows a number)."""
    from backend.services.jobs_sidebar import scoring_resume

    engine = engine or get_engine()
    day = as_of or fit_as_of()
    user_id = int(getattr(user, "id", user))
    profile = scoring_resume(db, user_id)
    if profile is None:
        return None
    settings = _settings_for(db, user_id)
    parsed = resume_for_profile(profile, settings, engine=engine, as_of=day)
    posting = posting_for_job(job, engine=engine, as_of=day)
    commit_posting_cache(db, job)
    result = _score_pair(parsed, posting, _calibrator(db, engine), engine, day, _DETAIL_OPTIONS,
                         getattr(job, "id", "?"))
    detail = json.loads(result.model_dump_json())
    detail["fingerprint"] = _fingerprint(profile, settings, parsed, engine)
    return detail


__all__ = [
    "ACTIVE_CLEARANCE_ANSWER",
    "FitEngine",
    "FitEngineError",
    "FitTraceSaltError",
    "bank_scores",
    "build_engine",
    "build_shadow_engine",
    "commit_posting_cache",
    "current_fingerprints",
    "delete_user_traces",
    "engine_ready",
    "explain_job",
    "fingerprint_for",
    "fit_as_of",
    "get_engine",
    "live_references",
    "load_verified_panel",
    "posting_for_job",
    "posting_keys",
    "posting_meta",
    "postings_for",
    "profile_fields",
    "RequirementCoverage",
    "posting_from_text",
    "reason_sentences",
    "refresh_calibration",
    "requirement_coverage",
    "reset_text_postings",
    "resume_for_document",
    "resume_for_profile",
    "score_document",
    "score_order",
    "shadow_config_dir",
    "shadow_scores",
    "stored_score",
    "subject_id",
]
