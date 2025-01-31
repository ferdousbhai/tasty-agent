import asyncio
from typing import Literal
import logging
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import signal
import sys
import threading
import time
import os

from pydantic import BaseModel, ValidationError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .setup import auth
from .getters import (
    plot_nlv_history, get_account_balances, get_open_positions,  # noqa: F401
    get_transaction_history, get_metrics, get_prices, mcp, NYC_TIMEZONE  # noqa: F401
)
from ..tastytrade_api.auth import session, account
from ..tastytrade_api.functions import place_trade

from ..utils import is_market_open, get_time_until_market_open

# Initialize logging and constants
logger = logging.getLogger(__name__)

# Configuration paths
CONFIG_DIR = Path.home() / ".tasty_agent"
TASKS_FILE = CONFIG_DIR / "scheduled_tasks.json"

# State Management
class SchedulerState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.scheduler: AsyncIOScheduler | None = None

scheduler_state = SchedulerState()

# Task Definition
class Task(BaseModel):
    """Represents a scheduled task"""
    task_id: str
    symbol: str
    quantity: int
    action: Literal["Buy to Open", "Sell to Close"]
    dry_run: bool = True
    description: str | None = None
    schedule_type: Literal["once", "daily"] = "once"
    run_time: str | None = None

    async def execute(self):
        """Execute the task"""
        if not is_market_open():
            logger.warning(f"Market closed, skipping task {self.task_id}")
            return

        try:
            # Execute trade and get result
            result = await place_trade(
                session=session,
                account=account,
                symbol=self.symbol,
                quantity=self.quantity,
                action=self.action,
                mcp=mcp,
                dry_run=self.dry_run
            )

            # Clean up one-time tasks after execution
            if self.schedule_type == "once":
                del scheduled_tasks[self.task_id]
                save_tasks()

            return result

        except Exception as e:
            error_msg = f"Task {self.task_id} failed: {str(e)}"
            logger.error(error_msg)
            mcp.send_log_message(level="error", data=error_msg)
            return error_msg

# Task Storage
scheduled_tasks: dict[str, Task] = {}

# Configuration Management
def _ensure_secure_config():
    """Ensure config directory exists with secure permissions"""
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if TASKS_FILE.exists():
        TASKS_FILE.chmod(0o600)  # Only user can read/write file

def save_tasks(cleanup_completed: bool = True) -> None:
    """
    Save tasks to disk, optionally cleaning up completed one-time tasks.
    """
    if cleanup_completed:
        # Only cleanup if scheduler is actually running
        if scheduler_state.scheduler and scheduler_state.scheduler.running:
            now = datetime.now(NYC_TIMEZONE)
            to_remove = []
            for task_id, task in scheduled_tasks.items():
                if task.schedule_type == "once":
                    job = scheduler_state.scheduler.get_job(task_id)
                    # Only remove if job doesn't exist or its next run time is in the past
                    if not job or (
                        hasattr(job, 'next_run_time') 
                        and job.next_run_time 
                        and job.next_run_time.astimezone(NYC_TIMEZONE) < now
                    ):
                        to_remove.append(task_id)

            for tid in to_remove:
                del scheduled_tasks[tid]
                logger.info(f"Cleaned up completed task: {tid}")

    with open(TASKS_FILE, "w") as f:
        json.dump({k: v.model_dump() for k, v in scheduled_tasks.items()}, f, indent=2)

def load_tasks():
    """Load tasks from secure JSON file"""
    _ensure_secure_config()
    if not TASKS_FILE.exists():
        return

    with open(TASKS_FILE, 'r') as f:
        tasks_data = json.load(f)

    for task_id, task_data in tasks_data.items():
        task = Task(**task_data)
        scheduled_tasks[task_id] = task
        _create_scheduler_job(task)

