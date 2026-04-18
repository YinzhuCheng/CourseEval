import html
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from markupsafe import Markup

from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import get_current_user, is_admin, is_super_admin, is_teacher_account, pop_flashes
from app.services.user_media import user_avatar_public_url
from app.config import get_settings
from app.enum_labels import label_for_enum
from app.i18n import get_locale, template_localizer, template_translator


settings = get_settings()
templates = Jinja2Templates(directory=str(settings.templates_dir))
display_timezone = ZoneInfo(settings.timezone_name)


def format_datetime(value: datetime | None, pattern: str = "%Y-%m-%d %H:%M:%S") -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        # Legacy rows may store UTC timestamps without tzinfo. Interpret naive
        # values as UTC so rendered times stay aligned with real-world Beijing time.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(display_timezone).strftime(pattern)


def format_discussion_body(text: str | None) -> Markup:
    """Escape HTML and preserve line breaks for plain-text discussion posts."""
    safe = html.escape((text or "").strip())
    return Markup("<br/>".join(safe.splitlines())) if safe else Markup("")


def course_cover_url(course) -> str | None:
    if course is None:
        return None
    path = getattr(course, "cover_image_path", None)
    if not path:
        return None
    return f"/data-files/{quote(str(path), safe='/')}"


def render_template(
    request: Request,
    db: Session,
    template_name: str,
    context: Mapping | None = None,
    status_code: int = 200,
):
    loc = get_locale(request)

    def elabel(category: str, value) -> str:
        raw = value.value if hasattr(value, "value") else value
        return label_for_enum(loc, category, raw)

    base_context = {
        "request": request,
        "current_user": get_current_user(request, db),
        "flashes": pop_flashes(request),
        "current_locale": loc,
        "t": template_translator(request),
        "lx": template_localizer(request),
        "format_datetime": format_datetime,
        "format_discussion_body": format_discussion_body,
        "course_cover_url": course_cover_url,
        "elabel": elabel,
    }
    base_context["can_use_teacher_features"] = is_teacher_account(base_context["current_user"])
    base_context["can_access_admin_features"] = is_admin(base_context["current_user"])
    base_context["can_manage_platform_users"] = is_super_admin(base_context["current_user"])
    base_context["current_user_role"] = (
        base_context["current_user"].effective_role.value if base_context["current_user"] else None
    )
    base_context["current_user_avatar_url"] = user_avatar_public_url(base_context["current_user"])
    if context:
        base_context.update(context)
    return templates.TemplateResponse(request, template_name, dict(base_context), status_code=status_code)
