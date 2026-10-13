"""A new settings row has no location (owner decision, docs/fit-engine.md "Decisions").

``user_settings.location`` used to be created as "United States": a default posing as the user's
answer, which autofill sent to employers and the engine read as a residence. New rows now start
empty. Existing rows are untouched (no data migration), and every reader already handles an empty
location: autofill and the extension sync fall through to the résumé, the settings page shows a
blank field, and the engine reads the résumé's location.
"""

import pytest

from backend.auth.dependencies import get_verified_user, get_verified_user_id
from backend.db.models import ResumeProfileDB, User, UserSettings
from backend.main import app
from backend.services import fit_reads

USER_ID = 7


@pytest.fixture
def user(db_session):
    u = User(id=USER_ID, email="loc@example.com", first_name="Loc", last_name="Test",
             email_verified=True, auth_provider="local")
    db_session.add(u)
    db_session.commit()

    async def _user():
        return u

    async def _user_id():
        return u.id

    app.dependency_overrides[get_verified_user] = _user
    app.dependency_overrides[get_verified_user_id] = _user_id
    yield u
    app.dependency_overrides.pop(get_verified_user, None)
    app.dependency_overrides.pop(get_verified_user_id, None)


def test_a_new_settings_row_has_no_location(db_session, user):
    row = UserSettings(user_id=user.id)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    assert row.location == ""


def test_an_existing_default_is_left_as_it_was(db_session, user):
    row = UserSettings(user_id=user.id, location="United States")
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    assert row.location == "United States"


def test_autofill_reads_the_resume_location_for_a_new_settings_row(client, db_session, user):
    db_session.add(ResumeProfileDB(user_id=user.id, name="Resume", is_primary=1, profile_name="Loc Test",
                                   location="Ottawa, ON, Canada", raw_text="resume"))
    db_session.commit()
    # Any write creates the settings row (here, the phone): the location must not appear from nowhere.
    assert client.put("/api/user/application-profile", json={"phone": "+1 555 0100"}).status_code == 200

    body = client.get("/api/user/application-profile").json()
    assert body["location"] == "Ottawa, ON, Canada"
    assert "United States" not in body.values()


def test_settings_page_shows_a_blank_location(client, db_session, user):
    body = client.get("/settings").json()
    assert body["location"] == ""


def test_the_engine_reads_the_resume_location_for_a_new_settings_row(db_session, user):
    row = UserSettings(user_id=user.id)
    db_session.add(row)
    db_session.commit()
    profile = ResumeProfileDB(user_id=user.id, location="Toronto, ON", raw_text="resume")
    assert fit_reads.profile_fields(profile, row)["location"] == "Toronto, ON"