# Scheduler Management
def _create_scheduler_job(task: Task) -> None:
    """Create a scheduler job for a task"""
    hour, minute = map(int, task.run_time.split(":"))

    logger.info(f"Creating scheduler job for task {task.task_id} at {hour}:{minute}")

    try:
        if task.schedule_type == "daily":
            scheduler_state.scheduler.add_job(
                task.execute,
                CronTrigger(hour=hour, minute=minute, timezone=NYC_TIMEZONE),
                id=task.task_id
            )
            logger.info(f"Created daily scheduler job for task {task.task_id}")
        elif task.schedule_type == "once":
            now = datetime.now(NYC_TIMEZONE)
            run_time = datetime.strptime(task.run_time, "%H:%M").time()
            run_datetime = datetime.combine(now.date(), run_time).replace(tzinfo=NYC_TIMEZONE)

            # If the time has passed today, schedule for tomorrow
            if run_datetime <= now:
                run_datetime = datetime.combine(now.date() + timedelta(days=1), run_time).replace(tzinfo=NYC_TIMEZONE)
                logger.info(f"Time {task.run_time} has passed today, scheduling for tomorrow at {run_datetime}")

            scheduler_state.scheduler.add_job(
                task.execute,
                'date',
                run_date=run_datetime,
                timezone=NYC_TIMEZONE,
                id=task.task_id
            )
            logger.info(f"Created one-time scheduler job for task {task.task_id} at {run_datetime}")
    except Exception as e:
        logger.error(f"Failed to create scheduler job for task {task.task_id}: {str(e)}")
        raise

def start_scheduler():
    """Start the scheduler in a new event loop"""
    logger.info("Starting scheduler in new event loop...")

    if scheduler_state.scheduler is None:
        logger.error("Scheduler not initialized before starting")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def start_and_run():
        try:
            scheduler_state.scheduler.configure(timezone=NYC_TIMEZONE)
            scheduler_state.scheduler.start()
            logger.info("Scheduler started successfully")

            stop_event = asyncio.Event()
            while scheduler_state.running:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            logger.error(f"Error in scheduler: {e}")
            scheduler_state.running = False
        finally:
            if scheduler_state.scheduler and scheduler_state.scheduler.running:
                scheduler_state.scheduler.shutdown()
                logger.info("Scheduler shutdown complete")
            loop.stop()

    try:
        loop.run_until_complete(start_and_run())
    except Exception as e:
        logger.error(f"Error in scheduler thread: {e}")
        scheduler_state.running = False
    finally:
        loop.close()

# Server Management
def shutdown_handler(_, __) -> None:
    """Handle graceful shutdown of the server."""
    # Prevent multiple shutdown attempts
    if not scheduler_state.running:
        return

    logger.info("Shutdown signal received, cleaning up...")

    try:
        with scheduler_state.lock:
            if scheduler_state.running and scheduler_state.scheduler:
                scheduler_state.running = False  # Set this first to prevent re-entry
                scheduler_state.scheduler.shutdown(wait=True)
                logger.info("Scheduler shutdown complete")

        save_tasks(cleanup_completed=True)
        logger.info("Tasks saved successfully")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
    finally:
        # Kill the process
        os._exit(0)

