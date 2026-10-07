import io
import warnings
from dataclasses import dataclass

import urllib3
from flask import current_app
from minio import Minio
from PIL import Image, ImageOps, UnidentifiedImageError


class StorageError(RuntimeError):
    pass


@dataclass
class ValidatedImage:
    data: bytes
    preview: bytes
    width: int
    height: int
    content_type: str
    extension: str


def validate_image(stream):
    data = stream.read(current_app.config["MAX_IMAGE_BYTES"] + 1)
    if not data or len(data) > current_app.config["MAX_IMAGE_BYTES"]:
        raise ValueError("La imagen está vacía o supera el límite por archivo.")
    formats = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in formats or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Solo se admiten imágenes estáticas JPEG, PNG y WebP.")
                if image.width * image.height > current_app.config["MAX_IMAGE_PIXELS"]:
                    raise ValueError("La imagen supera el límite de píxeles.")
                mime, extension = formats[image.format]
                image.verify()
            with Image.open(io.BytesIO(data)) as original:
                image = ImageOps.exif_transpose(original)
                width, height = image.size
                image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
                preview = io.BytesIO()
                image.convert("RGBA" if "A" in image.getbands() else "RGB").save(preview, "WEBP", quality=85)
                return ValidatedImage(data, preview.getvalue(), width, height, mime, extension)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("El archivo no es una imagen válida o segura.") from exc


class MinioStorage:
    def __init__(self, app):
        self.bucket = app.config["MINIO_BUCKET"]
        required = ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
        self.client = None
        if all(app.config.get(key) for key in required):
            self.client = Minio(app.config["MINIO_ENDPOINT"], access_key=app.config["MINIO_ACCESS_KEY"],
                                secret_key=app.config["MINIO_SECRET_KEY"], secure=app.config["MINIO_SECURE"],
                                http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=60), retries=2))
        elif not app.testing:
            raise RuntimeError("Configuración de MinIO incompleta.")

    def ensure_bucket(self):
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
            # Refuse an existing bucket with a public policy; never change another app's policy.
            try:
                policy = self.client.get_bucket_policy(self.bucket)
            except Exception as exc:
                if getattr(exc, "code", None) != "NoSuchBucketPolicy":
                    raise
            else:
                if policy:
                    raise StorageError("El bucket debe ser privado, sin política pública. Usá un bucket dedicado.")
        except Exception as exc:
            raise StorageError("No se pudo inicializar el bucket privado.") from exc

    def put(self, key, data, content_type):
        try:
            self.client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)
        except Exception as exc:
            raise StorageError("No se pudo subir la imagen a MinIO.") from exc

    def open(self, key, offset=0, length=0):
        try:
            return self.client.get_object(self.bucket, key, offset=offset, length=length)
        except Exception as exc:
            raise StorageError("No se pudo leer la imagen de MinIO.") from exc

    def delete(self, key):
        try:
            self.client.remove_object(self.bucket, key)
        except Exception as exc:
            raise StorageError("No se pudo eliminar el objeto de MinIO.") from exc

    def healthcheck(self):
        try:
            if not self.client.bucket_exists(self.bucket):
                raise StorageError("Bucket inexistente.")
        except Exception as exc:
            raise StorageError("MinIO no está disponible.") from exc


def get_storage():
    return current_app.extensions["media_storage"]
