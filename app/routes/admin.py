from datetime import date

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from ..extensions import db, limiter
from ..media import photo_response, queue_photo_delete, unit_usage, upload_images
from ..storage import validate_image
from ..models import AuditLog, Category, Event, ObjectCleanup, Photo, Unit, User, utcnow
from ..security import accessible_events, audit, managed_event, normalize, safe_url, slugify, superadmin_required, validate_user

bp = Blueprint("admin", __name__)


def form_text(name, max_length, required=False):
    value = request.form.get(name, "").strip()
    if len(value) > max_length or (required and not value):
        raise ValueError(f"Revisá el campo {name}: máximo {max_length} caracteres.")
    return value


def populate_event(event):
    event.title = form_text("title", 255, True)
    slug = slugify(form_text("slug", 180) or event.title)
    if not slug:
        raise ValueError("El enlace del evento no es válido.")
    duplicate = Event.query.filter(Event.slug == slug, Event.id != (event.id or 0)).first()
    if duplicate:
        raise ValueError("El enlace ya está en uso por otro evento.")
    category = db.session.get(Category, request.form.get("category_id", type=int))
    unit_id = request.form.get("unit_id", type=int) if current_user.is_superadmin else current_user.unit_id
    unit = db.session.get(Unit, unit_id) if unit_id else None
    if not category or not unit:
        raise ValueError("Seleccioná una categoría y una unidad existentes.")
    if event.id and event.unit_id != unit.id and event.photos:
        raise ValueError("No se puede cambiar la unidad de un evento con fotografías.")
    event.slug, event.category_id, event.unit_id = slug, category.id, unit.id
    try:
        event.event_date = date.fromisoformat(request.form.get("event_date", ""))
    except ValueError as exc:
        raise ValueError("La fecha del evento no es válida.") from exc
    event.summary = form_text("summary", 500, True)
    event.description = form_text("description", 20000, True)
    event.location = form_text("location", 255, True)
    event.credits = form_text("credits", 255, True)
    event.usage_terms = form_text("usage_terms", 4000, True)
    event.source_url = safe_url(request.form.get("source_url", ""))
    event.cover_position = request.form.get("cover_position", "center")
    if event.cover_position not in {"center", "top", "bottom", "left", "right"}:
        raise ValueError("Posición de portada inválida.")
    event.search_text = normalize(" ".join((event.title, event.summary, event.description, event.location, category.name)))


@bp.get("/admin/")
@bp.get("/admin/dashboard")
def dashboard():
    query = accessible_events()
    units = Unit.query.all() if current_user.is_superadmin else Unit.query.filter_by(id=current_user.unit_id).all()
    return render_template("dashboard.html", total=query.count(), published=query.filter_by(published=True).count(),
                           photos=Photo.query.join(Event).filter(Event.id.in_(query.with_entities(Event.id))).count(),
                           units=[(unit, unit_usage(unit.id)) for unit in units],
                           cleanup_count=ObjectCleanup.query.count() if current_user.is_superadmin else None,
                           events=query.order_by(Event.updated_at.desc()).limit(5).all())


@bp.get("/admin/events")
def events():
    query = accessible_events()
    if request.args.get("q"):
        query = query.filter(Event.search_text.contains(normalize(request.args["q"][:200]), autoescape=True))
    pagination = query.order_by(Event.event_date.desc(), Event.id.desc()).paginate(page=max(1, request.args.get("page", 1, type=int)), per_page=20, error_out=False)
    return render_template("events.html", pagination=pagination)


@bp.route("/admin/events/new", methods=["GET", "POST"])
def new_event():
    event = Event(created_by_id=current_user.id, unit_id=current_user.unit_id,
                  credits="Intendencia de Lavalleja", event_date=date.today(),
                  usage_terms="Citar a la Intendencia de Lavalleja y los créditos de cada fotografía.")
    if request.method == "POST":
        try:
            populate_event(event)
            db.session.add(event)
            db.session.flush()
            audit("EVENT_CREATE", f"Evento {event.id}: {event.title}")
            db.session.commit()
            flash("Borrador creado. Agregá fotografías antes de publicar.", "success")
            return redirect(url_for("admin.edit_event", event_id=event.id))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "El enlace ya está en uso.", "error")
    return render_template("event_form.html", event=event, categories=Category.query.order_by(Category.name).all(),
                           units=Unit.query.order_by(Unit.name).all())


@bp.route("/admin/events/<int:event_id>", methods=["GET", "POST"])
def edit_event(event_id):
    event = managed_event(event_id, lock=request.method == "POST")
    if request.method == "POST":
        try:
            populate_event(event)
            audit("EVENT_UPDATE", f"Evento {event.id}: {event.title}")
            db.session.commit()
            flash("Evento actualizado.", "success")
            return redirect(url_for("admin.edit_event", event_id=event.id))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "El enlace ya está en uso.", "error")
    return render_template("event_form.html", event=event, categories=Category.query.order_by(Category.name).all(),
                           units=Unit.query.order_by(Unit.name).all())


