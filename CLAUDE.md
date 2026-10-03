# tasty-agent

MCP server for Tastytrade account data, market data, watchlists, and order workflows. TypeScript, calling the Tastytrade REST API and DXLink directly; runs over stdio (`npx tasty-agent`) and on Cloudflare Workers.

## Code index

- `src/tastytrade/` — standalone Tastytrade client (no MCP imports; exported as `tasty-agent/tastytrade` for reuse, e.g. by spicytrade): OAuth token refresh, request gate, API versions, errors, tick-size schedules, DXLink feed
- `src/server.ts` — MCP tool, prompt, and schema registration
- `src/instruments.ts` — instrument specs, resolution, nested option-chain cache
- `src/pricing.ts` — quote-derived signed mid pricing, tick rounding, budget sizing
- `src/orders.ts` — dry-run-first placement, replacement, cancellation, live orders
- `src/market.ts` — REST quotes, DXLink Greeks, metrics, market status, symbol search
- `src/account.ts` — compact balances, positions, orders, transactions, history
- `src/watchlists.ts` — watchlist operations
- `src/broker.ts` — per-request broker context, account selection, and `createBroker` wiring
- `src/stdio.ts` — stdio entry point (npm bin)
- `src/version.ts` — the server version (keep in step with `package.json`)
- `src/shared-load.ts` — in-flight de-duplication for per-request lookups
- `src/worker/` — Cloudflare Worker entry point (bearer auth, `/mcp`) and the `BrokerGate` Durable Object
- `test/` — vitest unit tests with a fake client; `integration.test.ts` is credential-gated
- `commands/portfolio.md` — Claude Code command surface
- `skills/trading/SKILL.md` — Claude Code trading skill

## Boundaries

- Every broker call goes through `TastytradeClient.request`, which acquires the shared gate (two requests per second) first. Never call `fetch` against Tastytrade elsewhere.
- Shared code under `src/` must use only web-standard APIs so it runs on both Node and Workers; Node-only code lives in `src/stdio.ts`, Workers-only code in `src/worker/`.
- On Workers, module state may hold settled values only (tokens, account number, chains) — never a pending promise.
- Option chains use the 24-hour `ChainCache`; tests that depend on chain changes must use a fresh cache.
- Order pricing must use `pricing.ts` with `decimal.js`, never JS floats; preserve signed debit/credit semantics. Every placement and replacement is dry-run first and refused on any dry-run warning; mutations are never retried, and an unanswered one is reported as an unknown outcome.
- Keep MCP output compact and never replace selected projections with full API payloads.

## Commands

```sh
npm run check
npm test
npm run build
npx wrangler deploy --dry-run --outdir dist-worker
```

## Releasing

There is no GitHub CI. Cloudflare Workers Builds deploys the Worker on every push to `main`, running `npm run workers-builds:build` (type-check and tests) before `npx wrangler deploy`; a failing check stops the deploy.

To release the npm package, bump the version in `package.json`, `src/version.ts`, and `.claude-plugin/plugin.json` together, then run `npm publish --access public` locally (`prepublishOnly` checks, tests, and builds first) and push a `v<version>` tag.

Credential-gated tests and live brokerage calls require explicit authorization.
