import io
from pathlib import Path

import pytest
from flask import g
from flask_migrate import upgrade
from PIL import Image

from app import create_app
from app.extensions import db
from app.models import Category, Event, Unit, User
from app.security import normalize
from app.storage import StorageError


class ObjectStream(io.BytesIO):
    def stream(self, size):
        while data := self.read(size):
            yield data

    def release_conn(self):
        pass


class MemoryStorage:
    bucket = "test-private"

    def __init__(self):
        self.objects = {}
        self.fail_upload = False
        self.fail_delete = False

    def put(self, key, data, content_type):
        if self.fail_upload and key.endswith("preview.webp"):
            raise StorageError("Injected MinIO failure")
        self.objects[key] = data

    def open(self, key, offset=0, length=0):
        data = self.objects[key]
        return ObjectStream(data[offset:offset + length if length else None])

    def delete(self, key):
        if self.fail_delete:
            raise StorageError("Injected deletion failure")
        self.objects.pop(key, None)

    def healthcheck(self):
        pass


@pytest.fixture
def app():
    app = create_app("testing")
    # This fixture retains an app context for direct DB assertions. Production
    # starts a fresh g per request; reproduce that for Flask-Login's user cache.
    def reset_request_cache():
        for name in ("_login_user", "csrf_token", "csrf_valid"):
            g.pop(name, None)
    app.before_request_funcs[None].insert(0, reset_request_cache)
    app.extensions["media_storage"] = MemoryStorage()
    with app.app_context():
        upgrade(directory=str(Path(__file__).resolve().parents[1] / "migrations"))
        one, two = Unit(name="Comunicación", quota_bytes=20 * 1024**3), Unit(name="Cultura", quota_bytes=20 * 1024**3)
        category = Category(name="Obras")
        db.session.add_all([one, two, category])
        db.session.flush()
        for username, role, unit in (("superadmin", "superadmin", None), ("editor", "admin", one), ("otro-editor", "admin", two)):
            user = User(username=username, email=username + "@lavalleja.uy", role=role, unit_id=unit.id if unit else None)
            user.set_password("Contraseña-pruebas-123")
            db.session.add(user)
        db.session.commit()
        event = Event(slug="ciclovia", title="Nueva ciclovía", summary="Obras en Minas", description="Registro de la nueva ciclovía.",
                      location="Minas", category_id=category.id, unit_id=one.id, created_by_id=1,
                      search_text=normalize("Nueva ciclovía Obras en Minas"))
        db.session.add(event)
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, email="superadmin@lavalleja.uy"):
    client.get("/admin/login")
    with client.session_transaction() as session:
        captcha = session["captcha_result"]
    response = client.post("/admin/login", data={"email": email, "password": "Contraseña-pruebas-123", "captcha": str(captcha)})
    assert response.status_code == 302 and response.location.endswith("/admin/2fa")
    code = client.application.extensions["outbox"][-1]["code"]
    response = client.post("/admin/2fa", data={"code": code})
    assert response.status_code == 302
    return code


@pytest.fixture
def authenticated(client):
    login(client)
    return client


def image_bytes(color="green"):
    output = io.BytesIO()
    Image.new("RGB", (120, 80), color).save(output, "JPEG")
    return output.getvalue()


def upload(client, filename="foto.jpg", data=None):
    return client.post("/admin/events/1/photos", data={"photos": (io.BytesIO(data or image_bytes()), filename), "alt": "Nueva ciclovía de Minas"}, content_type="multipart/form-data")
