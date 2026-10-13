"""Match-alert opt-out: the unsubscribe token, the link and headers every alert
carries, the unauthenticated endpoint that flips the flag, and the Settings
field that shows it.

Alerts shipped with no opt-out at all (91 sent, 57 of them to one user) while
the Privacy Policy promised "the unsubscribe link in emails", and the footer
told people they had "enabled match alerts" when no such toggle existed. Most
users are Canadian students: CASL requires a working unsubscribe mechanism in
every commercial electronic message.
"""

import base64
import datetime
import hashlib
import hmac
import html
import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.db.models import JobMatchNotification, ScrapedJob, User, UserSettings
from backend.services import alert_unsubscribe, match_notifier
from backend.services.alert_unsubscribe import (
    UNSUBSCRIBE_PATH,
    make_token,
    unsubscribe_url,
    verify_token,
)
from backend.services.email_service import EmailService

TEST_USER_ID = 1  # what the conftest client authenticates as


def _user(db_session, email="alerts@example.com", **fields):
    user = User(email=email, first_name="Sam", email_verified=True,
                auth_provider="local", **fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _job(db_session, title="Engineer", **fields):
    job = ScrapedJob(
        title=title,
        company="Kinaxis",
        url=f"https://jobs.example.com/{title}".replace(" ", "-"),
        description="x" * 200,
        posted_date=datetime.datetime.utcnow(),
        **fields,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _token_from(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


# ─── Token ───────────────────────────────────────────────────────────────────

def test_token_round_trips_to_its_user():
    assert verify_token(make_token(42)) == 42


def _mangled():
    good = make_token(42)
    uid, mac = good.split(".")
    flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
    return [
        f"43.{mac}",  # someone else's id under my MAC
        f"{uid}.{flipped}",  # one character off
        f"{uid}.{mac}x",
        f"{uid}.",
        f".{mac}",
        uid,
        "",
        None,
        f"-42.{mac}",
        f"4{'2' * 20}.{mac}",
        "abc.def",
        # Unicode digits pass str.isdigit(): "²" then crashed int() (a 500),
        # and Arabic-Indic "٤٢" read as 42 under 42's own MAC.
        "².abc",
        f"².{mac}",
        f"٤٢.{mac}",
    ]


@pytest.mark.parametrize("token", _mangled())
def test_forged_or_mangled_tokens_are_rejected(token):
    assert verify_token(token) is None


def test_token_is_bound_to_the_server_secret(monkeypatch):
    token = make_token(42)
    monkeypatch.setenv("JWT_SECRET", "a-different-secret")
    assert verify_token(token) is None


def test_token_key_is_derived_for_this_purpose_only():
    """A MAC of the id under JWT_SECRET itself (what signs our JWTs) is not a
    valid unsubscribe token: the key is derived for unsubscribing alone."""
    raw = hmac.new(os.environ["JWT_SECRET"].encode(), b"42", hashlib.sha256).digest()
    forged = "42." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert verify_token(forged) is None


def test_no_secret_means_no_tokens(monkeypatch):
    token = make_token(42)
    monkeypatch.setenv("JWT_SECRET", "")
    assert verify_token(token) is None
    with pytest.raises(RuntimeError):
        make_token(42)


def test_unsubscribe_url_is_absolute_on_the_app_origin(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://www.tailrd.ca/")
    url = unsubscribe_url(42)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://www.tailrd.ca/settings/unsubscribe-alerts"
    )
    assert verify_token(_token_from(url)) == 42


def test_unsubscribe_url_is_empty_without_frontend_url(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    assert alert_unsubscribe.unsubscribe_url(42) == ""


# ─── Email ───────────────────────────────────────────────────────────────────

JOBS = [{"title": "Engineer", "company": "Kinaxis", "match_score": 88,
         "apply_url": "https://www.tailrd.ca/app?job=1"}]
LINK = "https://www.tailrd.ca/settings/unsubscribe-alerts?token=42.abc"


def _configured() -> EmailService:
    svc = EmailService()
    svc.api_key = "re_test"
    svc.from_email = "alerts@tailrd.ca"
    svc.frontend_url = None
    return svc


@pytest.fixture
def resend_payload(monkeypatch):
    import backend.services.email_service as es

    captured: dict = {}
    monkeypatch.setattr(es.resend.Emails, "send", staticmethod(lambda p: captured.update(p)))
    return captured


def test_alert_carries_one_click_unsubscribe_headers_and_a_footer_link(resend_payload):
    assert _configured().send_job_match_alert(
        "u@example.com", JOBS, "Sam", unsubscribe_url=LINK
    ) is True

    # RFC 8058: what puts the mail client's own "Unsubscribe" button up top.
    assert resend_payload["headers"] == {
        "List-Unsubscribe": f"<{LINK}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert f'href="{LINK}"' in resend_payload["html"]
    assert "Unsubscribe from match alerts" in resend_payload["html"]


def test_footer_no_longer_claims_the_user_enabled_alerts():
    html = _configured()._build_job_alert_html(JOBS, unsubscribe_url=LINK)
    assert "enabled match alerts" not in html
    assert "because you uploaded a resume" in html


def test_alert_without_a_link_sends_no_unsubscribe_headers(resend_payload):
    assert _configured().send_job_match_alert("u@example.com", JOBS) is True
    assert "headers" not in resend_payload
    assert "under Settings" in resend_payload["html"]


# ─── Notifier ────────────────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    def fake(to, jobs, name=None, unsubscribe_url=None):
        calls.append({"to": to, "jobs": jobs, "unsubscribe_url": unsubscribe_url})
        return True

    monkeypatch.setattr(match_notifier.email_service, "send_job_match_alert", fake)
    return calls


def test_notify_hands_each_recipient_their_own_unsubscribe_link(db_session, sent, monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://www.tailrd.ca")
    user = _user(db_session)

    assert match_notifier.notify_high_matches(db_session, user.id, [(_job(db_session), 95, "Strong match", "SCORED")]) == 1
    assert verify_token(_token_from(sent[0]["unsubscribe_url"])) == user.id


def test_notify_sends_nothing_to_an_opted_out_user(db_session, sent):
    user = _user(db_session)
    db_session.add(UserSettings(user_id=user.id, match_alerts_enabled=False))
    db_session.commit()

    assert match_notifier.notify_high_matches(db_session, user.id, [(_job(db_session), 99, "Strong match", "SCORED")]) == 0
    assert sent == []
    assert db_session.query(JobMatchNotification).count() == 0


def test_notify_drops_jobs_outside_the_users_region(db_session, sent):
    """The resume-upload path hands over whatever it scored; a Canada-only
    user still only hears about Canadian jobs."""
    user = _user(db_session)
    db_session.add(UserSettings(user_id=user.id, regions="CA"))
    db_session.commit()
    toronto = _job(db_session, "Toronto Role", country="CA")
    austin = _job(db_session, "Austin Role", country="US")

    count = match_notifier.notify_high_matches(
        db_session, user.id, [(toronto, 95, "Strong match", "SCORED"), (austin, 97, "Strong match", "SCORED")]
    )

    assert count == 1
    assert [j["title"] for j in sent[0]["jobs"]] == ["Toronto Role"]
    assert {r.job_id for r in db_session.query(JobMatchNotification).all()} == {toronto.id}


# ─── Endpoint ────────────────────────────────────────────────────────────────

def _flag(db_session, user_id):
    db_session.expire_all()
    row = db_session.query(UserSettings).filter_by(user_id=user_id).first()
    return None if row is None else row.match_alerts_enabled


def _form_action(page: str) -> str:
    """The confirm button's target, as a browser would resolve it."""
    match = re.search(r'<form method="post" action="([^"]+)"', page)
    assert match, page
    return html.unescape(match.group(1))


def test_footer_link_asks_and_the_button_turns_alerts_off(client, db_session):
    # No settings row at all, like 6 of the 12 accounts in prod.
    user = _user(db_session)

    page = client.get(UNSUBSCRIBE_PATH, params={"token": make_token(user.id)})

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Unsubscribe from match alerts?" in page.text
    assert _flag(db_session, user.id) is None  # asking changed nothing

    action = _form_action(page.text)
    assert urlsplit(action).path == UNSUBSCRIBE_PATH
    res = client.post(action)

    assert res.status_code == 200
    assert "unsubscribed" in res.text.lower()
    assert _flag(db_session, user.id) is False


def test_a_mail_scanner_opening_the_link_unsubscribes_nobody(client, db_session):
    """University inboxes (Defender Safe Links, Proofpoint, Mimecast) GET every
    link in a message on delivery. However often that happens, alerts stay on
    until a person presses the button."""
    user = _user(db_session)
    db_session.add(UserSettings(user_id=user.id))
    db_session.commit()

    for _ in range(3):
        assert client.get(UNSUBSCRIBE_PATH, params={"token": make_token(user.id)}).status_code == 200

    assert _flag(db_session, user.id) is True


def test_one_click_post_turns_alerts_off(client, db_session):
    user = _user(db_session)
    db_session.add(UserSettings(user_id=user.id))
    db_session.commit()
    assert _flag(db_session, user.id) is True

    # Exactly what Gmail sends: the List-Unsubscribe URL, POSTed with this body.
    res = client.post(
        f"{UNSUBSCRIBE_PATH}?token={make_token(user.id)}",
        data={"List-Unsubscribe": "One-Click"},
    )

    assert res.status_code == 200
    assert _flag(db_session, user.id) is False


def test_the_link_after_unsubscribing_just_confirms(client, db_session):
    user = _user(db_session)
    client.post(f"{UNSUBSCRIBE_PATH}?token={make_token(user.id)}")

    page = client.get(UNSUBSCRIBE_PATH, params={"token": make_token(user.id)})

    assert page.status_code == 200
    assert "You're unsubscribed" in html.unescape(page.text)
    assert "<form" not in page.text


def test_unsubscribe_needs_no_login(client, db_session):
    from backend.auth.dependencies import (
        get_current_user_id,
        get_optional_user_id,
        get_verified_user_id,
    )
    from backend.main import app

    for dep in (get_current_user_id, get_optional_user_id, get_verified_user_id):
        app.dependency_overrides.pop(dep, None)
    user = _user(db_session)
    token = make_token(user.id)

    assert client.get(UNSUBSCRIBE_PATH, params={"token": token}).status_code == 200
    res = client.post(f"{UNSUBSCRIBE_PATH}?token={token}")

    assert res.status_code == 200
    assert _flag(db_session, user.id) is False


def test_a_tampered_token_changes_nothing(client, db_session):
    me = _user(db_session)
    other = _user(db_session, email="other@example.com")
    mac = make_token(me.id).split(".")[1]

    for bad in (f"{other.id}.{mac}", f"{me.id}.{mac[:-1]}", ""):
        for res in (
            client.get(UNSUBSCRIBE_PATH, params={"token": bad}),
            client.post(UNSUBSCRIBE_PATH, params={"token": bad}),
        ):
            assert res.status_code == 400, bad
            assert "invalid" in res.text.lower()
            assert "<form" not in res.text

    assert _flag(db_session, me.id) is None
    assert _flag(db_session, other.id) is None


def test_a_unicode_digit_token_is_a_400_not_a_500(client, db_session):
    """The endpoint is unauthenticated: a crafted id like "²" must get the
    invalid-link page, never an unhandled exception."""
    for res in (
        client.get(UNSUBSCRIBE_PATH, params={"token": "².abc"}),
        client.post(f"{UNSUBSCRIBE_PATH}?token=%C2%B2.abc"),
    ):
        assert res.status_code == 400
        assert "invalid" in res.text.lower()


def test_unsubscribing_twice_is_harmless(client, db_session):
    user = _user(db_session)
    token = make_token(user.id)

    assert client.post(f"{UNSUBSCRIBE_PATH}?token={token}").status_code == 200
    assert client.post(f"{UNSUBSCRIBE_PATH}?token={token}").status_code == 200
    assert _flag(db_session, user.id) is False
    assert db_session.query(UserSettings).filter_by(user_id=user.id).count() == 1


def test_a_deleted_account_gets_the_page_but_no_row(client, db_session):
    token = make_token(9999)
    for res in (
        client.get(UNSUBSCRIBE_PATH, params={"token": token}),
        client.post(f"{UNSUBSCRIBE_PATH}?token={token}"),
    ):
        assert res.status_code == 200
        assert "You're unsubscribed" in html.unescape(res.text)

    assert db_session.query(UserSettings).count() == 0


def test_unsubscribe_pages_allow_inline_styles_and_a_same_origin_form_only(client, db_session):
    user = _user(db_session)
    token = make_token(user.id)

    for res in (
        client.get(UNSUBSCRIBE_PATH, params={"token": token}),
        client.post(f"{UNSUBSCRIBE_PATH}?token={token}"),
    ):
        csp = res.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "style-src 'unsafe-inline'" in csp
        assert "form-action 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "script" not in csp
        assert "<script" not in res.text.lower()
        assert res.headers["cache-control"] == "no-store"


# ─── Settings ────────────────────────────────────────────────────────────────

@pytest.fixture
def me(db_session):
    return _user(db_session, email="me@example.com", id=TEST_USER_ID)


def test_settings_show_alerts_on_by_default_and_the_toggle_persists(client, me):
    assert client.get("/settings").json()["match_alerts_enabled"] is True

    res = client.put("/settings", json={"match_alerts_enabled": False})
    assert res.status_code == 200
    assert res.json()["match_alerts_enabled"] is False
    assert client.get("/settings").json()["match_alerts_enabled"] is False

    assert client.put("/settings", json={"match_alerts_enabled": True}).json()[
        "match_alerts_enabled"
    ] is True


def test_an_unrelated_settings_save_leaves_alerts_alone(client, me):
    client.put("/settings", json={"match_alerts_enabled": False})
    client.put("/settings", json={"smooth_scrolling": True})
    assert client.get("/settings").json()["match_alerts_enabled"] is False


def test_settings_show_an_email_unsubscribe(client, me):
    client.post(f"{UNSUBSCRIBE_PATH}?token={make_token(me.id)}")
    assert client.get("/settings").json()["match_alerts_enabled"] is False
