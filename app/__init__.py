import os
import uuid

from flask import Flask, g, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from redis import Redis
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import CONFIGS
from .cors import init_public_cors
from .extensions import csrf, db, limiter, login_manager, mail, migrate
from .models import User
from .storage import MinioStorage, StorageError

# Default-deny: every registered endpoint outside these exact names needs a session.
# Public photo routes additionally verify that their parent event is published.
PUBLIC_ENDPOINTS = frozenset({
    "root", "static", "health", "liveness", "auth.login", "auth.verify_2fa",
    "public.catalog", "public.categories", "public.stats", "public.event_detail",
    "public.photo_content", "public.album_download",
})


def create_app(config_name=None, overrides=None):
    app = Flask(__name__)
    app.config.from_object(CONFIGS[config_name or os.getenv("FLASK_CONFIG", "default")])
    if overrides:
        app.config.update(overrides)
    app.config["WTF_CSRF_SECRET_KEY"] = app.config.get("WTF_CSRF_SECRET_KEY") or app.config.get("SECRET_KEY")
    if not app.config.get("SECRET_KEY") or not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("SECRET_KEY y DATABASE_URL son obligatorias.")
    if (config_name or os.getenv("FLASK_CONFIG")) == "production" and not app.testing:
        if len(app.config["SECRET_KEY"]) < 32 or app.config["RATELIMIT_STORAGE_URI"] == "memory://":
            raise RuntimeError("Producción requiere SECRET_KEY de al menos 32 caracteres y Redis.")
    proxies = app.config["TRUSTED_PROXY_COUNT"]
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.session_protection = "strong"
    mail.init_app(app)
    limiter.init_app(app)
    app.extensions["media_storage"] = MinioStorage(app)

    @login_manager.user_loader
    def load_user(identifier):
        try:
            user_id, version = map(int, identifier.split(":"))
            user = db.session.get(User, user_id)
            return user if user and user.is_active and user.session_version == version else None
        except (TypeError, ValueError):
            return None

    @app.before_request
    def enforce_access():
        g.request_id = str(uuid.uuid4())
        if request.endpoint and request.endpoint not in PUBLIC_ENDPOINTS and not current_user.is_authenticated:
            if request.path.startswith("/api/"):
                return {"error": "Iniciá sesión para continuar."}, 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

    # Authorization runs before CSRF; authenticated mutations still require CSRF.
    csrf.init_app(app)

    @app.after_request
    def secure_headers(response):
        response.headers["X-Request-ID"] = g.get("request_id", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    from .routes.auth import bp as auth_bp
    from .routes.admin import bp as admin_bp
    from .routes.public import bp as public_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(public_bp)
    init_public_cors(app)

    from .commands import register_commands
    register_commands(app)

    @app.get("/")
    def root():
        return redirect(url_for("admin.dashboard"))

    @app.get("/health/live")
    def liveness():
        return {"status": "ok"}

    @app.get("/health")
    def health():
        try:
            db.session.execute(db.text("SELECT 1"))
            app.extensions["media_storage"].healthcheck()
            if app.config["RATELIMIT_STORAGE_URI"].startswith("redis"):
                Redis.from_url(app.config["RATELIMIT_STORAGE_URI"], socket_timeout=3).ping()
        except Exception:
            db.session.rollback()
            app.logger.exception("Falló la verificación de salud")
            return {"status": "unavailable"}, 503
        return {"status": "ok"}

    def error_response(message, status):
        if request.path.startswith("/api/") or request.path.startswith("/health"):
            return {"error": message}, status
        return render_template("errors.html", message=message, status=status), status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages = {400: "Solicitud inválida.", 403: "No tenés permiso para acceder.",
                    404: "No encontramos lo que buscabas.", 413: "La carga supera el tamaño máximo.",
                    429: "Demasiados intentos. Esperá un minuto y volvé a intentar."}
        return error_response(messages.get(exc.code, "No se pudo completar la solicitud."), exc.code)

    @app.errorhandler(CSRFError)
    def csrf_error(exc):
        return error_response("La sesión del formulario venció. Recargá la página e intentá nuevamente.", 400)

    @app.errorhandler(StorageError)
    def storage_error(exc):
        db.session.rollback()
        app.logger.exception("Error de almacenamiento")
        return error_response("MinIO no está disponible. Intentá nuevamente.", 503)

    @app.errorhandler(500)
    def internal_error(exc):
        db.session.rollback()
        return error_response("No se pudo completar la operación. Referencia: " + g.get("request_id", ""), 500)

    @app.template_filter("filesize")
    def filesize(value):
        return f"{value / 1024**2:.1f} MB" if value >= 1024**2 else f"{value / 1024:.0f} KB"

    return app
