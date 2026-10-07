import base64
import io
import json
from pathlib import Path

import click
from flask.cli import with_appcontext
from flask_migrate import upgrade
from werkzeug.datastructures import FileStorage

from .extensions import db
from .media import cleanup_storage, upload_images
from .models import Category, Event, Unit, User, utcnow
from .security import normalize, safe_url, validate_user
from .storage import get_storage


@click.command("create-admin")
@click.argument("username")
@click.argument("email")
@click.argument("password")
@click.argument("is_superuser", default="false")
@click.argument("unit_name", required=False)
@with_appcontext
def create_admin(username, email, password, is_superuser, unit_name):
    """Crea un admin/superadmin; el admin sin unidad explícita usa Comunicación."""
    upgrade()
    if is_superuser.lower() not in {"true", "false"}:
        raise click.ClickException("El cuarto argumento debe ser true o false.")
    role = "superadmin" if is_superuser.lower() == "true" else "admin"
    unit = None
    if role == "admin":
        unit = Unit.query.filter_by(name=unit_name or "Comunicación").first()
        if not unit:
            raise click.ClickException(
                f"La unidad '{unit_name or 'Comunicación'}' no existe. Ejecutá seed-data primero."
            )
    try:
        email = validate_user(username.strip(), email, password, role, unit)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if User.query.filter((User.email == email) | (User.username == username.strip())).first():
        raise click.ClickException("El nombre o correo ya existe.")
    user = User(username=username.strip(), email=email, role=role, unit_id=unit.id if role == "admin" else None)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"{'Super Admin' if user.is_superadmin else 'Admin'} {user.username} creado correctamente.")


@click.command("seed-data")
@with_appcontext
def seed_data():
    """Solo crea categorías y unidad iniciales. Nunca crea cuentas ni publica fotos."""
    upgrade()
    if not Unit.query.filter_by(name="Comunicación").first():
        db.session.add(Unit(name="Comunicación", quota_bytes=20 * 1024**3))
    for name in ("Obras", "Comunidad", "Cultura", "Deporte", "Turismo", "Institucional"):
        if not Category.query.filter_by(name=name).first():
            db.session.add(Category(name=name))
    db.session.commit()
    click.echo("Datos base listos.")


@click.command("init-storage")
@with_appcontext
def init_storage():
    get_storage().ensure_bucket()
    click.echo("Bucket privado listo.")


@click.command("cleanup-storage")
@with_appcontext
def cleanup_command():
    removed, failed = cleanup_storage()
    click.echo(f"Objetos procesados: {removed}; pendientes por error: {failed}.")
    if failed:
        raise click.ClickException("Hay objetos pendientes; se reintentará en la próxima ejecución.")


@click.command("import-prototype")
@click.option("--file", "path", type=click.Path(exists=True), default="data/prototype.json")
@click.option("--admin-email", required=True)
@click.option("--publish", is_flag=True, help="Publica explícitamente los álbumes importados.")
@with_appcontext
def import_prototype(path, admin_email, publish):
    """Importa las fotos del HTML a MinIO, conservando fuentes y textos. Idempotente."""
    user = User.query.filter_by(email=admin_email.lower(), is_active=True).first()
    if not user or not user.is_superadmin:
        raise click.ClickException("Se requiere un superadmin activo.")
    unit = Unit.query.filter_by(name="Comunicación").first()
    if not unit:
        raise click.ClickException("Ejecutá seed-data primero.")
    get_storage().ensure_bucket()
    for record in json.loads(Path(path).read_text(encoding="utf-8")):
        if Event.query.filter_by(slug=record["id"]).first():
            click.echo(f"Omitido (ya existe): {record['id']}")
            continue
        category = Category.query.filter_by(name=record["category"]).one()
        event = Event(slug=record["id"], title=record["title"], summary=record["summary"],
                      description=record["description"], event_date=__import__("datetime").date.fromisoformat(record["date"]),
                      category_id=category.id, unit_id=unit.id, created_by_id=user.id,
                      source_url=safe_url(record["source"]), location=record["location"],
                      search_text=normalize(" ".join((record["title"], record["summary"], record["description"], record["location"], record["category"]))))
        db.session.add(event)
        db.session.commit()
        try:
            files = [FileStorage(stream=io.BytesIO(base64.b64decode(p["src"].split(",", 1)[1], validate=True)), filename=f"{record['id']}-{i+1}.jpg") for i, p in enumerate(record["photos"])]
            photos = upload_images(event, files, [{"alt": p["alt"], "source_url": safe_url(p["source"])} for p in record["photos"]])
        except Exception:
            db.session.rollback()
            db.session.delete(event)
            db.session.commit()
            raise
        for i, photo in enumerate(photos):
            photo.is_cover = i == record.get("cover", 0)
        event.published, event.published_at = publish, utcnow() if publish else None
        db.session.commit()
        click.echo(f"Importado: {event.slug} ({len(photos)} fotos, {'publicado' if publish else 'borrador'})")


def register_commands(app):
    for command in (create_admin, seed_data, init_storage, cleanup_command, import_prototype):
        app.cli.add_command(command)
