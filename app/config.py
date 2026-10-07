import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"true", "1", "yes", "on"}


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")
    WTF_CSRF_SECRET_KEY = os.getenv("WTF_CSRF_SECRET_KEY") or SECRET_KEY
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 1800}
    MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT")
    MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY")
    MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY")
    MINIO_BUCKET = os.getenv("MINIO_BUCKET", "lavalleja-medios")
    MINIO_SECURE = env_bool("MINIO_SECURE", True)
    MAIL_SERVER = os.getenv("MAIL_SERVER")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = env_bool("MAIL_USE_TLS", True)
    MAIL_USE_SSL = env_bool("MAIL_USE_SSL", False)
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "Sala de medios <medios@lavalleja.uy>")
    RATELIMIT_STORAGE_URI = os.getenv("REDIS_URL", "memory://")
    SESSION_COOKIE_NAME = "lavalleja_medios_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.getenv("SESSION_HOURS", "8")))
    TWO_FACTOR_TTL_MINUTES = int(os.getenv("TWO_FACTOR_TTL_MINUTES", "10"))
    TWO_FACTOR_MAX_ATTEMPTS = int(os.getenv("TWO_FACTOR_MAX_ATTEMPTS", "5"))
    CAPTCHA_ENABLED = True
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024**2)))
    MAX_FORM_MEMORY_SIZE = 2 * 1024**2
    MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(25 * 1024**2)))
    MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", "40000000"))
    MAX_BATCH_IMAGES = 30
    MAX_ZIP_BYTES = int(os.getenv("MAX_ZIP_BYTES", str(500 * 1024**2)))
    MAX_ZIP_PHOTOS = 200
    TRUSTED_PROXY_COUNT = int(os.getenv("TRUSTED_PROXY_COUNT", "1"))
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
    CORS_ORIGINS = tuple(origin.strip().rstrip("/") for origin in os.getenv("CORS_ORIGINS", PUBLIC_BASE_URL).split(",") if origin.strip())
    APP_NAME = "Sala de medios · Lavalleja"


class DevelopmentConfig(Config):
    SESSION_COOKIE_SECURE = False


class ProductionConfig(Config):
    pass


class TestingConfig(Config):
    TESTING = True
    SECRET_KEY = "test-only-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    SESSION_COOKIE_SECURE = False
    TRUSTED_PROXY_COUNT = 0


CONFIGS = {"default": DevelopmentConfig, "development": DevelopmentConfig,
           "production": ProductionConfig, "testing": TestingConfig}
