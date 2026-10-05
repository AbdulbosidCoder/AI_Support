import io

from PIL import Image as PILImage

from ai_support.images import MAX_EDGE, prepare_image
from fakes import png


def _encode(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def test_small_supported_image_unchanged():
    data = png()
    out = prepare_image(data)
    assert out.data == data and out.media_type == "image/png"


def test_jpeg_detected_by_content_not_name():
    data = _encode(PILImage.new("RGB", (50, 50)), "JPEG")
    assert prepare_image(data).media_type == "image/jpeg"


def test_large_image_downscaled_to_jpeg():
    data = png(size=(1200, 4000))
    out = prepare_image(data)
    w, h = PILImage.open(io.BytesIO(out.data)).size
    assert max(w, h) <= MAX_EDGE and out.media_type == "image/jpeg"


def test_unsupported_format_converted():
    data = _encode(PILImage.new("RGBA", (30, 30)), "BMP")
    assert prepare_image(data).media_type == "image/jpeg"


def test_garbage_rejected():
    assert prepare_image(b"hello") is None
    assert prepare_image(b"") is None
