from app.extensions import db
from app.models import Event, User
from conftest import upload


def test_all_admin_templates_render(authenticated):
    upload(authenticated)
    for path in ("/admin/", "/admin/events", "/admin/events/new", "/admin/events/1", "/admin/users", "/admin/units", "/admin/categories", "/admin/audit", "/admin/profile"):
        assert authenticated.get(path).status_code == 200, path


def test_create_event_and_duplicate_slug(authenticated):
    data = {"title": "Nuevo festival", "slug": "festival", "event_date": "2026-10-06", "category_id": "1", "unit_id": "1", "summary": "Festival", "description": "Cobertura del festival", "location": "Minas", "credits": "Lavalleja", "usage_terms": "Citar créditos", "cover_position": "center", "source_url": "https://www.gub.uy/intendencia-lavalleja/"}
    response = authenticated.post("/admin/events/new", data=data)
    assert response.status_code == 302 and Event.query.filter_by(slug="festival").count() == 1
    assert authenticated.post("/admin/events/new", data=data).status_code == 200
    assert Event.query.filter_by(slug="festival").count() == 1
    assert authenticated.post("/admin/events/2/publish").status_code == 302
    assert not db.session.get(Event, 2).published


def test_cli_same_signature_and_seed_idempotent(app):
    runner = app.test_cli_runner()
    assert runner.invoke(args=["seed-data"]).exit_code == 0
    assert runner.invoke(args=["seed-data"]).exit_code == 0
    result = runner.invoke(args=["create-admin", "nuevo-admin", "nuevo@lavalleja.uy", "Contraseña-segura-123", "true"])
    assert result.exit_code == 0, result.output
    assert User.query.filter_by(email="nuevo@lavalleja.uy").one().is_superadmin
    result = runner.invoke(args=["create-admin", "admin-default", "default@lavalleja.uy", "Contraseña-segura-123", "false"])
    assert result.exit_code == 0, result.output
    assert User.query.filter_by(email="default@lavalleja.uy").one().unit.name == "Comunicación"
    result = runner.invoke(args=["create-admin", "admin-cultura", "cultura@lavalleja.uy", "Contraseña-segura-123", "false", "Cultura"])
    assert result.exit_code == 0, result.output
    assert User.query.filter_by(email="cultura@lavalleja.uy").one().unit.name == "Cultura"


def test_self_privilege_removal_rejected(authenticated):
    authenticated.post("/admin/users/1/update", data={"role": "admin", "unit_id": "1", "is_active": "on"})
    assert db.session.get(User, 1).is_superadmin
