from datetime import datetime
import logging
from typing import Callable, Awaitable
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

class JobQueue:
    """Manages sequential execution of scheduled jobs."""

    def __init__(self, scheduler: AsyncIOScheduler):
        self.scheduler = scheduler
        self._queue: list[tuple[str, datetime, Callable[[], Awaitable[None]]]] = []
        self._is_processing = False
        self._processing_lock = None  # Will be initialized when needed

    async def add_job(
        self,
        job_func: Callable[[], Awaitable[None]],
        run_date: datetime,
        job_id: str | None = None
    ) -> str:
        """Add a job to the queue for sequential execution.

        Args:
            job_func: The async function to execute
            run_date: When to start processing the queue
            job_id: Optional job ID. If not provided, one will be generated.

        Returns:
            The job ID
        """
        if job_id is None:
            job_id = str(uuid4())

        self._queue.append((job_id, run_date, job_func))
        logger.info(f"Added job {job_id} to queue. Queue length: {len(self._queue)}")

        # If this is the first job, schedule the queue processor
        if len(self._queue) == 1:
            self._schedule_queue_processor(run_date)

        return job_id

    def _schedule_queue_processor(self, run_date: datetime) -> None:
        """Schedule the queue processor to start at the specified time."""
        self.scheduler.add_job(
            self._process_queue,
            'date',
            run_date=run_date,
            id='queue_processor'
        )

    async def _process_queue(self) -> None:
        """Process jobs in the queue sequentially."""
        if self._is_processing:
            logger.warning("Queue processor already running")
            return

        self._is_processing = True
        logger.info(f"Starting queue processor. Queue length: {len(self._queue)}")

        try:
            while self._queue:
                job_id, _, job_func = self._queue[0]
                logger.info(f"Processing job {job_id}")

                try:
                    await job_func()
                    logger.info(f"Successfully completed job {job_id}")
                except Exception as e:
                    logger.error(f"Error processing job {job_id}: {e}")
                finally:
                    # Remove the job from the queue regardless of success/failure
                    self._queue.pop(0)

            logger.info("Queue processing completed")
        finally:
            self._is_processing = False

            # If there are more jobs in the queue, schedule the next processor
            if self._queue:
                next_run_date = self._queue[0][1]
                self._schedule_queue_processor(next_run_date)

    def remove_job(self, job_id: str) -> bool:
        """Remove a job from the queue.

        Args:
            job_id: The ID of the job to remove

        Returns:
            True if the job was found and removed, False otherwise
        """
        for i, (queued_job_id, _, _) in enumerate(self._queue):
            if queued_job_id == job_id:
                self._queue.pop(i)
                logger.info(f"Removed job {job_id} from queue")
                return True
        return False

    def get_queue_status(self) -> list[tuple[str, datetime]]:
        """Get the current status of the queue.

        Returns:
            List of tuples containing (job_id, run_date) for each job in the queue
        """
        return [(job_id, run_date) for job_id, run_date, _ in self._queue]