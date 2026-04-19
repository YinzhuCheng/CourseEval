"""Shared validation and normalization for uploaded raster images."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import get_settings


settings = get_settings()

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def human_upload_max_bytes() -> str:
    """Short label for UI copy (e.g. 5 MB)."""
    b = settings.upload_max_bytes
    if b >= 1024 * 1024 and b % (1024 * 1024) == 0:
        return f"{b // (1024 * 1024)} MB"
    if b >= 1024 and b % 1024 == 0:
        return f"{b // 1024} KB"
    return f"{b} bytes"


def localized_image_validation_messages(value_error_key: str) -> tuple[str, str]:
    """English and Chinese flash strings for ValueError keys from normalize_uploaded_image."""
    exts = ", ".join(sorted(e.replace(".", "").upper() for e in sorted(ALLOWED_IMAGE_EXTENSIONS)))
    max_label = human_upload_max_bytes()
    if value_error_key == "unsupported_image_type":
        return (
            f"Invalid image type or content. Allowed: {exts}; extension must match the file contents.",
            f"图片格式无效或内容与扩展名不符。允许：{exts}；扩展名需与实际图像格式一致。",
        )
    if value_error_key == "file_too_large":
        return (
            f"Image is too large (max {max_label} per file after processing).",
            f"图片过大（处理后单文件不超过 {max_label}）。",
        )
    raise ValueError(value_error_key)


def format_image_upload_error(request: object, value_error_key: str) -> str:
    from app.i18n import choose_text

    en, zh = localized_image_validation_messages(value_error_key)
    return choose_text(request, en, zh)
_EXTENSION_FORMATS = {
    ".png": {"PNG"},
    ".jpg": {"JPEG"},
    ".jpeg": {"JPEG"},
    ".gif": {"GIF"},
    ".webp": {"WEBP"},
}

# Keep uploads comfortably below Pillow's default decompression-bomb threshold,
# but high enough for scanned pages and screenshots.
Image.MAX_IMAGE_PIXELS = 40_000_000


def normalize_uploaded_image(file_bytes: bytes, original_filename: str) -> tuple[str, bytes]:
    """Validate image bytes and return (extension, re-encoded bytes)."""
    ext = Path(original_filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("unsupported_image_type")
    if len(file_bytes) > settings.upload_max_bytes:
        raise ValueError("file_too_large")

    try:
        with Image.open(io.BytesIO(file_bytes)) as image:
            image.load()
            fmt = (image.format or "").upper()
            if fmt not in _EXTENSION_FORMATS[ext]:
                raise ValueError("unsupported_image_type")
            normalized = _encode_image(image, fmt)
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("unsupported_image_type") from exc

    if len(normalized) > settings.upload_max_bytes:
        raise ValueError("file_too_large")
    return ext, normalized


def _encode_image(image: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    if fmt == "JPEG":
        image.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
    elif fmt == "PNG":
        image.save(out, format="PNG", optimize=True)
    elif fmt == "WEBP":
        image.save(out, format="WEBP", quality=85, method=6)
    elif fmt == "GIF":
        image.save(out, format="GIF", save_all=True)
    else:
        raise ValueError("unsupported_image_type")
    return out.getvalue()

