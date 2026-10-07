import io
import random

import pyvips
import pytest
from PIL import Image

from app.extensions import db
from app.models import Photo, Unit
from app.storage import WEBP_DIMENSION_LIMIT, encode_webp, validate_image
from conftest import upload


def encoded_image(format, size=(64, 48), mode="RGB", color="green", **kwargs):
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, format, **kwargs)
    return output.getvalue()


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "AVIF", "HEIF", "JPEG2000", "TGA"])
def test_common_input_formats_are_always_converted_to_webp(app, format):
    source = encoded_image(format)
    result = validate_image(io.BytesIO(source))
    assert result.content_type == "image/webp" and result.extension == ".webp"
    with Image.open(io.BytesIO(result.data)) as stored:
        assert stored.format == "WEBP" and stored.size == (64, 48)
        assert getattr(stored, "n_frames", 1) == 1
    with Image.open(io.BytesIO(result.preview)) as preview:
        assert preview.format == "WEBP"


def test_large_image_is_reduced_without_upscaling_small_images(app):
    result = validate_image(io.BytesIO(encoded_image("BMP", size=(3000, 1500))))
    assert (result.width, result.height) == (2160, 1080)
    with Image.open(io.BytesIO(result.preview)) as preview:
        assert preview.size == (1200, 600)
    assert len(result.data) <= app.config["WEBP_MAX_BYTES"]


def test_wide_image_keeps_its_width_when_already_below_height_limit(app):
    result = validate_image(io.BytesIO(encoded_image("JPEG", size=(4000, 1000))))
    assert (result.width, result.height) == (4000, 1000)


@pytest.mark.parametrize("size,expected", [((2400, 4800), (540, 1080)), ((4800, 2400), (2160, 1080)),
                                         ((320, 640), (320, 640))])
def test_height_limit_preserves_portrait_landscape_and_small_image_ratio(app, size, expected):
    result = validate_image(io.BytesIO(encoded_image("JPEG", size=size)))
    assert (result.width, result.height) == expected


def test_height_limit_is_configurable(app):
    app.config["WEBP_MAX_HEIGHT"] = 720
    result = validate_image(io.BytesIO(encoded_image("JPEG", size=(3000, 1500))))
    assert (result.width, result.height) == (1440, 720)


@pytest.mark.parametrize("format,size,expected", [("JPEG", (15360, 8640), (1920, 1080)),
                                                ("PNG", (16384, 16384), (1080, 1080))])
