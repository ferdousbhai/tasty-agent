import { z } from 'zod'

import type { Broker } from './broker.js'
import { chunks, compactRow, decimalText, num, toTable, type Row } from './compact.js'
import { InstrumentSpecSchema, MARKET_DATA_TYPE, OptionSpecSchema, type InstrumentDetail } from './instruments.js'
import type { LegQuote } from './pricing.js'
import { naturalDelta, nyDate, nyIsoTime } from './time.js'
import { requestItems } from './tastytrade/client.js'
import { collectFeedEvents, type FeedEvent } from './tastytrade/dxlink.js'
import { errorMessage, TastytradeApiError } from './tastytrade/errors.js'
import { jsonDecimal, jsonObject, jsonText, type JsonObject, type JsonValue } from './tastytrade/json.js'

/** Tastytrade's market-data endpoints take at most 100 symbols per request, across all types. */
const MARKET_DATA_CHUNK = 100

const EXCHANGE = z.enum(['Equity', 'CME', 'CFE', 'Smalls'])
type Exchange = z.infer<typeof EXCHANGE>

export const QuotesInput = z.object({
  instruments: z
    .array(InstrumentSpecSchema)
    .min(1)
    .describe('Use symbol only for stocks/futures; set instrument_type="Index" for SPX/VIX/NDX; add option fields for options.'),
})
export const GreeksInput = z.object({
  options: z
    .array(OptionSpecSchema)
    .min(1)
    .max(100)
    .describe('Option contracts by underlying symbol, C/P, strike, and expiration_date.'),
  timeout: z.number().positive().max(30).default(10).describe('Seconds to wait for DXLink data (at most 30).'),
})
export const MarketMetricsInput = z.object({ symbols: z.array(z.string().trim().min(1)).min(1) })
export const MarketStatusInput = z.object({ exchanges: z.array(EXCHANGE).min(1).default(['Equity']) })
export const SearchInput = z.object({
  symbol: z.string().trim().min(1).describe('Query, e.g. AAPL or Apple.'),
  limit: z.number().int().positive().default(10).describe('Max results.'),
})

/** Live quote rows from REST market data, keyed by broker symbol. */
export async function fetchQuoteRows(broker: Broker, details: readonly InstrumentDetail[]): Promise<Map<string, JsonObject>> {
  const unique = [...new Map(details.map((detail) => [`${detail.kind}:${detail.symbol}`, detail])).values()]
  const pages = await Promise.all(
    chunks(unique, MARKET_DATA_CHUNK).map((chunk) => {
      const query: Record<string, string[]> = {}
      for (const detail of chunk) (query[MARKET_DATA_TYPE[detail.kind]] ??= []).push(detail.symbol)
      return requestItems(broker.client, '/market-data/by-type', { query })
    }),
  )
  const rows = new Map(pages.flat().map((row) => [jsonText(row.symbol) ?? '', row]))
  const missing = unique.filter((detail) => !rows.has(detail.symbol)).map((detail) => detail.symbol)
  if (missing.length) throw new Error(`Tastytrade returned no quote for: [${missing.join(', ')}]`)
  return rows
}

/** The two-sided market of a quote row: present, non-negative, and not crossed. */
export function legQuote(row: JsonObject, label: string): LegQuote {
  const bid = jsonDecimal(row.bid)
  const ask = jsonDecimal(row.ask)
  if (!bid) throw new Error(`Missing bid price for ${label}`)
  if (!ask) throw new Error(`Missing ask price for ${label}`)
  if (bid.isNegative() || ask.isNegative()) throw new Error(`Invalid quote for ${label}: bid/ask cannot be negative`)
  if (bid.gt(ask)) throw new Error(`Crossed quote for ${label}: bid ${bid.toString()} exceeds ask ${ask.toString()}`)
  return { bid, ask }
}

/** The actionable bid/ask/mid of a quote; an index with no two-sided market reports its last price. */
export function compactQuote(detail: InstrumentDetail, row: JsonObject): Row {
  const sym = detail.symbol
  if (detail.kind === 'Index' && !jsonDecimal(row.ask)?.gt(0)) {
    const last = jsonDecimal(row.last)
    if (!last) throw new Error(`Missing last price for ${sym}`)
    return { sym, last: decimalText(last) }
  }
  const { bid, ask } = legQuote(row, sym)
  return compactRow({
    sym,
    bid: decimalText(bid),
    ask: decimalText(ask),
    mid: decimalText(bid.plus(ask).div(2)),
    bid_sz: num(row['bid-size']),
    ask_sz: num(row['ask-size']),
  })
}

