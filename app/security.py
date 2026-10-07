import re
import unicodedata
from functools import wraps
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from flask import abort, has_request_context, request
from flask_login import current_user

from .extensions import db
from .models import AuditLog, Event


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFD", value.lower()) if not unicodedata.combining(c)).strip()


def slugify(value):
    return re.sub(r"[^a-z0-9]+", "-", normalize(value)).strip("-")[:180]


def safe_url(value):
    value = value.strip()
    if value:
        parts = urlsplit(value)
        if len(value) > 2048 or parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("La fuente debe ser una URL http o https válida.")
    return value


def valid_email(value):
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError("Ingresá un correo válido.") from exc


def validate_user(username, email, password, role, unit):
    if not 3 <= len(username) <= 64 or role not in {"admin", "superadmin"}:
        raise ValueError("Nombre de 3 a 64 caracteres y rol válido obligatorios.")
    if not 12 <= len(password) <= 256:
        raise ValueError("La contraseña debe tener entre 12 y 256 caracteres.")
    if role == "admin" and not unit:
        raise ValueError("Los administradores requieren una unidad existente.")
    return valid_email(email)


def superadmin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_superadmin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def accessible_events():
    query = Event.query
    return query if current_user.is_superadmin else query.filter_by(unit_id=current_user.unit_id)


def managed_event(event_id, lock=False):
    query = accessible_events().filter_by(id=event_id)
    event = (query.with_for_update() if lock else query).first()
    if not event:
        abort(404)
    return event


def audit(action, details="", user=None):
    actor = user or (current_user if has_request_context() and current_user.is_authenticated else None)
    db.session.add(AuditLog(user_id=actor.id if actor else None, action=action,
                            details=details[:4000], ip_address=request.remote_addr if has_request_context() else None))
