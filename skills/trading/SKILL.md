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
5. **Execute** — use `manage_order` with `action="place"` for new orders (supports multi-leg, auto mid-price discovery), `action="replace"` to modify price, or `action="cancel"` to cancel. Always require explicit user confirmation before placing.
6. **Track** — use `get_history` for transaction or order history, `manage_order` with `action="list"` for live orders, and `watchlist` to manage symbol lists.

## Key Rules

- Never place orders without explicit user confirmation.
- Equity and option legs use `Buy to Open`, `Buy to Close`, `Sell to Open`, `Sell to Close`; futures use `Buy` or `Sell`.
- Supported time-in-force values: `Day`, `GTC`, `GTD`, `Ext`, `Ext Overnight`, `GTC Ext`, `GTC Ext Overnight`, `IOC`.
- `get_gex` returns compact analysis (not raw per-strike data) to avoid oversized responses on large chains.
- Use `get_history(type="transactions")` for trade/money history (default 90 days) and `type="orders"` for order history (default 7 days). Paginate with `page_offset` and `max_results`.
