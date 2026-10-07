import io
import hashlib
import zipfile

from PIL import Image
from app.extensions import db
from app.media import cleanup_storage
from app.models import Event, ObjectCleanup, Photo, Unit
from conftest import image_bytes, login, upload


def test_drafts_never_leak_and_unpublishing_revokes_access(authenticated):
    assert upload(authenticated).status_code == 201
    assert authenticated.get("/api/public/events").json["total"] == 0
    assert authenticated.get("/api/public/events/ciclovia").status_code == 404
    photo = Photo.query.first()
    assert authenticated.get(f"/api/public/photos/{photo.id}/content").status_code == 404
    assert authenticated.get("/api/public/events/ciclovia/download").status_code == 404
    authenticated.post("/admin/events/1/publish")
    assert authenticated.get("/api/public/events").json["total"] == 1
    assert authenticated.get(f"/api/public/photos/{photo.id}/content").status_code == 200
    authenticated.post("/admin/events/1/publish")
    assert authenticated.get(f"/api/public/photos/{photo.id}/content").status_code == 404


def test_upload_converts_to_webp_and_serves_minio_preview(authenticated, app):
    original = image_bytes()
    assert upload(authenticated, data=original).status_code == 201
    photo = Photo.query.first()
    assert photo.width == 120 and photo.height == 80 and photo.is_cover
    assert ObjectCleanup.query.count() == 0
    stored = app.extensions["media_storage"].objects[photo.object_key]
    assert stored != original
    assert photo.object_key.endswith(".webp") and photo.filename == "foto.webp"
    assert photo.content_type == "image/webp" and photo.size_bytes == len(stored)
    assert photo.sha256 == hashlib.sha256(stored).hexdigest()
    with Image.open(io.BytesIO(stored)) as image:
        assert image.format == "WEBP" and image.size == (120, 80)
    response = authenticated.get(f"/admin/photos/{photo.id}/content?download=1")
    assert response.data == stored and response.content_type == "image/webp"
    assert "foto.webp" in response.headers["Content-Disposition"]
    response = authenticated.get(f"/admin/photos/{photo.id}/content?variant=preview")
    assert response.content_type == "image/webp" and response.data[:4] == b"RIFF"
    assert response.headers["Cache-Control"] == "no-store"


def test_invalid_image_and_batch_rejected_without_objects(authenticated, app):
    assert upload(authenticated, "fake.jpg", b"<script>alert(1)</script>").status_code == 400
    assert not app.extensions["media_storage"].objects and Photo.query.count() == 0
    response = authenticated.post("/admin/events/1/photos", data={"photos": [(io.BytesIO(image_bytes()), "real.jpg"), (io.BytesIO(b"bad"), "fake.png")]}, content_type="multipart/form-data")
    assert response.status_code == 400 and not app.extensions["media_storage"].objects


def test_quota_enforced(authenticated, app):
    Unit.query.first().quota_bytes = 1
    db.session.commit()
    assert upload(authenticated).status_code == 400
    assert Photo.query.count() == 0 and not app.extensions["media_storage"].objects


def test_failed_upload_compensates_original(authenticated, app):
    app.extensions["media_storage"].fail_upload = True
    assert upload(authenticated).status_code == 503
    assert Photo.query.count() == 0 and not app.extensions["media_storage"].objects


def test_failed_cleanup_is_durable(authenticated, app):
    assert upload(authenticated).status_code == 201
    app.extensions["media_storage"].fail_delete = True
    assert authenticated.post("/admin/photos/1/delete").status_code == 302
    assert Photo.query.count() == 0 and ObjectCleanup.query.count() == 2
    assert cleanup_storage()[1] == 2 and ObjectCleanup.query.count() == 2
    app.extensions["media_storage"].fail_delete = False
    assert cleanup_storage() == (2, 0)
    assert not app.extensions["media_storage"].objects


def test_public_search_dates_pagination_and_zip(authenticated):
    upload(authenticated)
    upload(authenticated, "segunda.jpg", image_bytes("red"))
    authenticated.post("/admin/events/1/publish")
    assert authenticated.get("/api/public/events?q=ciclovia").json["total"] == 1
    assert authenticated.get("/api/public/events?q=inexistente").json["total"] == 0
    assert authenticated.get("/api/public/events?from=2027-01-01&to=2026-01-01").status_code == 400
    assert authenticated.get("/api/public/events?from=bad").status_code == 400
    assert authenticated.get("/api/public/events?sort=bad").status_code == 400
    assert authenticated.get("/api/public/events?page=2&per_page=1").json["items"] == []
    assert authenticated.get("/api/public/categories").json["items"][0]["count"] == 1
    assert authenticated.get("/api/public/stats").json == {"albums": 1, "photos": 2}
    response = authenticated.get("/api/public/events/ciclovia/download?photos=1")
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.namelist() == ["ciclovia-001.webp", "LEEME.txt"]
        assert archive.read("ciclovia-001.webp")[:4] == b"RIFF"
        assert "Créditos" in archive.read("LEEME.txt").decode()
    assert authenticated.get("/api/public/events/ciclovia/download?photos=999").status_code == 400


def test_range_and_head(authenticated):
    upload(authenticated)
    stored = authenticated.get("/admin/photos/1/content").data
    response = authenticated.get("/admin/photos/1/content", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206 and response.data == stored[:10]
    assert authenticated.get("/admin/photos/1/content", headers={"Range": "bytes=999999-"}).status_code == 416
    assert authenticated.head("/admin/photos/1/content").status_code == 200


def test_cross_unit_photo_protection(app, client):
    login(client)
    upload(client)
    client.post("/admin/logout")
    login(client, "otro-editor@lavalleja.uy")
    assert client.get("/admin/photos/1/content").status_code == 404
    assert client.post("/admin/photos/1/delete").status_code == 404


def test_last_photo_unpublishes_event(authenticated):
    upload(authenticated)
    authenticated.post("/admin/events/1/publish")
    authenticated.post("/admin/photos/1/delete")
    assert not db.session.get(Event, 1).published
    assert authenticated.get("/api/public/events").json["total"] == 0
