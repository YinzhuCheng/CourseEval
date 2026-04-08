from collections.abc import Mapping

from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import get_current_user, pop_flashes
from app.config import get_settings


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
    }
    if context:
        base_context.update(context)
    return templates.TemplateResponse(template_name, base_context, status_code=status_code)
