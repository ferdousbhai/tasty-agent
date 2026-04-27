---
name: trading
description: "Manage TastyTrade brokerage accounts — monitor portfolios, analyze options with Greeks and GEX, place and manage multi-leg orders, and stream real-time market data. Use when the user asks about account balances, positions, options chains, IV rank, gamma exposure, order placement, watchlists, or market status."
---

# TastyTrade Trading

Interact with TastyTrade brokerage accounts via the tasty-agent MCP server. Covers portfolio monitoring, market data streaming, options analysis, and order management with built-in rate limiting (2 req/s).

## Workflow

1. **Check market status** — call `market_status` to confirm the relevant exchange is open before placing orders or fetching live quotes.
2. **Review account state** — use `account_overview` with `include=["balances","positions"]` to see net liquidating value and current holdings.
3. **Research** — gather data with the appropriate tool:
   - `get_quotes` for real-time stock/option/futures quotes via DXLink streaming
   - `get_greeks` for delta, gamma, theta, vega, rho on specific option contracts
   - `get_gex` for gamma exposure analysis (net GEX, flip level, call/put walls)
   - `get_market_metrics` for IV rank, IV percentile, beta, and liquidity across symbols
   - `search_symbols` to look up tickers by name
4. **Plan the trade** — verify positions with `account_overview`, check Greeks for risk, and confirm the user's intent before proceeding.
5. **Execute** — use `place_order` for new orders, `replace_order` to reprice existing live orders at the current mid, or `cancel_order` to cancel. Always require explicit user confirmation before placing.
6. **Track** — use `get_history` for transaction or order history, `list_orders` for live orders, and `watchlist` to manage symbol lists.

## Key Rules

- Never place orders without explicit user confirmation.
- Equity and option legs use `Buy to Open`, `Buy to Close`, `Sell to Open`, `Sell to Close`; futures use `Buy` or `Sell`.
- `place_order` always uses quote-derived mid pricing; do not pass raw prices.
- For dollar-budget orders, pass top-level `target_value`. With `target_value`, leg quantity is a ratio and usually omitted/defaults to 1.
- `place_order` defaults to `chase=true`: wait 10 seconds, re-check live order status, recalculate from fresh exact-instrument quotes, move 1 valid price tick closer to fill, repeat up to 10 reprices.
- For replacing an order, call `replace_order(order_id)` to reprice at current mid.
- Do not use underlying stock quotes as option order prices. `place_order` resolves the exact instrument quote and validates the signed net limit against the current bid/ask market.
- Tool outputs are intentionally compact; use the returned bid/ask/mid, sizing, warnings, and order summaries rather than expecting full SDK dumps.
- Supported time-in-force values: `Day`, `GTC`, `GTD`, `Ext`, `Ext Overnight`, `GTC Ext`, `GTC Ext Overnight`, `IOC`.
- `get_gex` returns compact analysis (not raw per-strike data) to avoid oversized responses on large chains.
- Use `get_history(type="transactions")` for trade/money history (default 90 days) and `type="orders"` for order history (default 7 days). Paginate with `page_offset` and `limit`.
- `watchlist(action="list")` without a name returns watchlist metadata only. Call it again with `name` to fetch symbols for a specific watchlist.
