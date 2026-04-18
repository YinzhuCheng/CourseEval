"""Extract embedded images from .ipynb into placeholders + ordered multimodal payloads."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import nbformat

from app.services.llm import ImageInput

# Per-image cap before we skip bytes (still emit placeholder with skipped=oversized).
MAX_IMAGE_BYTES = 4 * 1024 * 1024

_DATA_URI_PATTERN = re.compile(
    r"data:(image/(?:png|jpeg|jpg|gif|webp));base64,([A-Za-z0-9+/=\s\r\n]+)",
    re.IGNORECASE,
)


@dataclass
class ImageRegistryEntry:
    placeholder_id: str
    source_cell: int
    source_field: str
    mime_type: str
    base64_payload: str
    sha256: str
    order_index: int
    skipped: bool = False
    broken: bool = False
    is_alias: bool = False


@dataclass
class NotebookSanitizeResult:
    """Text with [[IMAGE:IMG_xxxx|mime=...]] markers + images for multimodal API (unique blobs, first-seen order)."""

    text: str
    registry: list[ImageRegistryEntry] = field(default_factory=list)
    images: list[ImageInput] = field(default_factory=list)


def _normalize_mime(mime: str) -> str:
    m = (mime or "").strip().lower()
    if m == "image/jpg":
        return "image/jpeg"
    return m


def _looks_like_image_magic(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:2] == b"\xff\xd8":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _b64decode_safe(raw: str) -> tuple[bytes | None, bool]:
    cleaned = re.sub(r"\s+", "", raw)
    if len(cleaned) < 32:
        return None, True
    try:
        return base64.b64decode(cleaned, validate=True), False
    except Exception:
        try:
            return base64.b64decode(cleaned, validate=False), False
        except Exception:
            return None, True


def _next_id(counter: list[int]) -> str:
    counter[0] += 1
    return f"IMG_{counter[0]:04d}"


def _sanitize_string_with_data_uris(
    text: str,
    *,
    cell_index: int,
    field_label: str,
    id_counter: list[int],
    sha_to_placeholder: dict[str, str],
    registry: list[ImageRegistryEntry],
    unique_order: list[tuple[str, bytes, str]],
) -> str:
    if not text:
        return text

    def repl(match: re.Match[str]) -> str:
        mime = _normalize_mime(match.group(1))
        raw_b64 = match.group(2)
        data, broken = _b64decode_safe(raw_b64)
        if broken or data is None:
            pid = _next_id(id_counter)
            registry.append(
                ImageRegistryEntry(
                    placeholder_id=pid,
                    source_cell=cell_index,
                    source_field=field_label,
                    mime_type=mime,
                    base64_payload="",
                    sha256="",
                    order_index=len(registry),
                    broken=True,
                )
            )
            return f"[[BROKEN_IMG:{pid}|mime={mime}]]"

        if not _looks_like_image_magic(data):
            pid = _next_id(id_counter)
            registry.append(
                ImageRegistryEntry(
                    placeholder_id=pid,
                    source_cell=cell_index,
                    source_field=field_label,
                    mime_type=mime,
                    base64_payload="",
                    sha256="",
                    order_index=len(registry),
                    broken=True,
                )
            )
            return f"[[BROKEN_IMG:{pid}|reason=not_an_image]]"

        digest = hashlib.sha256(data).hexdigest()
        skipped = len(data) > MAX_IMAGE_BYTES
        if digest in sha_to_placeholder:
            pid = sha_to_placeholder[digest]
            registry.append(
                ImageRegistryEntry(
                    placeholder_id=pid,
                    source_cell=cell_index,
                    source_field=field_label,
                    mime_type=mime,
                    base64_payload="",
                    sha256=digest,
                    order_index=len(registry),
                    skipped=skipped,
                    is_alias=True,
                )
            )
        else:
            pid = _next_id(id_counter)
            sha_to_placeholder[digest] = pid
            registry.append(
                ImageRegistryEntry(
                    placeholder_id=pid,
                    source_cell=cell_index,
                    source_field=field_label,
                    mime_type=mime,
                    base64_payload=base64.b64encode(data).decode("ascii"),
                    sha256=digest,
                    order_index=len(registry),
                    skipped=skipped,
                )
            )
            if not skipped:
                unique_order.append((pid, data, mime))

        if skipped:
            return f"[[IMAGE:{pid}|mime={mime}|skipped=oversized]]"
        return f"[[IMAGE:{pid}|mime={mime}]]"

    return _DATA_URI_PATTERN.sub(repl, text)


def _extract_from_output_data(
    data: dict[str, Any],
    *,
    cell_index: int,
    output_index: int,
    id_counter: list[int],
    sha_to_placeholder: dict[str, str],
    registry: list[ImageRegistryEntry],
    unique_order: list[tuple[str, bytes, str]],
    lines_out: list[str],
) -> None:
    if not isinstance(data, dict):
        return
    for key in list(data.keys()):
        if not isinstance(key, str) or not key.startswith("image/"):
            continue
        mime = _normalize_mime(key)
        raw_val = data.get(key)
        if raw_val is None:
            continue
        if isinstance(raw_val, str):
            payload = raw_val
        elif isinstance(raw_val, list):
            payload = "".join(str(x) for x in raw_val)
        else:
            continue
        data_b64, broken = _b64decode_safe(payload)
        field_label = f"cell_{cell_index}/output_{output_index}/data[{key}]"
        if broken or data_b64 is None:
            pid = _next_id(id_counter)
            registry.append(
                ImageRegistryEntry(
                    placeholder_id=pid,
                    source_cell=cell_index,
                    source_field=field_label,
                    mime_type=mime,
                    base64_payload="",
                    sha256="",
                    order_index=len(registry),
                    broken=True,
                )
            )
            lines_out.append(f"[[BROKEN_IMG:{pid}|mime={mime}]]")
            del data[key]
            continue

        if not _looks_like_image_magic(data_b64):
            pid = _next_id(id_counter)
            registry.append(
                ImageRegistryEntry(
                    placeholder_id=pid,
                    source_cell=cell_index,
                    source_field=field_label,
                    mime_type=mime,
                    base64_payload="",
                    sha256="",
                    order_index=len(registry),
                    broken=True,
                )
            )
            lines_out.append(f"[[BROKEN_IMG:{pid}|reason=not_an_image]]")
            del data[key]
            continue

        digest = hashlib.sha256(data_b64).hexdigest()
        skipped = len(data_b64) > MAX_IMAGE_BYTES
        if digest in sha_to_placeholder:
            pid = sha_to_placeholder[digest]
            registry.append(
                ImageRegistryEntry(
                    placeholder_id=pid,
                    source_cell=cell_index,
                    source_field=field_label,
                    mime_type=mime,
                    base64_payload="",
                    sha256=digest,
                    order_index=len(registry),
                    skipped=skipped,
                    is_alias=True,
                )
            )
        else:
            pid = _next_id(id_counter)
            sha_to_placeholder[digest] = pid
            registry.append(
                ImageRegistryEntry(
                    placeholder_id=pid,
                    source_cell=cell_index,
                    source_field=field_label,
                    mime_type=mime,
                    base64_payload=base64.b64encode(data_b64).decode("ascii"),
                    sha256=digest,
                    order_index=len(registry),
                    skipped=skipped,
                )
            )
            if not skipped:
                unique_order.append((pid, data_b64, mime))

        if skipped:
            lines_out.append(f"[[IMAGE:{pid}|mime={mime}|skipped=oversized]]")
        else:
            lines_out.append(f"[[IMAGE:{pid}|mime={mime}]]")
        del data[key]


def sanitize_notebook_for_llm(file_path: Path, *, require_outputs: bool) -> NotebookSanitizeResult:
    """Parse ipynb, replace embedded images with [[IMAGE:IMG_xxxx|...]] and collect unique images for multimodal."""
    notebook = nbformat.read(file_path, as_version=4)
    id_counter = [0]
    sha_to_placeholder: dict[str, str] = {}
    registry: list[ImageRegistryEntry] = []
    unique_order: list[tuple[str, bytes, str]] = []
    sections: list[str] = []
    has_outputs = False

    for index, cell in enumerate(notebook.cells, start=1):
        cell_type = cell.get("cell_type", "unknown")
        sections.append(f"Cell {index} [{cell_type}]")
        source = cell.get("source") or ""
        if isinstance(source, list):
            source = "".join(source)
        source = str(source).strip()
        if source:
            source = _sanitize_string_with_data_uris(
                source,
                cell_index=index,
                field_label=f"cell_{index}/source",
                id_counter=id_counter,
                sha_to_placeholder=sha_to_placeholder,
                registry=registry,
                unique_order=unique_order,
            )
            sections.append(source)

        outputs = cell.get("outputs") or []
        if outputs:
            has_outputs = True
            rendered_outputs: list[str] = []
            for oi, output in enumerate(outputs, start=1):
                if not isinstance(output, dict):
                    continue
                ot = output.get("output_type")
                if ot == "stream":
                    text = output.get("text", "")
                    if isinstance(text, list):
                        text = "".join(text)
                    t = str(text).strip()
                    if t:
                        t = _sanitize_string_with_data_uris(
                            t,
                            cell_index=index,
                            field_label=f"cell_{index}/output_{oi}/stream",
                            id_counter=id_counter,
                            sha_to_placeholder=sha_to_placeholder,
                            registry=registry,
                            unique_order=unique_order,
                        )
                        rendered_outputs.append(t)
                elif "text" in output:
                    text = output.get("text", "")
                    if isinstance(text, list):
                        text = "".join(text)
                    t = str(text).strip()
                    if t:
                        t = _sanitize_string_with_data_uris(
                            t,
                            cell_index=index,
                            field_label=f"cell_{index}/output_{oi}/text",
                            id_counter=id_counter,
                            sha_to_placeholder=sha_to_placeholder,
                            registry=registry,
                            unique_order=unique_order,
                        )
                        rendered_outputs.append(t)
                elif "data" in output and isinstance(output["data"], dict):
                    data = dict(output["data"])
                    extra_lines: list[str] = []
                    _extract_from_output_data(
                        data,
                        cell_index=index,
                        output_index=oi,
                        id_counter=id_counter,
                        sha_to_placeholder=sha_to_placeholder,
                        registry=registry,
                        unique_order=unique_order,
                        lines_out=extra_lines,
                    )
                    for k, v in data.items():
                        if k == "text/plain":
                            tv = v if isinstance(v, str) else "".join(v) if isinstance(v, list) else str(v)
                            tv = str(tv).strip()
                            if tv:
                                tv = _sanitize_string_with_data_uris(
                                    tv,
                                    cell_index=index,
                                    field_label=f"cell_{index}/output_{oi}/data[text/plain]",
                                    id_counter=id_counter,
                                    sha_to_placeholder=sha_to_placeholder,
                                    registry=registry,
                                    unique_order=unique_order,
                                )
                                rendered_outputs.append(tv)
                    rendered_outputs.extend(extra_lines)
            if rendered_outputs:
                sections.append("Outputs:")
                sections.append("\n".join(item for item in rendered_outputs if item))
        sections.append("")

    if require_outputs and not has_outputs:
        raise ValueError("Notebook submissions for this question must include executed outputs before upload.")

    rendered = "\n".join(section for section in sections if section is not None).strip()
    if not rendered:
        raise ValueError("The uploaded notebook is empty.")

    images = [ImageInput(mime_type=mime, data=data) for _pid, data, mime in unique_order]
    return NotebookSanitizeResult(text=rendered, registry=registry, images=images)


def notebook_placeholder_alignment_block(registry: list[ImageRegistryEntry], num_attached_images: int) -> str:
    """System/user hint: map IMG_* to attached image order (unique blobs only)."""
    if not registry:
        return ""
    lines = [
        "Notebook multimodal rules:",
        "- The student notebook text uses placeholders [[IMAGE:IMG_XXXX|mime=...]] and possibly [[BROKEN_IMG:...]].",
        "- You will receive images after the text; they are numbered Image #1, Image #2, ... in the same order as "
        "first appearance of each distinct image content in the notebook.",
        "- Map each IMG_XXXX to an image using the table below. Multiple placeholders with the same ID refer to the same image.",
        "- When you comment, cite placeholder IDs (e.g. IMG_0003) so text and visuals stay aligned.",
        "",
        "Placeholder → attached image index (empty if broken or skipped):",
    ]
    seen: dict[str, int] = {}
    next_idx = 1
    for e in registry:
        if e.broken or e.skipped or not e.sha256:
            lines.append(f"  {e.placeholder_id}: (no image — broken or skipped)")
            continue
        if e.is_alias:
            if e.placeholder_id in seen:
                idx = seen[e.placeholder_id]
                lines.append(
                    f"  {e.placeholder_id} (also at cell {e.source_cell}, {e.source_field}): same as Image #{idx}"
                )
            continue
        if e.placeholder_id not in seen:
            seen[e.placeholder_id] = next_idx
            next_idx += 1
        idx = seen[e.placeholder_id]
        lines.append(f"  {e.placeholder_id}: Image #{idx} ({e.mime_type}), cell {e.source_cell}, {e.source_field}")
    lines.append(f"Total distinct images attached: {num_attached_images}.")
    return "\n".join(lines)
