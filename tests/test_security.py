import re
from datetime import timedelta

import pytest

from app import PUBLIC_ENDPOINTS
from app.extensions import db
from app.models import TwoFactorCode, User, utcnow
from app.routes.auth import safe_next
from conftest import login


@pytest.mark.parametrize("path", ["/admin/", "/admin/dashboard", "/admin/events", "/admin/events/1", "/admin/users", "/admin/units", "/admin/categories", "/admin/audit", "/admin/profile", "/admin/photos/1/content"])
def test_admin_requires_login(client, path):
    response = client.get(path)
    assert response.status_code == 302 and "/admin/login" in response.location


def test_default_deny_applies_to_new_routes(app, client):
    app.add_url_rule("/api/internal-new", endpoint="new_internal", view_func=lambda: {"secret": True})
    assert client.get("/api/internal-new").status_code == 401
    assert "new_internal" not in PUBLIC_ENDPOINTS


def test_captcha_rotates_and_2fa_is_mandatory(client):
    client.get("/admin/login")
    response = client.post("/admin/login", data={"email": "superadmin@lavalleja.uy", "password": "Contraseña-pruebas-123", "captcha": "-1"})
    assert "CAPTCHA incorrecto" in response.get_data(as_text=True)
    assert not client.application.extensions.get("outbox")
    with client.session_transaction() as session:
        captcha = session["captcha_result"]
    response = client.post("/admin/login", data={"email": "superadmin@lavalleja.uy", "password": "Contraseña-pruebas-123", "captcha": str(captcha)})
    assert response.location.endswith("/admin/2fa")
    assert client.get("/admin/events").status_code == 302
    assert client.post("/admin/2fa", data={"code": "bad"}).status_code == 200
    code = client.application.extensions["outbox"][-1]["code"]
    assert client.post("/admin/2fa", data={"code": code}).status_code == 302
    assert client.get("/admin/events").status_code == 200
    challenge = TwoFactorCode.query.first()
    assert challenge.consumed_at and challenge.code_hash != code


def test_expired_and_exhausted_code_cannot_login(client):
    client.get("/admin/login")
    with client.session_transaction() as session:
        captcha = session["captcha_result"]
    client.post("/admin/login", data={"email": "superadmin@lavalleja.uy", "password": "Contraseña-pruebas-123", "captcha": captcha})
    code = client.application.extensions["outbox"][-1]["code"]
    challenge = TwoFactorCode.query.first()
    challenge.expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    assert client.post("/admin/2fa", data={"code": code}).status_code == 200
    challenge.expires_at = utcnow() + timedelta(minutes=10)
    challenge.attempts = 5
    db.session.commit()
    assert client.post("/admin/2fa", data={"code": code}).status_code == 200
    assert client.get("/admin/events").status_code == 302


def test_disabled_user_and_changed_password_revoke_session(authenticated):
    user = User.query.filter_by(role="superadmin").first()
    user.set_password("Otra-contraseña-segura-123")
    db.session.commit()
    assert authenticated.get("/admin/events").status_code == 302


def test_disabled_user_cannot_complete_pending_2fa(client):
    client.get("/admin/login")
    with client.session_transaction() as session:
        captcha = session["captcha_result"]
    client.post("/admin/login", data={"email": "superadmin@lavalleja.uy", "password": "Contraseña-pruebas-123", "captcha": captcha})
    user = User.query.filter_by(role="superadmin").first()
    user.is_active = False
    db.session.commit()
    code = client.application.extensions["outbox"][-1]["code"]
    assert client.post("/admin/2fa", data={"code": code}).location.endswith("/admin/login")


def test_admin_scope_and_superadmin_pages(client):
    login(client, "otro-editor@lavalleja.uy")
    assert client.get("/admin/events/1").status_code == 404
    assert client.post("/admin/events/1/delete").status_code == 404
    for path in ("users", "categories", "units", "audit"):
        assert client.get("/admin/" + path).status_code == 403


def test_csrf_enforced_for_mutation(app, client):
    login(client)
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/admin/events/1/delete").status_code == 400
    body = client.get("/admin/events/1").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    assert client.post("/admin/events/1/delete", data={"csrf_token": token}).status_code == 302


@pytest.mark.parametrize("value", ["https://evil.test", "//evil.test", "/\\evil.test", "/admin/\\evil.test", "/api/public/events", "/admin/\nother"])
def test_redirects_stay_inside_admin(value):
    assert safe_next(value) is None


def test_internal_redirect_allowed():
    assert safe_next("/admin/events/1?tab=photos") == "/admin/events/1?tab=photos"
