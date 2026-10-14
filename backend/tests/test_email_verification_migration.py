"""
backend/migrations/add_email_verification.py runs on every startup.

The grandfather clause (mark existing users verified, clear their tokens) must run only on the
run that adds ``email_verified``. Re-running it on every cold start would verify every user who
signed up since and break every pending verification link.
"""

from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import backend.migrations.add_email_verification as migration


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR)"))
        conn.execute(text("INSERT INTO users (id, email) VALUES (1, 'old@example.com')"))
    return engine


def _rows(engine):
    with engine.connect() as conn:
        return {r[0]: (bool(r[1]), r[2]) for r in conn.execute(
            text("SELECT id, email_verified, verification_token FROM users ORDER BY id"))}


def test_first_run_grandfathers_existing_users(monkeypatch):
    engine = _engine()
    monkeypatch.setattr(migration, "engine", engine)
    migration.run_migration()
    assert _rows(engine) == {1: (True, None)}


def test_later_runs_leave_new_users_and_their_tokens_alone(monkeypatch):
    engine = _engine()
    monkeypatch.setattr(migration, "engine", engine)
    migration.run_migration()
    expires = datetime.utcnow() + timedelta(hours=1)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email, email_verified, verification_token, verification_token_expires_at) "
                 "VALUES (2, 'new@example.com', 0, 'tok-123', :e)"),
            {"e": expires},
        )
    migration.run_migration()
    assert _rows(engine) == {1: (True, None), 2: (False, "tok-123")}
