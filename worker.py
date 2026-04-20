import logging
import multiprocessing
import time

from rq import Worker
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.models import LLMConfig
from app.services.llm_groups import group_has_callable_target
from app.services.submissions import (
    cleanup_missing_queued_jobs,
    cleanup_stale_running_items,
    get_code_queue_name,
    llm_queue_name_for_config,
    redis_connection,
)


settings = get_settings()
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _run_worker(queue_name: str) -> None:
    connection = redis_connection()
    worker = Worker([queue_name], connection=connection)
    worker.work(with_scheduler=False)


def _desired_worker_layout() -> dict[str, int]:
    layout: dict[str, int] = {get_code_queue_name(): 1}
    with SessionLocal() as db:
        configs = list(
            db.scalars(
                select(LLMConfig)
                .options(selectinload(LLMConfig.members))
                .where(LLMConfig.enabled.is_(True), LLMConfig.queue_concurrency > 0)
                .order_by(LLMConfig.id.asc())
            ).all()
        )
        for config in configs:
            if not group_has_callable_target(config):
                continue
            layout[llm_queue_name_for_config(config)] = max(int(config.queue_concurrency or 1), 1)
    return layout


def _stop_process(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=5)


def main() -> None:
    recovered_items = cleanup_stale_running_items()
    recovered_items += cleanup_missing_queued_jobs()
    if recovered_items:
        logger.warning(
            "Marked %s stale submissions or evaluation tasks as failed during worker startup.",
            recovered_items,
        )

    processes: dict[str, list[multiprocessing.Process]] = {}
    try:
        while True:
            desired_layout = _desired_worker_layout()
            recovered = cleanup_missing_queued_jobs()
            if recovered:
                logger.warning("Marked %s queued tasks as failed because their Redis jobs are missing.", recovered)

            for queue_name in list(processes):
                live_processes = [process for process in processes[queue_name] if process.is_alive()]
                processes[queue_name] = live_processes
                desired_count = desired_layout.get(queue_name, 0)
                while len(processes[queue_name]) > desired_count:
                    process = processes[queue_name].pop()
                    _stop_process(process)
                    recovered = cleanup_stale_running_items()
                    if recovered:
                        logger.warning(
                            "Marked %s stale submissions or evaluation tasks as failed after stopping a worker.",
                            recovered,
                        )
                if not processes[queue_name]:
                    processes.pop(queue_name, None)

            for queue_name, desired_count in desired_layout.items():
                current_processes = processes.setdefault(queue_name, [])
                while len(current_processes) < desired_count:
                    process = multiprocessing.Process(target=_run_worker, args=(queue_name,), daemon=True)
                    process.start()
                    current_processes.append(process)
                    logger.info("Started worker process pid=%s for queue %s", process.pid, queue_name)

            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Stopping worker manager.")
    finally:
        for queue_processes in processes.values():
            for process in queue_processes:
                _stop_process(process)


if __name__ == "__main__":
    main()