@bp.post("/admin/events/<int:event_id>/publish")
def publish(event_id):
    event = managed_event(event_id, lock=True)
    if not event.published and (not event.photos or not event.usage_terms or not event.description):
        flash("El evento requiere fotografías, descripción y condiciones de uso para publicarse.", "error")
    else:
        event.published = not event.published
        event.published_at = utcnow() if event.published else None
        audit("EVENT_PUBLISH" if event.published else "EVENT_UNPUBLISH", f"Evento {event.id}")
        db.session.commit()
        flash("Evento publicado." if event.published else "Evento retirado del archivo público.", "success")
    return redirect(url_for("admin.edit_event", event_id=event.id))


@bp.post("/admin/events/<int:event_id>/delete")
def delete_event(event_id):
    event = managed_event(event_id, lock=True)
    for photo in event.photos:
        queue_photo_delete(photo)
    audit("EVENT_DELETE", f"Evento {event.id}: {event.title}")
    db.session.delete(event)
    db.session.commit()
    flash("Evento eliminado. La limpieza de sus objetos quedó programada.", "success")
    return redirect(url_for("admin.events"))


@bp.post("/admin/events/<int:event_id>/photos")
@limiter.limit("20 per minute")
def upload_photos(event_id):
    event = managed_event(event_id)
    files = [file for file in request.files.getlist("photos") if file.filename]
    alts = request.form.getlist("alt")
    if alts and len(alts) != len(files):
        return {"error": "Los textos alternativos no coinciden con los archivos."}, 400
    try:
        photos = upload_images(event, files, [{"alt": alts[i] if alts else "", "credits": ""} for i in range(len(files))])
    except ValueError as exc:
        return {"error": str(exc)}, 400
    return {"uploaded": len(photos), "redirect": url_for("admin.edit_event", event_id=event.id)}, 201


@bp.post("/admin/events/<int:event_id>/photos/preview")
@limiter.limit("60 per minute")
def preview_photo(event_id):
    """Decode browser-incompatible inputs for preview, without writing to MinIO."""
    managed_event(event_id)
    file = request.files.get("photo")
    if not file or not file.filename:
        return {"error": "Seleccioná una fotografía para previsualizar."}, 400
    try:
        image = validate_image(file.stream)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    return Response(image.preview, mimetype="image/webp")


@bp.get("/admin/photos/<int:photo_id>/content")
def photo_content(photo_id):
    photo = db.session.get(Photo, photo_id)
    if not photo:
        abort(404)
    managed_event(photo.event_id)
    return photo_response(photo)


@bp.post("/admin/photos/<int:photo_id>/update")
def update_photo(photo_id):
    photo = db.session.get(Photo, photo_id)
    if not photo:
        abort(404)
    event = managed_event(photo.event_id, lock=True)
    try:
        photo.alt = form_text("alt", 500, True)
        photo.credits = form_text("credits", 255)
        photo.source_url = safe_url(request.form.get("source_url", ""))
        position = request.form.get("position", type=int)
        if position is None or position < 0 or position > 10000:
            raise ValueError("El orden debe ser un número entre 0 y 10000.")
        photo.position = position
        if request.form.get("is_cover") == "on":
            Photo.query.filter_by(event_id=event.id).update({"is_cover": False}, synchronize_session="fetch")
            photo.is_cover = True
        audit("PHOTO_UPDATE", f"Fotografía {photo.id}")
        db.session.commit()
        flash("Fotografía actualizada.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.edit_event", event_id=event.id, _anchor="gallery"))


@bp.post("/admin/photos/<int:photo_id>/delete")
def delete_photo(photo_id):
    photo = db.session.get(Photo, photo_id)
    if not photo:
        abort(404)
    event = managed_event(photo.event_id, lock=True)
    queue_photo_delete(photo)
    cover = photo.is_cover
    audit("PHOTO_DELETE", f"Fotografía {photo.id}, evento {event.id}")
    db.session.delete(photo)
    db.session.flush()
    remaining = Photo.query.filter_by(event_id=event.id).order_by(Photo.position, Photo.id).all()
    if not remaining:
        event.published = False
        event.published_at = None
    elif cover:
        remaining[0].is_cover = True
    db.session.commit()
    flash("Fotografía eliminada.", "success")
    return redirect(url_for("admin.edit_event", event_id=event.id, _anchor="gallery"))


