import io
import math
import warnings
from dataclasses import dataclass
from threading import BoundedSemaphore

import pyvips
import urllib3
from flask import current_app
from minio import Minio
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

# Decode the full HEIC/HEIF image instead of a potentially smaller embedded thumbnail.
register_heif_opener(thumbnails=False)
UPLOAD_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP", "DIB", "TIFF", "AVIF", "HEIF", "JPEG2000", "TGA", "ICO")
STREAMING_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "GIF", "TIFF", "AVIF"})
WEBP_DIMENSION_LIMIT = 16383
MAX_DECODE_PIXELS = 16384**2
PREVIEW_MAX_BYTES = 512 * 1024
# The custom check below runs before decoding. Avoid Pillow's lower default rejecting 16K headers.
Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS
# Keep upload buffers out of libvips' global operation cache and bound simultaneous decodes per worker.
pyvips.cache_set_max(0)
image_processing_slot = BoundedSemaphore(1)


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


def encode_webp(image, quality):
    output = io.BytesIO()
    image.save(output, "WEBP", quality=quality, method=4, exact=True)
    return output.getvalue()


def normalize_image(image):
    image = ImageOps.exif_transpose(image)
    if image.mode in {"I;16", "I;16B", "I;16L"}:
        image = image.convert("I").point(lambda value: value / 257).convert("L")
    transparent = "A" in image.getbands() or "transparency" in image.info
    mode = "RGBA" if transparent else "RGB"
    profile = image.info.get("icc_profile")
    if profile:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            color_input = image if image.mode in {"RGB", "RGBA", "CMYK", "L", "LAB"} else image.convert(mode)
            image = ImageCms.profileToProfile(color_input, source, ImageCms.createProfile("sRGB"), outputMode=mode)
        except (ImageCms.PyCMSError, OSError, ValueError):
            image = image.convert(mode)
    else:
        image = image.convert(mode)
    # Re-encoding strips EXIF/XMP and other embedded source metadata.
    image.info.clear()
    return image


def image_within_bounds(data, source):
    max_height = current_app.config["WEBP_MAX_HEIGHT"]
    if source.format in STREAMING_IMAGE_FORMATS:
        # Streaming/shrink-on-load avoids a full-size RGB copy for 16K JPEG/PNG/TIFF/etc.
        # thumbnail_buffer corrects orientation, preserves aspect ratio and never enlarges.
        reduced = pyvips.Image.thumbnail_buffer(data, WEBP_DIMENSION_LIMIT, height=max_height,
                                               size="down", fail_on="error", output_profile="srgb")
        with Image.open(io.BytesIO(reduced.pngsave_buffer(strip=True))) as image:
            image.load()
            return normalize_image(image)
    # HEVC and less common raster formats use Pillow's bundled decoders. Resize before
    # normalizing to avoid extra full-resolution copies; decoding itself can require more memory.
    source.seek(0)
    rotated = source.getexif().get(274) in {5, 6, 7, 8}
    bounds = (max_height, WEBP_DIMENSION_LIMIT) if rotated else (WEBP_DIMENSION_LIMIT, max_height)
    source.thumbnail(bounds, Image.Resampling.LANCZOS)
    image = normalize_image(source)
    image.thumbnail((WEBP_DIMENSION_LIMIT, max_height), Image.Resampling.LANCZOS)
    return image


def fit_webp(image, quality, max_bytes):
    converted = encode_webp(image, quality)
    while len(converted) > max_bytes and quality > 50:
        quality = max(50, quality - 10)
        converted = encode_webp(image, quality)
    source = image
    scale = 1.0
    while len(converted) > max_bytes:
        # Alpha/noise can remain large at quality 50. Reduce resolution until the byte cap is met.
        # Derive every size from the same source so successive rounding cannot distort its ratio.
        scale *= min(0.85, math.sqrt(max_bytes / len(converted)) * 0.9)
        size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        if image.size == (1, 1):
            raise ValueError("No pudimos convertir la imagen dentro del peso máximo configurado.")
        image = source.resize(size, Image.Resampling.LANCZOS)
        converted = encode_webp(image, quality)
    return image, converted, quality


def validate_image(stream):
    data = stream.read(current_app.config["MAX_IMAGE_BYTES"] + 1)
    if not data or len(data) > current_app.config["MAX_IMAGE_BYTES"]:
        raise ValueError("La imagen está vacía o supera el límite por archivo.")
    try:
        with image_processing_slot, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=UPLOAD_IMAGE_FORMATS) as image:
                if image.width * image.height > current_app.config["MAX_IMAGE_PIXELS"]:
                    raise ValueError("La imagen supera el límite de píxeles.")
                image.verify()
            with Image.open(io.BytesIO(data), formats=UPLOAD_IMAGE_FORMATS) as original:
                # Albums contain still photos; decoders select the first animation frame/page.
                image = image_within_bounds(data, original)
                max_bytes = current_app.config["WEBP_MAX_BYTES"]
                image, converted, quality = fit_webp(image, current_app.config["WEBP_QUALITY"], max_bytes)
                width, height = image.size
                preview_max_bytes = min(max_bytes, PREVIEW_MAX_BYTES)
                if max(image.size) <= 1200 and len(converted) <= preview_max_bytes:
                    preview = converted
                else:
                    image = image.copy()
                    image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
                    _, preview, _ = fit_webp(image, quality, preview_max_bytes)
                return ValidatedImage(converted, preview, width, height, "image/webp", ".webp")
    except (UnidentifiedImageError, OSError, pyvips.Error, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("No pudimos convertir el archivo: está dañado, supera los límites o su formato no es compatible. Usá JPEG, PNG, WebP, HEIC/HEIF, AVIF, TIFF, BMP o GIF.") from exc


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
