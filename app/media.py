import hashlib
import tempfile
import uuid
import zipfile
from datetime import timedelta

from flask import Response, abort, current_app, request, send_file
from sqlalchemy import func
from werkzeug.http import parse_range_header
from werkzeug.utils import secure_filename

from .extensions import db
from .models import Event, ObjectCleanup, Photo, Unit, utcnow
from .security import audit
from .storage import get_storage, validate_image


def unit_usage(unit_id):
    return db.session.query(func.coalesce(func.sum(Photo.size_bytes + Photo.preview_bytes), 0)).join(Event).filter(Event.unit_id == unit_id).scalar()


def upload_images(event, files, metadata=None):
    if not files or len(files) > current_app.config["MAX_BATCH_IMAGES"]:
        raise ValueError("Seleccioná entre 1 y 30 fotografías.")
    metadata = metadata or [{} for _ in files]
    if len(metadata) != len(files):
        raise ValueError("Los datos de las fotografías están incompletos.")
    prepared = []
    total = 0
    for file, meta in zip(files, metadata):
        image = validate_image(file.stream)
        total += len(image.data) + len(image.preview)
        if total > current_app.config["MAX_CONTENT_LENGTH"]:
            raise ValueError("La carga supera el tamaño máximo del lote.")
        filename = (secure_filename(file.filename or "foto") or "foto")[:230]
        alt = meta.get("alt", "").strip() or filename.rsplit(".", 1)[0].replace("_", " ")
        if len(alt) > 500 or len(meta.get("credits", "")) > 255:
            raise ValueError("El texto alternativo o los créditos son demasiado largos.")
        key = f"images/{uuid.uuid4().hex}"
        prepared.append((image, filename, alt, meta, key + image.extension, key + "-preview.webp"))
    # Before touching MinIO, persist compensation keys. A killed worker leaves a
    # recoverable lease; successful uploads remove the lease in the photo commit.
    leases = []
    for *_, key, preview_key in prepared:
        for object_key in (key, preview_key):
            lease = ObjectCleanup(object_key=object_key, not_before=utcnow() + timedelta(hours=2))
            leases.append(lease)
            db.session.add(lease)
    db.session.commit()
    try:
        # Serialize quota and photo-order changes for this unit across workers.
        unit = Unit.query.filter_by(id=event.unit_id).with_for_update().one()
        locked = Event.query.filter_by(id=event.id).with_for_update().first()
        if not locked:
            raise ValueError("El evento ya no existe.")
        if unit_usage(unit.id) + total > unit.quota_bytes:
            raise ValueError("La unidad no tiene espacio suficiente en su cuota.")
        position = db.session.query(func.max(Photo.position)).filter_by(event_id=event.id).scalar()
        position = (position + 1) if position is not None else 0
        has_cover = Photo.query.filter_by(event_id=event.id, is_cover=True).first() is not None
        uploaded = []
        for i, (image, filename, alt, meta, key, preview_key) in enumerate(prepared):
            get_storage().put(key, image.data, image.content_type)
            get_storage().put(preview_key, image.preview, "image/webp")
            photo = Photo(event_id=event.id, object_key=key, preview_key=preview_key,
                          filename=filename, content_type=image.content_type, width=image.width, height=image.height,
                          size_bytes=len(image.data), preview_bytes=len(image.preview),
                          sha256=hashlib.sha256(image.data).hexdigest(), alt=alt,
                          credits=meta.get("credits", "").strip(), source_url=meta.get("source_url", ""),
                          position=position + i, is_cover=(not has_cover and i == 0))
            db.session.add(photo)
            uploaded.append(photo)
        for lease in leases:
            db.session.delete(lease)
        audit("PHOTO_UPLOAD", f"Evento {event.id}: {len(uploaded)} fotografías")
        db.session.commit()
        return uploaded
    except Exception:
        db.session.rollback()
        # Durable leases survive rollback and will be cleaned by cleanup-storage.
        # Try promptly as well; failures remain queued.
        for *_, key, preview_key in prepared:
            for object_key in (key, preview_key):
                try:
                    get_storage().delete(object_key)
                    ObjectCleanup.query.filter_by(object_key=object_key).delete()
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    current_app.logger.exception("Objeto pendiente de limpieza")
        raise


