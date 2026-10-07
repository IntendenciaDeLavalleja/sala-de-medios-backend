import secrets
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user
from flask_mail import Message

from ..extensions import db, limiter, mail
from ..models import TwoFactorCode, User, utcnow
from ..security import audit, valid_email

bp = Blueprint("auth", __name__)


def safe_next(value):
    if not value or "\\" in value or any(ord(c) < 32 for c in value):
        return None
    parts = urlsplit(value)
    return value if value.startswith("/admin/") and not parts.netloc and not parts.scheme else None


def new_captcha():
    first, second = secrets.randbelow(10) + 1, secrets.randbelow(10) + 1
    session["captcha_result"] = first + second
    session["captcha_question"] = f"¿Cuánto es {first} + {second}?"
    return session["captcha_question"]


def send_code(user, code):
    if current_app.testing:
        current_app.extensions.setdefault("outbox", []).append({"to": user.email, "code": code})
        return
    message = Message(subject="Código de acceso · Sala de medios", recipients=[user.email],
                      body=render_template("emails/2fa.txt", code=code),
                      html=render_template("emails/2fa.html", code=code))
    mail.send(message)


@bp.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))
    next_url = safe_next(request.form.get("next") or request.args.get("next"))
    if request.method == "POST":
        expected = session.pop("captcha_result", None)
        session.pop("captcha_question", None)
        if expected is None or request.form.get("captcha", "").strip() != str(expected):
            flash("CAPTCHA incorrecto. Intentá nuevamente.", "error")
        else:
            user = User.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
            password = request.form.get("password", "")
            if user and user.is_active and len(password) <= 256 and user.check_password(password):
                # Lock the user so simultaneous logins invalidate older challenges consistently.
                db.session.refresh(user, with_for_update=True)
                TwoFactorCode.query.filter_by(user_id=user.id, consumed_at=None).update({"consumed_at": utcnow()})
                code = f"{secrets.randbelow(1_000_000):06d}"
                challenge = TwoFactorCode.issue(user, code, current_app.config["TWO_FACTOR_TTL_MINUTES"])
                db.session.add(challenge)
                db.session.commit()
                try:
                    send_code(user, code)
                except Exception:
                    current_app.logger.exception("No se pudo enviar el código 2FA")
                    challenge.consumed_at = utcnow()
                    db.session.commit()
                    flash("No fue posible enviar el código. Contactá al administrador.", "error")
                else:
                    session.clear()
                    session["pending_user_id"] = user.id
                    session["pending_code_id"] = challenge.id
                    session["pending_next"] = next_url
                    return redirect(url_for("auth.verify_2fa"))
            else:
                audit("LOGIN_FAILED", "Credenciales rechazadas")
                db.session.commit()
                flash("Correo o contraseña incorrectos.", "error")
        new_captcha()
    elif not session.get("captcha_question"):
        new_captcha()
    return render_template("login.html", captcha_question=session["captcha_question"], next_url=next_url)


@bp.route("/admin/2fa", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def verify_2fa():
    user = db.session.get(User, session.get("pending_user_id")) if session.get("pending_user_id") else None
    challenge = TwoFactorCode.query.filter_by(id=session.get("pending_code_id")).with_for_update().first()
    if not user or not user.is_active or not challenge or challenge.user_id != user.id or challenge.session_version != user.session_version:
        session.clear()
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if challenge.verify(request.form.get("code", "").strip(), current_app.config["TWO_FACTOR_MAX_ATTEMPTS"]):
            user.last_login_at = utcnow()
            audit("LOGIN", "Acceso con CAPTCHA y 2FA", user=user)
            db.session.commit()
            next_url = safe_next(session.get("pending_next"))
            session.clear()
            login_user(user)
            session.permanent = True
            return redirect(next_url or url_for("admin.dashboard"))
        db.session.commit()
        flash("Código incorrecto, vencido o sin intentos disponibles.", "error")
    return render_template("verify_2fa.html", email=user.email)


@bp.post("/admin/logout")
def logout():
    audit("LOGOUT")
    db.session.commit()
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/admin/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        try:
            username = request.form.get("username", "").strip()
            email = valid_email(request.form.get("email", ""))
            if not 3 <= len(username) <= 64:
                raise ValueError("El nombre debe tener de 3 a 64 caracteres.")
            if not current_user.check_password(request.form.get("current_password", "")):
                raise ValueError("La contraseña actual es incorrecta.")
            password = request.form.get("password", "")
            if password and not 12 <= len(password) <= 256:
                raise ValueError("La nueva contraseña debe tener de 12 a 256 caracteres.")
            if User.query.filter(User.id != current_user.id, (User.username == username) | (User.email == email)).first():
                raise ValueError("El nombre o correo ya está en uso.")
            current_user.username, current_user.email = username, email
            if password:
                current_user.set_password(password)
            else:
                current_user.session_version += 1
            audit("PROFILE_UPDATE")
            db.session.commit()
            logout_user()
            session.clear()
            flash("Perfil actualizado. Iniciá sesión con tus datos nuevos.", "success")
            return redirect(url_for("auth.login"))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("profile.html")
