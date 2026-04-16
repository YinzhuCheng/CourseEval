import logging

from rq import Worker

from app.config import get_settings
from app.services.submissions import cleanup_stale_running_items, redis_connection


settings = get_settings()
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    recovered_items = cleanup_stale_running_items()
    if recovered_items:
        logger.warning(
            "Marked %s stale submissions or evaluation tasks as failed during worker startup.",
            recovered_items,
        )

    connection = redis_connection()
    worker = Worker([settings.rq_queue_name], connection=connection)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