def test_real_16k_inputs_are_reduced_before_full_pillow_decode(app, monkeypatch, format, size, expected):
    # Generate huge fixtures as a streaming pipeline rather than allocating their full RGB raster.
    source = pyvips.Image.black(*size, bands=3).new_from_image([32, 128, 64])
    data = source.jpegsave_buffer(Q=85) if format == "JPEG" else source.pngsave_buffer()
    original_load = Image.Image.load

    def bounded_load(image, *args, **kwargs):
        assert image.width * image.height <= WEBP_DIMENSION_LIMIT * app.config["WEBP_MAX_HEIGHT"]
        return original_load(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", bounded_load)
    result = validate_image(io.BytesIO(data))
    assert (result.width, result.height) == expected
    assert len(result.data) <= app.config["WEBP_MAX_BYTES"]
    with Image.open(io.BytesIO(result.data)) as stored:
        assert stored.format == "WEBP" and stored.size == expected


def test_byte_cap_also_reduces_dimensions_when_quality_alone_is_not_enough(app):
    size = (256, 192)
    noisy = Image.frombytes("RGBA", size, random.Random(7).randbytes(size[0] * size[1] * 4))
    app.config["WEBP_MAX_BYTES"] = 8 * 1024
    assert len(encode_webp(noisy, 50)) > app.config["WEBP_MAX_BYTES"]
    source = io.BytesIO()
    noisy.save(source, "PNG")
    result = validate_image(io.BytesIO(source.getvalue()))
    assert result.width < size[0] and result.height < size[1]
    assert abs(result.width - result.height * size[0] / size[1]) <= 1
    for data in (result.data, result.preview):
        assert len(data) <= app.config["WEBP_MAX_BYTES"]
        with Image.open(io.BytesIO(data)) as stored:
            assert stored.format == "WEBP" and stored.mode == "RGBA"


def test_extreme_panorama_respects_the_webp_codec_width_limit(app):
    result = validate_image(io.BytesIO(encoded_image("PNG", size=(20000, 100))))
    assert result.width <= WEBP_DIMENSION_LIMIT
    assert abs(result.width - result.height * 200) <= 100


def test_transparency_is_preserved_in_both_variants(app):
    result = validate_image(io.BytesIO(encoded_image("PNG", mode="RGBA", color=(255, 0, 0, 128))))
    for data in (result.data, result.preview):
        with Image.open(io.BytesIO(data)) as image:
            assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 128


def test_exif_rotation_is_applied_and_source_metadata_removed(app):
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "SOURCE_METADATA_SHOULD_NOT_BE_STORED"
    result = validate_image(io.BytesIO(encoded_image("JPEG", size=(80, 40), exif=exif)))
    assert (result.width, result.height) == (40, 80)
    with Image.open(io.BytesIO(result.data)) as stored:
        assert not stored.getexif()
    assert b"SOURCE_METADATA_SHOULD_NOT_BE_STORED" not in result.data


@pytest.mark.parametrize("format,streaming", [("JPEG", True), ("TIFF", True), ("TIFF", False)])
def test_height_limit_is_applied_after_exif_rotation(app, monkeypatch, format, streaming):
    if not streaming:
        monkeypatch.setattr("app.storage.STREAMING_IMAGE_FORMATS", frozenset())
    exif = Image.Exif()
    exif[274] = 6
    result = validate_image(io.BytesIO(encoded_image(format, size=(2400, 1200), exif=exif)))
    assert (result.width, result.height) == (540, 1080)


def test_minio_only_receives_variants_within_the_configured_limits(authenticated, app):
    app.config["WEBP_MAX_HEIGHT"] = 80
    app.config["WEBP_MAX_BYTES"] = 2048
    size = (256, 192)
    source = io.BytesIO()
    Image.frombytes("RGB", size, random.Random(9).randbytes(size[0] * size[1] * 3)).save(source, "PNG")
    response = upload(authenticated, "grande.png", source.getvalue())
    assert response.status_code == 201
    photo = Photo.query.first()
    assert photo.height <= 80 and photo.width <= WEBP_DIMENSION_LIMIT
    assert abs(photo.width - photo.height * size[0] / size[1]) <= 1
    objects = app.extensions["media_storage"].objects
    for key, expected_bytes in ((photo.object_key, photo.size_bytes), (photo.preview_key, photo.preview_bytes)):
        assert len(objects[key]) == expected_bytes <= 2048
        assert objects[key] != source.getvalue()
        with Image.open(io.BytesIO(objects[key])) as stored:
            assert stored.format == "WEBP" and stored.height <= 80
    with Image.open(io.BytesIO(objects[photo.object_key])) as stored:
        assert stored.size == (photo.width, photo.height)


def test_animated_input_uses_only_the_first_frame(app):
    output = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(output, "GIF", save_all=True,
                                          append_images=[Image.new("RGB", (64, 48), "blue")], duration=100, loop=0)
    result = validate_image(io.BytesIO(output.getvalue()))
    with Image.open(io.BytesIO(result.data)) as stored:
        assert getattr(stored, "n_frames", 1) == 1
        assert stored.getpixel((10, 10))[0] > 200


def test_16_bit_tiff_is_scaled_to_eight_bit(app):
    image = Image.new("I;16", (64, 48), 32768)
    output = io.BytesIO()
    image.save(output, "TIFF")
    result = validate_image(io.BytesIO(output.getvalue()))
    with Image.open(io.BytesIO(result.data)) as stored:
        assert 120 <= stored.getpixel((10, 10))[0] <= 135


def test_pixel_and_input_size_limits_apply_before_encoding(app):
    app.config["MAX_IMAGE_PIXELS"] = 10
    with pytest.raises(ValueError, match="píxeles"):
        validate_image(io.BytesIO(encoded_image("PNG")))
    app.config["MAX_IMAGE_BYTES"] = 10
    with pytest.raises(ValueError, match="límite por archivo"):
        validate_image(io.BytesIO(encoded_image("PNG")))


def test_quota_uses_actual_webp_bytes_instead_of_input_size(authenticated):
    source = encoded_image("BMP", size=(256, 256))
    Unit.query.first().quota_bytes = 2000
    db.session.commit()
    assert len(source) > 2000
    assert upload(authenticated, "foto.bmp", source).status_code == 201
    photo = Photo.query.first()
    assert photo.filename == "foto.webp" and photo.size_bytes + photo.preview_bytes <= 2000


def test_browser_incompatible_preview_does_not_write_to_minio(authenticated, app):
    response = authenticated.post("/admin/events/1/photos/preview",
                                  data={"photo": (io.BytesIO(encoded_image("HEIF")), "iphone.heic")},
                                  content_type="multipart/form-data")
    assert response.status_code == 200 and response.content_type == "image/webp"
    with Image.open(io.BytesIO(response.data)) as preview:
        assert preview.format == "WEBP"
    assert not app.extensions["media_storage"].objects and Photo.query.count() == 0


def test_preview_requires_login_and_respects_unit_permissions(client, app):
    assert client.post("/admin/events/1/photos/preview").status_code == 302
    from conftest import login
    login(client, "otro-editor@lavalleja.uy")
    assert client.post("/admin/events/1/photos/preview").status_code == 404