def queue_photo_delete(photo):
    for key in (photo.object_key, photo.preview_key):
        db.session.add(ObjectCleanup(object_key=key))


def cleanup_storage():
    removed = failed = 0
    for job in ObjectCleanup.query.filter(ObjectCleanup.not_before <= utcnow()).limit(500).all():
        try:
            # Guard against accidental removal of a currently referenced object.
            referenced = Photo.query.filter((Photo.object_key == job.object_key) | (Photo.preview_key == job.object_key)).first()
            if not referenced:
                get_storage().delete(job.object_key)
            db.session.delete(job)
            db.session.commit()
            removed += 1
        except Exception:
            db.session.rollback()
            current_app.logger.exception("No se pudo limpiar un objeto de MinIO")
            failed += 1
    return removed, failed


def photo_response(photo):
    preview = request.args.get("variant") == "preview"
    key = photo.preview_key if preview else photo.object_key
    size = photo.preview_bytes if preview else photo.size_bytes
    mime = "image/webp" if preview else photo.content_type
    start, end, status = 0, size, 200
    if request.headers.get("Range"):
        parsed = parse_range_header(request.headers["Range"])
        region = parsed.range_for_length(size) if parsed else None
        if region is None:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        start, end = region
        status = 206
    stream = get_storage().open(key, offset=start, length=end - start) if request.method != "HEAD" else None

    def generate():
        try:
            yield from stream.stream(64 * 1024)
        finally:
            stream.close()
            stream.release_conn()

    response = Response(generate(), status=status, mimetype=mime, direct_passthrough=True)
    response.headers["Content-Length"] = str(end - start)
    response.headers["Accept-Ranges"] = "bytes"
    if status == 206:
        response.headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
    disposition = "attachment" if request.args.get("download") == "1" else "inline"
    name = secure_filename(photo.filename) or f"foto-{photo.id}"
    response.headers.set("Content-Disposition", disposition, filename=name)
    if stream:
        response.call_on_close(lambda: (stream.close(), stream.release_conn()))
    return response


def album_zip(event, photos):
    if not photos or len(photos) > current_app.config["MAX_ZIP_PHOTOS"]:
        abort(400)
    if sum(photo.size_bytes for photo in photos) > current_app.config["MAX_ZIP_BYTES"]:
        abort(413)
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024**2)
    try:
        with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for i, photo in enumerate(photos, 1):
                extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[photo.content_type]
                stream = get_storage().open(photo.object_key)
                try:
                    with archive.open(f"{event.slug}-{i:03d}{extension}", "w") as output:
                        for chunk in stream.stream(64 * 1024):
                            output.write(chunk)
                finally:
                    stream.close()
                    stream.release_conn()
            note = f"SALA DE MEDIOS · INTENDENCIA DE LAVALLEJA\n\n{event.title}\n{event.event_date}\n{event.location}\n\n{event.description}\n\nFuente: {event.source_url}\nCréditos: {event.credits}\nCondiciones: {event.usage_terms}\n\n"
            note += "\n".join(f"{i:03d}: {p.alt} | {p.credits or event.credits} | {p.source_url or event.source_url} | SHA-256: {p.sha256}" for i, p in enumerate(photos, 1))
            archive.writestr("LEEME.txt", note.encode("utf-8"))
        spool.seek(0)
        response = send_file(spool, mimetype="application/zip", as_attachment=True, download_name=f"{event.slug}.zip", conditional=False)
        response.call_on_close(spool.close)
        return response
    except Exception:
        spool.close()
        raise
