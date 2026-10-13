"""
ApplyPilot, FastAPI Backend

Serves AI endpoints for the Chrome extension and React frontend.
Runs as Vercel serverless function or standalone with uvicorn.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from backend.services.llm_cost import configure_logging

# Before anything else logs: uvicorn and Vercel leave the root logger
# unconfigured, so without this the app's own INFO records (including the
# per-call `llm_cost` lines) are dropped and spend is unobservable.
configure_logging()

from backend.db.database import engine, Base
from backend.migrations.add_email_verification import run_migration
from backend.migrations.add_admin_role import run_migration as run_admin_migration
from backend.migrations.add_security_fields import run_migration as run_security_migration
from backend.migrations.add_extension_sync_fields import run_migration as run_extension_sync_migration
from backend.migrations.add_extension_auth_codes import run_migration as run_extension_auth_codes_migration
from backend.migrations.add_company_domain import run_migration as run_company_domain_migration
from backend.migrations.add_sessions import run_migration as run_sessions_migration
from backend.migrations.add_job_match_notifications import run_migration as run_job_match_notifications_migration
from backend.migrations.add_onboarding_field import run_migration as run_onboarding_migration
from backend.migrations.add_setup_field import run_migration as run_setup_migration
from backend.migrations.add_autofill_profile_fields import run_migration as run_autofill_profile_migration
from backend.migrations.add_profile_answer_columns import run_migration as run_profile_answer_columns_migration
from backend.migrations.add_resume_sections import run_migration as run_resume_sections_migration
from backend.migrations.add_resume_content_updated import run_migration as run_resume_content_updated_migration
from backend.migrations.add_job_catalogue_fields import run_migration as run_job_catalogue_migration
from backend.migrations.add_ingestion_freshness import run_migration as run_ingestion_freshness_migration
from backend.migrations.add_autofill_field_outcomes import run_migration as run_autofill_field_outcomes_migration
from backend.migrations.drop_saved_answers import run_migration as run_drop_saved_answers_migration
from backend.migrations.add_autofill_diagnostic_capture import run_migration as run_autofill_diagnostic_capture_migration
from backend.migrations.add_listing_probe_columns import run_migration as run_listing_probe_columns_migration
from backend.migrations.add_company_logos import run_migration as run_company_logos_migration
from backend.migrations.add_match_alerts_opt_out import run_migration as run_match_alerts_opt_out_migration
from backend.migrations.add_job_match_terms import run_migration as run_job_match_terms_migration
from backend.migrations.add_fit_engine import run_migration as run_fit_engine_migration
from backend.routers import health, resumes, jobs, settings, fill, ai, apply, connections, github_sources, profile, autofill
from backend.routers import auth, auth_extension, extension, tailor, cover_letter, auth_linkedin
from backend.routers.feedback import router as feedback_router
from backend.services.alert_unsubscribe import UNSUBSCRIBE_PATH

# Postgres only: create_all's lock waits are capped like the first-deploy
# migrations' (a CREATE queued behind an open transaction would otherwise
# stall the cold start), and cold starts take turns on an advisory lock.
CREATE_TABLES_LOCK_TIMEOUT = "5s"
# Arbitrary, unique to create_all (the migrations use ...001 and ...002).
_CREATE_TABLES_LOCK_KEY = 7_311_027_530_000


def create_tables(bind=None) -> None:
    """Base.metadata.create_all, safe for every cold start at once.

    create_all checks each table, then sends a plain CREATE TABLE for the
    missing ones. On Postgres two instances that both checked before either
    created a table would collide (the loser fails on a duplicate pg_type
    row), so the check and the CREATEs run under an advisory lock: the loser
    checks after the winner commits and finds the table. Once every table
    exists it sends no DDL and takes no lock. A lock timeout is not caught:
    startup fails and the next cold start retries. SQLite is unchanged.
    """
    bind = bind or engine
    if bind.dialect.name != "postgresql":
        Base.metadata.create_all(bind=bind)
        return
    if not set(Base.metadata.tables) - set(inspect(bind).get_table_names()):
        return
    with bind.begin() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{CREATE_TABLES_LOCK_TIMEOUT}'"))
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CREATE_TABLES_LOCK_KEY})
        Base.metadata.create_all(bind=conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    run_migration()
    run_admin_migration()
    run_security_migration()
    run_extension_sync_migration()
    run_extension_auth_codes_migration()
    run_company_domain_migration()
    run_sessions_migration()
    run_job_match_notifications_migration()
    run_onboarding_migration()
    run_setup_migration()
    run_autofill_profile_migration()
    run_profile_answer_columns_migration()
    run_resume_sections_migration()
    run_resume_content_updated_migration()
    run_job_catalogue_migration()
    run_ingestion_freshness_migration()
    run_autofill_field_outcomes_migration()
    run_drop_saved_answers_migration()
    run_autofill_diagnostic_capture_migration()
    # First-deploy DDL that every cold-starting instance runs at once. Each
    # caps its lock waits, so a failure is fast, and it is never swallowed:
    # an instance started without the column (or the logo store every insert
    # path reads) would answer 500 for as long as it stays warm, and nothing
    # would run the migration again. Failing startup leaves the retry to the
    # next cold start.
    run_listing_probe_columns_migration()
    run_company_logos_migration()
    # Every UserSettings query selects match_alerts_enabled, GET /settings
    # included, so an instance without the column must not serve either.
    run_match_alerts_opt_out_migration()
    run_job_match_terms_migration()
    # job_match_scores.score turns nullable and gains the engine columns; every
    # score query selects them once the producers switch (Task 9.2).
    run_fit_engine_migration()
    yield


_IS_PRODUCTION = os.getenv("ENVIRONMENT", "development") == "production"

# The interactive API docs (and the OpenAPI schema that powers them) expose the
# full surface of the API. Keep them on in development, but off in production
# unless explicitly re-enabled, so we don't hand attackers a map for free.
_DOCS_ENABLED = (not _IS_PRODUCTION) or os.getenv("ENABLE_DOCS", "").strip().lower() in ("1", "true", "yes")

# Reject oversized request bodies early. Resume PDFs are a few hundred KB; 10 MB
# leaves generous headroom while blocking obvious memory-exhaustion attempts.
try:
    MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(10 * 1024 * 1024)))
except ValueError:
    MAX_REQUEST_BYTES = 10 * 1024 * 1024

app = FastAPI(
    title="Tailrd API",
    version="0.2.0",
    description="API for the Tailrd web app and extension",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

# --- CORS Configuration ---
# Restrict origins to known frontends. Use CORS_ORIGINS env var in production.
_default_origins = "http://localhost:5173"
_cors_origins = os.getenv("CORS_ORIGINS", _default_origins).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# --- Request Size Guard ---
# Reject obviously oversized bodies before they are read into memory. This only
# inspects the declared Content-Length; the platform still caps truly unbounded
# (chunked) uploads, so this is a cheap first line of defence, not the only one.
@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large."},
                )
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length."})
    return await call_next(request)


# --- Security Headers Middleware ---
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP: the API serves JSON, so it should load nothing and be framed by no one.
    # Swagger/ReDoc are HTML and pull their own assets, so relax CSP just for them.
    path = request.url.path
    if path.startswith(_DOCS_PATHS):
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    elif path.startswith("/jobs/logo/") and "Content-Security-Policy" in response.headers:
        pass  # served SVG logos carry their own sandboxing CSP (inline styles allowed)
    elif path == UNSUBSCRIBE_PATH and "Content-Security-Policy" in response.headers:
        pass  # the unsubscribe pages: inline styles and one same-origin form, still no scripts
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
    # HSTS: only enable if behind HTTPS in production
    if _IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.include_router(health.router, tags=["health"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(auth_extension.router, prefix="/auth/extension", tags=["auth-extension"])
app.include_router(auth_linkedin.router, prefix="/auth/linkedin", tags=["auth-linkedin"])
app.include_router(fill.router, prefix="/api", tags=["fill"])
app.include_router(tailor.router, prefix="/api", tags=["tailor"])
app.include_router(cover_letter.router, prefix="/api", tags=["cover-letter"])
app.include_router(profile.router, prefix="/api", tags=["profile"])
app.include_router(extension.router, prefix="/api/extension", tags=["extension-sync"])
app.include_router(resumes.router, prefix="/resumes", tags=["resumes"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
app.include_router(settings.router, prefix="/settings", tags=["settings"])
app.include_router(ai.router, prefix="/ai", tags=["ai"])
app.include_router(apply.router, prefix="/apply", tags=["apply"])
app.include_router(autofill.router, prefix="/autofill", tags=["autofill"])
app.include_router(connections.router, prefix="/connections", tags=["connections"])
app.include_router(github_sources.router, prefix="/github-sources", tags=["github-sources"])
app.include_router(feedback_router)