# Trade Management Tools
@mcp.tool()
async def schedule_trade(
    symbol: str,
    quantity: int,
    action: Literal["Buy to Open", "Sell to Close"],
    execution_type: Literal["immediate", "once", "daily"] = "immediate",
    run_time: str | None = None,
    dry_run: bool = False
) -> str:
    """Schedule a trade for execution."""
    if not scheduler_state.running or not scheduler_state.scheduler:
        return "Error: Scheduler is not running. Cannot schedule trades."

    try:
        task_id = str(uuid4())
        logger.info(f"Starting schedule_trade with task_id: {task_id}")

        # Validate run_time format first if provided
        if run_time:
            try:
                hour, minute = map(int, run_time.split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return "Invalid time format. Please use HH:MM in 24-hour format between 00:00 and 23:59"
            except ValueError:
                return "Invalid time format. Please use HH:MM format (e.g., '09:30' for 9:30 AM)"

        # Validate execution_type and run_time combination
        if execution_type in ["once", "daily"] and not run_time:
            run_time = "09:30"
            logger.info(f"No run_time specified for {execution_type} task, defaulting to market open (09:30)")

        # Create and validate task
        try:
            task = Task(
                task_id=task_id,
                symbol=symbol,
                quantity=quantity,
                action=action,
                dry_run=dry_run,
                description=f"{action} {quantity} {symbol}",
                schedule_type=execution_type,
                run_time=run_time
            )
        except ValidationError as e:
            error_msg = f"Invalid task parameters for task {task_id}: {str(e)}"
            logger.error(error_msg)
            return error_msg
        except Exception as e:
            error_msg = f"Unexpected error creating task {task_id}: {str(e)}"
            logger.error(error_msg)
            return error_msg

        # Store task (but don't save yet)
        scheduled_tasks[task_id] = task
        logger.info(f"Created task {task_id}: {task.description}")

        # Handle immediate execution
        if execution_type == "immediate":
            if not is_market_open():
                wait_time = get_time_until_market_open()
                next_open = datetime.now(ZoneInfo("America/New_York")) + wait_time
                logger.info(f"Market closed, scheduling task {task_id} for next market open at {next_open}")
                try:
                    scheduler_state.scheduler.add_job(
                        task.execute,
                        'date',
                        run_date=next_open,
                        timezone=ZoneInfo("America/New_York"),
                        id=task_id
                    )
                    return f"Market is closed. Task {task_id} scheduled for next market open in {wait_time}"
                except Exception as e:
                    error_msg = f"Failed to schedule immediate task for next market open: {str(e)}"
                    logger.error(error_msg)
                    return error_msg

            logger.info(f"Executing immediate task {task_id}")
            try:
                await task.execute()
                return f"Task {task_id} executed immediately"
            except Exception as e:
                error_msg = f"Failed to execute immediate task: {str(e)}"
                logger.error(error_msg)
                return error_msg

        # Create scheduler job for scheduled tasks BEFORE saving
        try:
            _create_scheduler_job(task)
            # Now save after the job is created
            save_tasks()
            return f"Task {task_id} scheduled for {'daily' if execution_type == 'daily' else 'one-time'} execution at {run_time} NYC time"
        except Exception as e:
            error_msg = f"Failed to create scheduler job: {str(e)}"
            logger.error(error_msg)
            # Only try to delete if it exists
            if task_id in scheduled_tasks:
                del scheduled_tasks[task_id]  # Clean up failed task
            return error_msg

    except Exception as e:
        error_msg = f"Unexpected error in schedule_trade for task {task_id}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return error_msg

@mcp.tool()
async def list_scheduled_trades() -> str:
    """List all scheduled trades."""
    if not scheduled_tasks:
        return "No trades currently scheduled."

    output = ["Scheduled Tasks:", ""]
    output.append(f"{'Task ID':<36} {'Time':<8} {'Type':<8} {'Description':<40}")
    output.append("-" * 92)

    for task_id, task in scheduled_tasks.items():
        job = scheduler_state.scheduler.get_job(task_id)
        # If job is None, it may have executed or failed to schedule,
        # so you can skip or show it (choice depends on desired behavior).
        if not job:
            continue

        # Get next run time safely
        try:
            time_str = job.next_run_time.strftime('%H:%M') if job.next_run_time else 'Pending'
        except AttributeError:
            time_str = task.run_time if task.run_time else 'Unknown'

        schedule_type = 'daily' if task.schedule_type == 'daily' else 'once'

        output.append(
            f"{task_id:<36} {time_str:<8} {schedule_type:<8} {task.description[:40]:<40}"
        )

    # If everything got skipped
    if len(output) <= 4:
        return "No trades currently scheduled."

    return "\n".join(output)

@mcp.tool()
async def remove_scheduled_trade(task_id: str) -> str:
    """Remove a scheduled trade."""
    if task_id not in scheduled_tasks:
        return f"Trade {task_id} not found."

    try:
        scheduler_state.scheduler.remove_job(task_id)
        del scheduled_tasks[task_id]
        save_tasks()
        return f"Trade {task_id} removed successfully."
    except Exception as e:
        return f"Error removing trade {task_id}: {str(e)}"

def main():
    try:
        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)

        if len(sys.argv) > 1 and sys.argv[1] == "setup":
            sys.exit(0 if auth() else 1)

        with scheduler_state.lock:
            if not scheduler_state.running:
                scheduler_state.scheduler = AsyncIOScheduler()
                scheduler_state.scheduler.configure(timezone=NYC_TIMEZONE)
                load_tasks()
                save_tasks(cleanup_completed=True)

                scheduler_state.running = True
                # Run scheduler in a daemon thread
                scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
                scheduler_thread.start()

                # Give the scheduler more time to start
                time.sleep(2)

                # Verify scheduler is running
                if not scheduler_state.running or not scheduler_state.scheduler.running:
                    logger.error("Scheduler failed to start properly")
                    sys.exit(1)

                logger.info("Server is running")

        # Run MCP in the main thread
        mcp.run()

    except Exception as e:
        logger.error(f"Error in main: {e}")
        sys.exit(1)
