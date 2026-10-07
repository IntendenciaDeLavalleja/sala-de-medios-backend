from datetime import date, datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask_login import UserMixin

from .extensions import db

ph = PasswordHasher()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Unit(TimestampMixin, db.Model):
    __tablename__ = "units"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    quota_bytes = db.Column(db.BigInteger, nullable=False, default=20 * 1024**3)


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="admin")
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    last_login_at = db.Column(db.DateTime)
    unit = db.relationship("Unit")
    __table_args__ = (
        db.CheckConstraint("role IN ('admin','superadmin')", name="ck_user_role"),
        db.CheckConstraint("role = 'superadmin' OR unit_id IS NOT NULL", name="ck_user_unit"),
    )

    @property
    def is_superadmin(self):
        return self.role == "superadmin"

    def get_id(self):
        return f"{self.id}:{self.session_version}"

    def set_password(self, password):
        self.password_hash = ph.hash(password)
        self.session_version = (self.session_version or 0) + 1

    def check_password(self, password):
        try:
            return ph.verify(self.password_hash, password)
        except (VerificationError, InvalidHashError):
            return False


class TwoFactorCode(db.Model):
    __tablename__ = "two_factor_codes"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_version = db.Column(db.Integer, nullable=False)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    consumed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    @classmethod
    def issue(cls, user, code, ttl_minutes=10):
        return cls(user_id=user.id, session_version=user.session_version, code_hash=ph.hash(code),
                   expires_at=utcnow() + timedelta(minutes=ttl_minutes))

    def verify(self, code, max_attempts=5):
        if self.consumed_at or self.expires_at <= utcnow() or self.attempts >= max_attempts:
            return False
        self.attempts += 1
        try:
            valid = ph.verify(self.code_hash, code)
        except (VerificationError, InvalidHashError):
            valid = False
        if valid:
            self.consumed_at = utcnow()
        return valid


class Category(TimestampMixin, db.Model):
    __tablename__ = "categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)


class Event(TimestampMixin, db.Model):
    __tablename__ = "events"
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(180), unique=True, nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    summary = db.Column(db.String(500), nullable=False, default="")
    description = db.Column(db.Text, nullable=False, default="")
    event_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    location = db.Column(db.String(255), nullable=False, default="")
    source_url = db.Column(db.String(2048), nullable=False, default="")
    usage_terms = db.Column(db.Text, nullable=False, default="Citar a la Intendencia de Lavalleja y los créditos de cada fotografía.")
    credits = db.Column(db.String(255), nullable=False, default="Intendencia de Lavalleja")
    cover_position = db.Column(db.String(20), nullable=False, default="center")
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    published = db.Column(db.Boolean, nullable=False, default=False, index=True)
    published_at = db.Column(db.DateTime)
    search_text = db.Column(db.Text, nullable=False, default="")
    category = db.relationship("Category")
    unit = db.relationship("Unit")
    photos = db.relationship("Photo", back_populates="event", cascade="all, delete-orphan",
                             order_by="(Photo.position, Photo.id)", lazy="selectin")


class Photo(TimestampMixin, db.Model):
    __tablename__ = "photos"
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    object_key = db.Column(db.String(255), nullable=False, unique=True)
    preview_key = db.Column(db.String(255), nullable=False, unique=True)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(50), nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    size_bytes = db.Column(db.BigInteger, nullable=False)
    preview_bytes = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    alt = db.Column(db.String(500), nullable=False)
    credits = db.Column(db.String(255), nullable=False, default="")
    source_url = db.Column(db.String(2048), nullable=False, default="")
    position = db.Column(db.Integer, nullable=False, default=0)
    is_cover = db.Column(db.Boolean, nullable=False, default=False)
    event = db.relationship("Event", back_populates="photos")


class ObjectCleanup(db.Model):
    """Persistent compensation for deletes and uploads interrupted before DB commit."""
    __tablename__ = "object_cleanup"
    id = db.Column(db.Integer, primary_key=True)
    object_key = db.Column(db.String(255), nullable=False, unique=True)
    not_before = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(64), nullable=False)
    details = db.Column(db.Text, nullable=False, default="")
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    user = db.relationship("User")
