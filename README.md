# tasty-agent: A TastyTrade MCP Server
[![Trust Score](https://archestra.ai/mcp-catalog/api/badge/quality/ferdousbhai/tasty-agent)](https://archestra.ai/mcp-catalog/ferdousbhai__tasty-agent)

A Model Context Protocol server for TastyTrade brokerage accounts. Enables LLMs to monitor portfolios, analyze positions, and execute trades. Features automated IV analysis prompts and compact tool output.

Written in TypeScript against the Tastytrade REST API and DXLink directly (no SDK). The same code runs locally over stdio (`npx tasty-agent`) or as a remote MCP server on Cloudflare Workers.

> **v7 is a rewrite.** The Python package (`uvx tasty-agent`, 6.x) is replaced by the npm package, and the Modal deployment by Cloudflare Workers. Tool names are unchanged. What differs from 6.x:
> - `get_quotes` no longer takes `timeout`: quotes come from REST market data, so they also answer outside market hours.
> - `get_greeks` accepts at most 100 contracts and a `timeout` of at most 30 seconds.
> - Orders and replacements are refused when the broker's dry run returns any warning.
> - A cancel, order, or watchlist change the broker may have applied without answering reports an unknown outcome instead of failing.
> - Prices are rounded with the broker's tick schedules read as upper bounds (e.g. sub-penny below $1, a cent from $1), and futures options can now be priced.
> - The `GTD` time-in-force and the local SSE/HTTP transports are gone; a blank `TASTYTRADE_ACCOUNT_ID` counts as unset.

## Authentication

**OAuth Setup**:
1. Create an OAuth app at https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications
2. Check all scopes, save your client ID and client secret
3. Create a "New Personal OAuth Grant" in your OAuth app settings (check all scopes)
4. Copy the generated refresh token
5. Configure the MCP server with your credentials (see Usage below)

| Variable | Required | Notes |
| --- | --- | --- |
| `TASTYTRADE_CLIENT_SECRET` | yes | OAuth app client secret |
| `TASTYTRADE_REFRESH_TOKEN` | yes | Personal OAuth grant refresh token |
| `TASTYTRADE_ACCOUNT_ID` | no | Needed only when the grant exposes several accounts; otherwise the one account is used |
| `TASTYTRADE_API_BASE` | no | Defaults to `https://api.tastyworks.com`; use `https://api.cert.tastyworks.com` for the sandbox |
| `MCP_BEARER_TOKEN` | Cloudflare only | Clients must send `Authorization: Bearer <token>` |

## MCP Tools

### Account & Portfolio
- **`account_overview(include=["balances","positions"])`** - Account balances (including net liquidating value) and open positions.

### Market Data & Research
- **`get_quotes(instruments)`** - Live bid/ask/mid quotes for stocks, options, futures, and indices (indices without a two-sided market report their last price)
- **`get_greeks(options, timeout=10.0)`** - Greeks (delta, gamma, theta, vega, rho) for equity and futures options via DXLink streaming (up to 100 contracts, timeout at most 30s)
- **`get_market_metrics(symbols)`** - IV rank, percentile, beta, liquidity for multiple symbols
- **`market_status(exchanges=['Equity'])`** - Market hours, status, holidays, and current NYC time ('Equity', 'CME', 'CFE', 'Smalls')
- **`search_symbols(symbol, limit=10)`** - Search for symbols by name/ticker

### History
- **`get_history(type, days=None, underlying_symbol=None, transaction_type=None, page_offset=0, limit=25)`** - Transaction history (`type="transactions"`, default 90 days) or order history (`type="orders"`, default 7 days). Paginated — use `page_offset` and `limit` for large result sets. Filter transactions by `"Trade"` or `"Money Movement"`.

### Order Management
- **`place_order(legs, target_value=None, time_in_force="Day", dry_run=false)`** - Place multi-leg orders with quote-derived mid pricing only. The tool fetches live quotes for the exact resolved instruments, computes the signed net mid, validates the final limit against bid/ask guardrails, and optionally sizes quantity from `target_value`.
  - `quantity` is the actual share/contract count. `target_value=50000` sizes an equity or equity-option order from quote-derived pricing; omit `quantity` for single-leg target-value orders. For multi-leg spreads with `target_value`, use `quantity` only to express the leg ratio, such as 1:1 or 2:1.
  - Order prices are aligned to the broker's valid tick grid before submission. If tick-size data is unavailable, the tool fails before placement instead of submitting an invalid price increment.
  - Every order is sent to the broker as a dry run first. Any dry-run warning refuses the order; `dry_run=true` returns the preview and marks such an order `blocked`.
  - If the broker does not answer a submission (timeout or 5xx), the tool reports an unknown outcome and never retries; check `list_orders` before placing again. Cancels and watchlist changes are handled the same way.
  - Equities and options use `Buy to Open`, `Buy to Close`, `Sell to Open`, or `Sell to Close`; futures use `Buy` or `Sell`.
- **`replace_order(order_id)`** - Reprice an existing live order at the current quote-derived mid (dry-run checked the same way).
- **`cancel_order(order_id)`** - Cancel an order.
- **`list_orders()`** - Get all live orders.
- Tool outputs are compact: quote tables include actionable bid/ask/mid/size fields; order results include compact order, buying-power, fee, warning, pricing, and sizing summaries.

### Watchlist Management
- **`watchlist(action, ...)`** - Unified watchlist management:
  - `action="list"` - No `name` returns compact watchlist metadata (`name`, `group`, `symbol_count`); with `name`, returns compact symbol entries.
  - `action="add"` - Add symbols to a watchlist (creates if doesn't exist)
  - `action="remove"` - Remove symbols from a watchlist
  - `action="delete"` - Delete a watchlist

### MCP Prompts
- **IV Rank Analysis** - Automated prompt to analyze IV rank extremes across positions and watchlists for entry/exit opportunities

All broker requests, including token refreshes, share one two-requests-per-second gate (per process locally, one Durable Object on Cloudflare).

## Usage

### Local (stdio)

Requires Node.js 22+. Add to your MCP client configuration (e.g., `claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "tastytrade": {
      "command": "npx",
      "args": ["-y", "tasty-agent@7"],
      "env": {
        "TASTYTRADE_CLIENT_SECRET": "your_client_secret",
        "TASTYTRADE_REFRESH_TOKEN": "your_refresh_token"
      }
    }
  }
}
```

Add `"TASTYTRADE_ACCOUNT_ID": "your_account_id"` only if your grant exposes more than one account.

### Remote (Cloudflare Workers)

The Worker is configured in `cloudflare.config.ts` and deployed with the [`cf` CLI](https://www.npmjs.com/package/cf). From a checkout:

```bash
npm install
npx cf auth login
npm run deploy                                         # builds and deploys the Worker
# with the values exported in your shell; MCP_BEARER_TOKEN is e.g. the output of: openssl rand -hex 32
for name in TASTYTRADE_CLIENT_SECRET TASTYTRADE_REFRESH_TOKEN MCP_BEARER_TOKEN; do
  npx cf workers secrets update "$name" --worker tasty-agent --text "${!name}"
done
# only for multi-account grants:
npx cf workers secrets update TASTYTRADE_ACCOUNT_ID --worker tasty-agent --text "$TASTYTRADE_ACCOUNT_ID"
```

The server is then at `https://tasty-agent.<your-subdomain>.workers.dev/mcp` (streamable HTTP). Connect with the bearer token, for example in Claude Code:

```bash
claude mcp add --transport http tastytrade https://tasty-agent.<your-subdomain>.workers.dev/mcp \
  --header "Authorization: Bearer $MCP_BEARER_TOKEN"
```

To redeploy on every push, connect your fork under the Worker's **Settings → Builds** in the Cloudflare dashboard, with build command `npm run workers-builds:build` and deploy command `npx cf build && npx cf deploy --prebuilt`; a failing check stops the deploy.

The Worker serves only your own account: anyone holding the bearer token can trade it, so treat the token like the refresh token.

### Reusing the Tastytrade client

The REST client, rate gate, tick-size rules, and DXLink feed have no MCP dependency and are exported separately:

```ts
import { createTastytradeClient, IntervalGate, collectFeedEvents } from 'tasty-agent/tastytrade'
```

`createTastytradeClient` authenticates with either an OAuth grant it refreshes itself (`clientSecret` / `refreshToken`, each a string or an async getter read only when a token is minted) or a caller-minted `accessToken`, which is never refreshed or retried. Every request, including token refreshes, goes through the `gate` you pass. `request(path, { raw: true })` returns the whole response body (with `pagination`), and `signal` aborts a request early. A mutation that fails without a definite answer raises `TastytradeOutcomeUnknownError`. spicytrade uses this client for its own transport.

## Examples

```
"Get my account balances and current positions"
"What's my net liquidating value?"
"Get real-time quotes for SPY and AAPL"
"Get quotes for a TQQQ call at strike 100 expiring YYYY-MM-DD" (use a concrete listed expiration)
"Get Greeks for an AAPL put at strike 150 expiring YYYY-MM-DD" (use a concrete listed expiration)
"Get Greeks for an /ES call at strike 5800 expiring YYYY-MM-DD" (use a concrete listed expiration)
"Buy to open 100 AAPL shares at mid"
"Buy to open 17 TQQQ calls at strike 100 expiring YYYY-MM-DD" (use a concrete listed expiration)
"Buy $50K of TSLA calls at strike 300 expiring YYYY-MM-DD" (use a concrete listed expiration)
"Place an AAPL 150/155 call spread expiring YYYY-MM-DD" (use a concrete listed expiration)
"Buy one /ES-CONTRACT future at mid" (use a concrete active contract symbol)
"Reprice order 12345 at mid"
"Cancel order 12345"
"Show my live orders"
"Get my trading history from January"
"Get my order history for SPY"
"Get my private watchlists"
"Add TSLA and NVDA to my tech watchlist"
```

## Development

```bash
npm install
npm run check     # type-check the Node and Worker builds
npm test          # unit tests; test/integration.test.ts runs live only when credentials are set
npm run dev       # local Worker via Vite (put secrets in .dev.vars, see .dev.vars.example)

# Debug with MCP inspector
npm run build && npx @modelcontextprotocol/inspector node dist/stdio.js
```

## License

MIT
