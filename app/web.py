from collections.abc import Mapping

from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import get_current_user, is_teacher_account, pop_flashes
from app.config import get_settings
from app.i18n import get_locale, template_translator


settings = get_settings()
templates = Jinja2Templates(directory=str(settings.templates_dir))


def render_template(
    request: Request,
    db: Session,
    template_name: str,
    context: Mapping | None = None,
    status_code: int = 200,
):
    base_context = {
        "request": request,
        "current_user": get_current_user(request, db),
        "flashes": pop_flashes(request),
        "current_locale": get_locale(request),
        "t": template_translator(request),
    }
    base_context["can_use_teacher_features"] = is_teacher_account(base_context["current_user"])
    if context:
        base_context.update(context)
    return templates.TemplateResponse(request, template_name, dict(base_context), status_code=status_code)