export async function quotes(broker: Broker, { instruments }: z.infer<typeof QuotesInput>): Promise<string> {
  const details = await broker.instruments.resolveAll(instruments)
  const rows = await fetchQuoteRows(broker, details)
  return toTable(details.map((detail) => compactQuote(detail, rows.get(detail.symbol)!)))
}

export function compactGreeks(event: FeedEvent): Row {
  return compactRow({
    sym: event.eventSymbol,
    price: event.price,
    iv: event.volatility,
    delta: event.delta,
    gamma: event.gamma,
    theta: event.theta,
    vega: event.vega,
    rho: event.rho,
  })
}

export async function greeks(broker: Broker, { options, timeout }: z.infer<typeof GreeksInput>): Promise<string> {
  const [details, quoteToken] = await Promise.all([broker.instruments.resolveAll(options), broker.quoteToken()])
  const symbols = [...new Set(details.map((detail) => detail.streamerSymbol))]
  try {
    const { events } = await collectFeedEvents({
      quoteToken,
      subscriptions: { Greeks: symbols },
      timeoutMs: timeout * 1000,
      isComplete: (collected) => symbols.every((symbol) => collected.Greeks.has(symbol)),
    })
    const missing = symbols.filter((symbol) => !events.Greeks.has(symbol))
    if (missing.length) {
      throw new Error(`Timeout getting Greeks after ${timeout}s. No data received for: [${missing.join(', ')}]`)
    }
    return toTable(details.map((detail) => compactGreeks(events.Greeks.get(detail.streamerSymbol)!)))
  } catch (error) {
    throw await withMarketContext(broker, symbols, new Error(errorMessage(error), { cause: error }))
  }
}

export function exchangesForSymbols(streamerSymbols: readonly string[]): Set<Exchange> {
  const exchanges = new Set<Exchange>()
  for (const symbol of streamerSymbols) {
    if (symbol.startsWith('/') || symbol.startsWith('./')) {
      exchanges.add(symbol.includes(':XCBF') || symbol.startsWith('/VX') || symbol.startsWith('./VX') ? 'CFE' : 'CME')
    } else {
      exchanges.add('Equity')
    }
  }
  return exchanges
}

type MarketSession = {
  exchange: string
  status: string
  openAt?: string | undefined
  closeAt?: string | undefined
  nextOpenAt?: string | undefined
}

async function marketSessions(broker: Broker, exchanges: readonly Exchange[]): Promise<MarketSession[]> {
  const rows = await requestItems(broker.client, '/market-time/sessions/current', {
    query: { 'instrument-collections[]': exchanges },
  })
  return rows.map((row) => ({
    exchange: jsonText(row['instrument-collection']) ?? '?',
    status: jsonText(row.state) ?? jsonText(row.status) ?? 'Unknown',
    openAt: jsonText(row['open-at']),
    closeAt: jsonText(row['close-at']),
    nextOpenAt: jsonText(jsonObject(row['next-session'])?.['open-at']),
  }))
}

export function nextOpenTime(session: MarketSession, now: Date): string | undefined {
  const time = (value: string | undefined) => (value === undefined ? undefined : Date.parse(value))
  if (session.status === 'Pre-market') return session.openAt
  if (session.status === 'Closed') {
    const openAt = time(session.openAt)
    const closeAt = time(session.closeAt)
    if (openAt !== undefined && now.getTime() < openAt) return session.openAt
    if (closeAt !== undefined && now.getTime() > closeAt && session.nextOpenAt) return session.nextOpenAt
  }
  if (session.status === 'Extended' && session.nextOpenAt) return session.nextOpenAt
  return undefined
}

