from .order_manager import OrderManager
from .utils import is_market_open
from src.tastytrade_api.auth import session, account


def load_and_review_queue(manager: OrderManager) -> list[dict]:
    """
    Loads the queue from file and returns all queued orders in a simple data structure
    that can be formatted by the caller (e.g., CLI or server).
    """
    manager.load_queue_from_file()
    tasks = []
    # Convert the queue to a flat list of dicts to make it easier to handle in the caller.
    for group in sorted(manager.task_queue.keys()):
        for item in manager.task_queue[group]:
            tasks.append({
                "group": group,
                "symbol": item["symbol"],
                "quantity": item["quantity"],
                "action": item["action"],
                "dry_run": bool(item.get("dry_run", False)),
            })
    return tasks


async def queue_order(manager: OrderManager, order_details: dict) -> str:
    """
    Queues an order using the shared manager and returns a status message.
    """
    return await manager.queue_order(**order_details)


async def execute_orders(manager: OrderManager, force: bool = False) -> str:
    """
    Loads the queue, checks if the market is open (unless force=True), then executes all orders.

    Returns:
        A status string with either success or error message.
    """
    # Load the latest queue:
    manager.load_queue_from_file()

    # Check if there are any non-dry-run orders that need an open market (unless forced):
    non_dry_run_tasks = [
        item
        for _, items in manager.task_queue.items()
        for item in items
        if not item.get("dry_run", False)
    ]
    if non_dry_run_tasks and not is_market_open() and not force:
        return (
            "Market is currently closed. Use force=True to execute anyway "
            "or wait for market hours."
        )

    try:
        await manager.execute_queued_tasks(session, account)
        return "All orders executed successfully."
    except Exception as e:
        return f"Error executing orders: {str(e)}"