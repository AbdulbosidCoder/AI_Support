"""Prepare client images for the model: validate, downscale, re-encode when needed."""
from __future__ import annotations

import io
import logging

from PIL import Image as PILImage, UnidentifiedImageError

from .models import Image

log = logging.getLogger(__name__)

SUPPORTED = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
# Longer images are downscaled: the model sees them at this size anyway, and it keeps requests small.
MAX_EDGE = 1568
MAX_BYTES = 3_500_000
MAX_IMAGES_PER_MESSAGE = 5


def prepare_image(data: bytes) -> Image | None:
    """Return an image the model accepts, or None if the bytes are not a readable image."""
    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        log.info("unreadable image: %s", e)
        return None

    fmt = img.format or ""
    if fmt in SUPPORTED and max(img.size) <= MAX_EDGE and len(data) <= MAX_BYTES:
        return Image(data, SUPPORTED[fmt])

    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return Image(out.getvalue(), "image/jpeg")
