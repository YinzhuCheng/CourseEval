"""Parse multipart discussion forms (markdown body + optional image attachments)."""

from __future__ import annotations

from starlette.requests import Request


async def extract_discussion_images(request: Request) -> list[tuple[bytes, str]]:
    form = await request.form()
    out: list[tuple[bytes, str]] = []
    for key in form:
        if not str(key).startswith("images"):
            continue
        f = form[key]
        if hasattr(f, "read"):
            content = await f.read()  # type: ignore[union-attr]
            name = getattr(f, "filename", None) or "image.png"
            if content:
                out.append((content, str(name)))
    return out
