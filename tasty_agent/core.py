from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import Context
from pydantic import BaseModel
from tabulate import tabulate
from tastytrade import Account, Session

logger = logging.getLogger(__name__)


def to_table(data: Sequence[BaseModel]) -> str:
    """Format list of Pydantic models as a plain table."""
    if not data:
        return "No data"
    return tabulate([item.model_dump() for item in data], headers="keys", tablefmt="plain")


@dataclass
class ServerContext:
    session: Session
    account: Account


def get_context(ctx: Context) -> ServerContext:
    """Extract ServerContext from the MCP request context."""
    return ctx.request_context.lifespan_context


def get_session(ctx: Context) -> Session:
    """Get the tastytrade session (auto-refreshes tokens before each API call)."""
    return get_context(ctx).session


@asynccontextmanager
async def lifespan(_) -> AsyncIterator[ServerContext]:
    """Manage Tastytrade session lifecycle."""
    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    account_id = os.getenv("TASTYTRADE_ACCOUNT_ID")

    if not client_secret or not refresh_token:
        raise ValueError(
            "Missing Tastytrade OAuth credentials. Set TASTYTRADE_CLIENT_SECRET and "
            "TASTYTRADE_REFRESH_TOKEN environment variables."
        )

    try:
        session = Session(client_secret, refresh_token)
        accounts = await Account.get(session)
        logger.info("Successfully authenticated with Tastytrade. Found %s account(s).", len(accounts))
    except Exception as e:
        logger.error("Failed to authenticate with Tastytrade: %s", e)
        raise

    if account_id:
        account = next((acc for acc in accounts if acc.account_number == account_id), None)
        if not account:
            available = [acc.account_number for acc in accounts]
            raise ValueError(f"Account '{account_id}' not found. Available: {available}")
        logger.info("Using specified account: %s", account.account_number)
    else:
        account = accounts[0]
        logger.info("Using default account: %s", account.account_number)

    yield ServerContext(session=session, account=account)
