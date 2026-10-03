import { McpServer, type CallToolResult } from '@modelcontextprotocol/server'
import { z } from 'zod'

import { accountHistory, accountOverview, HistoryInput, OverviewInput } from './account.js'
import type { Broker } from './broker.js'
import { toolXml } from './compact.js'
import {
  greeks,
  GreeksInput,
  marketMetrics,
  MarketMetricsInput,
  marketStatus,
  MarketStatusInput,
  quotes,
  QuotesInput,
  SearchInput,
  searchSymbols,
} from './market.js'
import { cancelOrder, listOrders, OrderIdInput, placeOrder, PlaceOrderInput, replaceOrder } from './orders.js'
import { SERVER_VERSION } from './version.js'
import { manageWatchlist, WatchlistInput } from './watchlists.js'

const READ_ONLY = { readOnlyHint: true, destructiveHint: false, openWorldHint: true } as const
const MUTATING = { readOnlyHint: false, destructiveHint: true, openWorldHint: true } as const
const NO_INPUT = z.object({})

type Tool<S extends z.ZodType> = {
  description: string
  input: S
  mutating?: boolean
  /** The element name the output is wrapped in. */
  tag: string
  run: (broker: Broker, args: z.output<S>) => Promise<unknown>
}

const tool = <S extends z.ZodType>(definition: Tool<S>) => definition

const TOOLS = {
  account_overview: tool({
    description:
      'Get balances and/or open positions. Use get_history(type="transactions", transaction_type="Money Movement") ' +
      'for deposits/withdrawals.',
    input: OverviewInput,
    tag: 'account_overview',
    run: accountOverview,
  }),
  get_history: tool({
    description: 'Get paginated transaction or order history.',
    input: HistoryInput,
    tag: 'history',
    run: accountHistory,
  }),
  place_order: tool({
    description:
      'Place a new order using live quote-derived mid pricing, rounded to the nearest valid tick. ' +
      'No manual limit price is accepted. Every order is first sent to the broker as a dry run; ' +
      'if the dry run returns any warning, the order is not submitted.\n\n' +
      'For options, set symbol to the underlying and include option_type, strike_price, expiration_date. ' +
      'Quantity is the actual share/contract count. With target_value, omit quantity for single-leg orders; ' +
      'for multi-leg spreads, use quantity only to express the leg ratio, such as 1:1 or 2:1.',
    input: PlaceOrderInput,
    mutating: true,
    tag: 'order',
    run: placeOrder,
  }),
  replace_order: tool({
    description: 'Reprice a live order once at the current quote-derived mid.',
    input: OrderIdInput,
    mutating: true,
    tag: 'order',
    run: replaceOrder,
  }),
  cancel_order: tool({
    description: 'Cancel a live order by id.',
    input: OrderIdInput,
    mutating: true,
    tag: 'order',
    run: cancelOrder,
  }),
  list_orders: tool({ description: 'List all live orders.', input: NO_INPUT, tag: 'orders', run: listOrders }),
  get_quotes: tool({
    description: 'Get live quotes for stocks, options, futures, and indices.',
    input: QuotesInput,
    tag: 'quotes',
    run: quotes,
  }),
  get_greeks: tool({
    description: 'Get option Greeks: delta, gamma, theta, vega, rho.',
    input: GreeksInput,
    tag: 'greeks',
    run: greeks,
  }),
  get_market_metrics: tool({
    description: 'Get IV/HV, beta, liquidity, valuation, dividends, earnings. IV rank/percentile are 0-1.',
    input: MarketMetricsInput,
    tag: 'market_metrics',
    run: marketMetrics,
  }),
  market_status: tool({
    description: 'Get exchange open/closed status, next open/close, holiday flags, and current NYC time.',
    input: MarketStatusInput,
    tag: 'market_status',
    run: marketStatus,
  }),
  search_symbols: tool({
    description: 'Search symbols by ticker or company name.',
    input: SearchInput,
    tag: 'symbol_search',
    run: searchSymbols,
  }),
  watchlist: tool({
    description:
      'Manage watchlists.\n\n' +
      'Actions:\n' +
      '  list: no name returns compact watchlists; with name returns symbols. Supports public/private.\n' +
      '  add: add symbols to a private watchlist; creates if missing.\n' +
      '  remove: remove symbols from a private watchlist.\n' +
      '  delete: delete a private watchlist by name.',
    input: WatchlistInput,
    mutating: true,
    tag: 'watchlist',
    run: manageWatchlist,
  }),
}

const IV_PROMPT = `Please analyze IV rank, percentile, and liquidity for:
1. All active positions in my account
2. All symbols in my watchlists

Focus on identifying extremes:
- Low IV rank (<.2) may present entry opportunities (cheap options)
- High IV rank (>.8) may present exit opportunities (expensive options)
- Also consider liquidity levels to ensure tradeable positions

Use account_overview, watchlist(action="list") for watchlist names, watchlist(action="list", name=...) for symbols, and get_market_metrics for IV/liquidity data.`

const IV_REPLY =
  "I'll analyze IV opportunities for your positions and watchlists. Let me start by gathering current positions and " +
  "watchlist names, then fetch each watchlist's symbols before getting market metrics."

/** Builds the MCP surface over one broker. */
export function createServer(broker: Broker): McpServer {
  const server = new McpServer({ name: 'tasty-agent', version: SERVER_VERSION })
  for (const [name, definition] of Object.entries(TOOLS) as [string, Tool<z.ZodType>][]) {
    server.registerTool(
      name,
      {
        description: definition.description,
        inputSchema: definition.input,
        annotations: definition.mutating ? MUTATING : READ_ONLY,
      },
      // The SDK turns a thrown error into an `isError` result carrying its message.
      async (args: unknown): Promise<CallToolResult> => ({
        content: [{ type: 'text', text: toolXml(definition.tag, await definition.run(broker, args)) }],
      }),
    )
  }
  server.registerPrompt('analyze_iv_opportunities', { title: 'IV Rank Analysis' }, () => ({
    messages: [
      { role: 'user', content: { type: 'text', text: IV_PROMPT } },
      { role: 'assistant', content: { type: 'text', text: IV_REPLY } },
    ],
  }))
  return server
}