@bp.route("/admin/categories", methods=["GET", "POST"])
@superadmin_required
def categories():
    if request.method == "POST":
        try:
            name = form_text("name", 100, True)
            category_id = request.form.get("category_id", type=int)
            category = db.session.get(Category, category_id) if category_id else Category()
            if not category:
                abort(404)
            category.name = name
            db.session.add(category)
            for event in Event.query.filter_by(category_id=category_id).all() if category_id else []:
                event.search_text = normalize(" ".join((event.title, event.summary, event.description, event.location, name)))
            audit("CATEGORY_SAVE", name)
            db.session.commit()
            flash("Categoría guardada.", "success")
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "La categoría ya existe.", "error")
        return redirect(url_for("admin.categories"))
    return render_template("categories.html", categories=Category.query.order_by(Category.name).all())


@bp.post("/admin/categories/<int:category_id>/delete")
@superadmin_required
def delete_category(category_id):
    category = db.session.get(Category, category_id)
    if not category:
        abort(404)
    if Event.query.filter_by(category_id=category.id).first():
        flash("La categoría tiene eventos. Reasignalos antes de eliminarla.", "error")
    else:
        audit("CATEGORY_DELETE", category.name)
        db.session.delete(category)
        db.session.commit()
        flash("Categoría eliminada.", "success")
    return redirect(url_for("admin.categories"))


@bp.route("/admin/units", methods=["GET", "POST"])
@superadmin_required
def units():
    if request.method == "POST":
        try:
            name = form_text("name", 255, True)
            quota = request.form.get("quota_gb", type=float)
            if quota is None or not 0.1 <= quota <= 100000:
                raise ValueError("La cuota debe estar entre 0,1 y 100000 GB.")
            unit_id = request.form.get("unit_id", type=int)
            unit = Unit.query.filter_by(id=unit_id).with_for_update().first() if unit_id else Unit()
            if not unit:
                abort(404)
            if unit.id and unit_usage(unit.id) > int(quota * 1024**3):
                raise ValueError("La cuota no puede ser menor que el espacio utilizado.")
            unit.name, unit.quota_bytes = name, int(quota * 1024**3)
            db.session.add(unit)
            audit("UNIT_SAVE", name)
            db.session.commit()
            flash("Unidad guardada.", "success")
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "La unidad ya existe.", "error")
        return redirect(url_for("admin.units"))
    return render_template("units.html", units=[(unit, unit_usage(unit.id)) for unit in Unit.query.order_by(Unit.name).all()])


@bp.route("/admin/users", methods=["GET", "POST"])
@superadmin_required
def users():
    if request.method == "POST":
        try:
            username = form_text("username", 64, True)
            email = form_text("email", 255, True)
            password = request.form.get("password", "")
            role = request.form.get("role", "admin")
            unit = db.session.get(Unit, request.form.get("unit_id", type=int)) if request.form.get("unit_id") else None
            email = validate_user(username, email, password, role, unit)
            user = User(username=username, email=email, role=role, unit_id=unit.id if role == "admin" else None)
            user.set_password(password)
            db.session.add(user)
            audit("USER_CREATE", f"{username}: {role}")
            db.session.commit()
            flash("Usuario creado. Su acceso requiere código por correo.", "success")
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "El nombre o correo ya está en uso.", "error")
        return redirect(url_for("admin.users"))
    return render_template("users.html", users=User.query.order_by(User.username).all(), units=Unit.query.order_by(Unit.name).all())


@bp.post("/admin/users/<int:user_id>/update")
@superadmin_required
def update_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    try:
        role = request.form.get("role", user.role)
        active = request.form.get("is_active") == "on"
        unit = db.session.get(Unit, request.form.get("unit_id", type=int)) if request.form.get("unit_id") else None
        if role not in {"admin", "superadmin"} or (role == "admin" and not unit):
            raise ValueError("Revisá el rol y la unidad del usuario.")
        if user.id == current_user.id and (not active or role != "superadmin"):
            raise ValueError("No podés quitarte tus privilegios ni desactivar tu cuenta.")
        password = request.form.get("password", "")
        if password and not 12 <= len(password) <= 256:
            raise ValueError("La contraseña debe tener entre 12 y 256 caracteres.")
        user.role, user.is_active, user.unit_id = role, active, unit.id if role == "admin" else None
        if password:
            user.set_password(password)
        else:
            user.session_version += 1
        audit("USER_UPDATE", f"{user.username}: {role}, activo={active}")
        db.session.commit()
        flash("Usuario actualizado; sus sesiones anteriores fueron revocadas.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.users"))


@bp.get("/admin/audit")
@superadmin_required
def audit_logs():
    pagination = AuditLog.query.order_by(AuditLog.id.desc()).paginate(page=max(1, request.args.get("page", 1, type=int)), per_page=50, error_out=False)
    return render_template("audit.html", pagination=pagination)