/** Adds "the market is closed" context to a streaming failure without hiding the original error. */
async function withMarketContext(broker: Broker, streamerSymbols: readonly string[], primary: Error): Promise<Error> {
  try {
    const now = new Date()
    const closed = (await marketSessions(broker, [...exchangesForSymbols(streamerSymbols)]))
      .filter((session) => session.status !== 'Open')
      .map((session) => {
        const nextOpen = nextOpenTime(session, now)
        return nextOpen
          ? `${session.exchange} (opens in ${naturalDelta(Date.parse(nextOpen) - now.getTime())})`
          : `${session.exchange} (closed)`
      })
    if (!closed.length) return primary
    return new Error(
      `Market is currently closed: ${closed.join(', ')}. Live data is not available while the market is closed. ` +
        `(${primary.message})`,
      { cause: primary },
    )
  } catch (contextError) {
    return new Error(`${primary.message} (market-status lookup also failed: ${errorMessage(contextError)})`, { cause: primary })
  }
}

export async function marketStatus(broker: Broker, { exchanges }: z.infer<typeof MarketStatusInput>, now = new Date()): Promise<Row> {
  const [sessions, calendar] = await Promise.all([
    marketSessions(broker, exchanges),
    broker.holidayCalendar(),
  ])
  if (!sessions.length) throw new Error(`No market sessions found for exchanges: [${exchanges.join(', ')}]`)
  // The holiday calendar is keyed by New York dates, which diverge from the UTC date every evening.
  const today = nyDate(now)
  const listed = (field: string): JsonValue[] => (Array.isArray(calendar[field]) ? calendar[field] : [])
  const isHoliday = listed('market-holidays').includes(today)
  const isHalfDay = listed('market-half-days').includes(today)

  const results = sessions.map((session) => {
    const result: Row = { exchange: session.exchange, status: session.status }
    if (session.status === 'Open') {
      if (session.closeAt) result.close_at = session.closeAt
      return result
    }
    const nextOpen = nextOpenTime(session, now)
    if (nextOpen) {
      result.next_open = nextOpen
      result.time_until_open = naturalDelta(Date.parse(nextOpen) - now.getTime())
    }
    if (isHoliday) result.is_holiday = true
    if (isHalfDay) result.is_half_day = true
    return result
  })
  return { current_time_nyc: nyIsoTime(now), exchanges: results }
}

export function compactMarketMetric(metric: JsonObject): Row {
  const symbol = jsonText(metric.symbol)
  if (!symbol) throw new Error('Market metric is missing symbol')
  return compactRow(
    {
      symbol,
      iv_rank: num(metric['implied-volatility-index-rank']),
      iv_pct: num(metric['implied-volatility-percentile']),
      iv30: num(metric['implied-volatility-30-day']),
      hv30: num(metric['historical-volatility-30-day']),
      iv_hv30: num(metric['iv-hv-30-day-difference']),
      beta: num(metric.beta),
      liq: num(metric['liquidity-rating']),
      liq_rank: num(metric['liquidity-rank']),
      market_cap: num(metric['market-cap']),
      pe: num(metric['price-earnings-ratio']),
      eps: num(metric['earnings-per-share']),
      div_yield: num(metric['dividend-yield']),
      earnings: jsonText(jsonObject(metric.earnings)?.['expected-report-date']),
    },
    { dropZero: true },
  )
}

export async function marketMetrics(broker: Broker, { symbols }: z.infer<typeof MarketMetricsInput>): Promise<string> {
  const pages = await Promise.all(
    chunks(symbols, MARKET_DATA_CHUNK).map((chunk) =>
      requestItems(broker.client, '/market-metrics', { query: { symbols: chunk.join(',') } }),
    ),
  )
  return toTable(pages.flat().map(compactMarketMetric))
}

export async function searchSymbols(broker: Broker, { symbol, limit }: z.infer<typeof SearchInput>): Promise<string> {
  let results: JsonObject[]
  try {
    results = await requestItems(broker.client, `/symbols/search/${encodeURIComponent(symbol)}`)
  } catch (error) {
    // Search answers an unknown phrase with an error status; that is "no matches", not a failure.
    if (error instanceof TastytradeApiError && !error.ambiguous) return toTable([])
    throw error
  }
  return toTable(
    results.slice(0, limit).map((row) => compactRow({ symbol: jsonText(row.symbol), description: jsonText(row.description) })),
  )
}
