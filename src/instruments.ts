import { Decimal } from 'decimal.js'
import { z } from 'zod'

import { decimalText } from './compact.js'
import { sharedLoad } from './shared-load.js'
import { requestItems, requestObject, type TastytradeClient } from './tastytrade/client.js'
import { jsonDecimal, jsonObjects, jsonText, type JsonObject } from './tastytrade/json.js'
import { parseTickSizes, type TickSize } from './tastytrade/tick-sizes.js'

const OPTION_FIELDS = {
  option_type: z.enum(['C', 'P']).describe('C=call, P=put.'),
  strike_price: z.number().positive().describe('Strike price.'),
  expiration_date: z.iso.date().describe('YYYY-MM-DD.'),
}
const OPTIONAL_OPTION_FIELDS = {
  option_type: OPTION_FIELDS.option_type.optional().describe('C=call, P=put; required for options.'),
  strike_price: OPTION_FIELDS.strike_price.optional().describe('Required for options.'),
  expiration_date: OPTION_FIELDS.expiration_date.optional().describe('YYYY-MM-DD; required for options.'),
}

type OptionFields = { option_type?: unknown; strike_price?: unknown; expiration_date?: unknown }

function suppliedOptionFields(spec: OptionFields): number {
  return [spec.option_type, spec.strike_price, spec.expiration_date].filter((value) => value !== undefined).length
}

function requireOptionFieldsTogether(spec: OptionFields, ctx: z.RefinementCtx): boolean {
  const supplied = suppliedOptionFields(spec)
  if (supplied === 0 || supplied === 3) return true
  ctx.addIssue({ code: 'custom', message: 'option_type, strike_price, and expiration_date must be supplied together' })
  return false
}

export const InstrumentSpecSchema = z
  .object({
    symbol: z.string().min(1).toUpperCase().describe('Symbol, e.g. AAPL, /ESH26, SPX.'),
    instrument_type: z
      .enum(['Equity', 'Future', 'Index'])
      .optional()
      .describe('Omit to infer Equity/Future/Option; use Index for SPX, VIX, NDX.'),
    ...OPTIONAL_OPTION_FIELDS,
  })
  .superRefine((spec, ctx) => {
    if (spec.instrument_type !== undefined && suppliedOptionFields(spec)) {
      ctx.addIssue({
        code: 'custom',
        message: 'instrument_type cannot be combined with option_type, strike_price, or expiration_date',
      })
    }
    requireOptionFieldsTogether(spec, ctx)
  })

export const OptionSpecSchema = z.object({
  symbol: z.string().min(1).toUpperCase().describe('Underlying symbol, e.g. AAPL or /ES.'),
  ...OPTION_FIELDS,
})

const POSITION_EFFECT_ACTIONS = ['Buy to Open', 'Buy to Close', 'Sell to Open', 'Sell to Close'] as const
const DIRECTIONAL_ACTIONS = ['Buy', 'Sell'] as const
const BUY_ACTIONS: ReadonlySet<string> = new Set(['Buy', 'Buy to Open', 'Buy to Close'])

export function isBuyAction(action: string): boolean {
  return BUY_ACTIONS.has(action)
}

export const OrderLegSchema = z
  .object({
    symbol: z.string().min(1).toUpperCase().describe('Underlying stock or future symbol, e.g. AAPL or /ESM26.'),
    action: z
      .enum([...POSITION_EFFECT_ACTIONS, ...DIRECTIONAL_ACTIONS])
      .describe('Equities/options: Buy/Sell to Open/Close. Futures: Buy or Sell.'),
    quantity: z
      .number()
      .int()
      .min(1)
      .default(1)
      .describe(
        'Actual share/contract count. For target_value sizing, omit quantity for single-leg orders; ' +
          'for multi-leg spreads, use quantity only to express the leg ratio, such as 1:1 or 2:1.',
      ),
    ...OPTIONAL_OPTION_FIELDS,
  })
  .superRefine((leg, ctx) => {
    if (!requireOptionFieldsTogether(leg, ctx)) return
    const isFuture = resolveInstrumentKind(leg) === 'Future'
    const allowed: readonly string[] = isFuture ? DIRECTIONAL_ACTIONS : POSITION_EFFECT_ACTIONS
    if (!allowed.includes(leg.action)) {
      ctx.addIssue({
        code: 'custom',
        message: isFuture
          ? "Futures must use 'Buy' or 'Sell'."
          : 'Equities and options must use one of: Buy to Open, Buy to Close, Sell to Open, Sell to Close.',
      })
    }
  })
type InstrumentKind = 'Equity' | 'Index' | 'Equity Option' | 'Future' | 'Future Option'

/** A live order leg: its instrument with the action and quantity the broker holds. */
export type ResolvedLeg = { detail: InstrumentDetail; action: string; quantity: number }

/** Everything pricing, quoting and order building need to know about one resolved instrument. */
export interface InstrumentDetail {
  kind: InstrumentKind
  /** The broker symbol: what order legs and REST market data name. */
  symbol: string
  /** The DXLink symbol, for streamed Greeks. */
  streamerSymbol: string
  /** A human label for messages, e.g. "MSFT C450 2028-01-21". */
  label: string
  /** The broker's tick schedule; empty when the broker supplied none (pricing then refuses). */
  tickSizes: TickSize[]
  sharesPerContract?: Decimal | undefined
}

/** The REST market-data parameter for each kind (`/market-data/by-type?equity=...`). */
export const MARKET_DATA_TYPE: Record<InstrumentKind, string> = {
  Equity: 'equity',
  Index: 'index',
  'Equity Option': 'equity-option',
  Future: 'future',
  'Future Option': 'future-option',
}

type SpecLike = {
  symbol: string
  instrument_type?: 'Equity' | 'Future' | 'Index' | undefined
  option_type?: 'C' | 'P' | undefined
  strike_price?: number | undefined
  expiration_date?: string | undefined
}

export function resolveInstrumentKind(spec: SpecLike): InstrumentKind {
  if (spec.instrument_type) return spec.instrument_type
  if (spec.option_type) return spec.symbol.startsWith('/') ? 'Future Option' : 'Equity Option'
  return spec.symbol.startsWith('/') ? 'Future' : 'Equity'
}

/** One contract from a nested option chain, kept compact for the 24-hour chain cache. */
interface ChainContract {
  expiration: string
  strike: string
  optionType: 'C' | 'P'
  symbol: string
  streamerSymbol: string
  sharesPerContract?: string | undefined
  tickSizes: TickSize[]
}

/** Contract metadata changes rarely; a day-long cache avoids repeating multi-MB chain reads. */
const OPTION_CHAIN_CACHE_TTL_MS = 24 * 60 * 60 * 1000
// Bounds memory by contracts held, not chains: one index chain can outweigh dozens of others.
const DEFAULT_MAX_CACHED_CONTRACTS = 250_000

export class ChainCache {
  private readonly entries = new Map<string, { expiresAt: number; contracts: ChainContract[] }>()
  private size = 0

  constructor(
    private readonly maxContracts = DEFAULT_MAX_CACHED_CONTRACTS,
    private readonly ttlMs = OPTION_CHAIN_CACHE_TTL_MS,
    private readonly now: () => number = Date.now,
  ) {}

  get(key: string): ChainContract[] | undefined {
    const entry = this.entries.get(key)
    if (!entry || this.now() >= entry.expiresAt) {
      this.delete(key)
      return undefined
    }
    // Re-insert so eviction (oldest insertion first) drops the least recently used chain.
    this.entries.delete(key)
    this.entries.set(key, entry)
    return entry.contracts
  }

  set(key: string, contracts: ChainContract[]): void {
    this.delete(key)
    this.entries.set(key, { expiresAt: this.now() + this.ttlMs, contracts })
    this.size += contracts.length
    for (const oldest of this.entries.keys()) {
      if (this.size <= this.maxContracts || oldest === key) break
      this.delete(oldest)
    }
  }

  private delete(key: string): void {
    const entry = this.entries.get(key)
    if (!entry) return
    this.size -= entry.contracts.length
    this.entries.delete(key)
  }
}

type BySymbol = Promise<Map<string, JsonObject>>

const equityChainKey = (symbol: string) => `option_chain:${symbol}`

export class InstrumentResolver {
  // Concurrent legs on one underlying share a single chain download.
  private readonly pendingChains = new Map<string, Promise<unknown>>()

  constructor(
    private readonly client: TastytradeClient,
    private readonly chains: ChainCache,
  ) {}

  async resolveAll(specs: readonly SpecLike[]): Promise<InstrumentDetail[]> {
    const legs = specs.map((spec) => ({ spec, symbol: spec.symbol, kind: resolveInstrumentKind(spec) }))
    const symbolsOf = (...kinds: InstrumentKind[]) => legs.filter((leg) => kinds.includes(leg.kind)).map((leg) => leg.symbol)
    const { equities, futures } = this.prefetch({
      equities: symbolsOf('Equity', 'Index'),
      futures: symbolsOf('Future'),
      optionUnderlyings: symbolsOf('Equity Option'),
    })
    return Promise.all(
      legs.map(async ({ spec, symbol, kind }) => {
        switch (kind) {
          case 'Equity Option':
            return this.optionContract(spec, symbol, kind, await this.equityOptionChain(symbol, equities))
          case 'Future Option':
            return this.optionContract(spec, symbol, kind, await this.futureOptionChain(symbol))
          case 'Future':
            return futureDetail((await futures).get(symbol)!, symbol)
          case 'Equity':
          case 'Index':
            return equityDetail((await equities).get(symbol)!, symbol, kind === 'Index')
        }
      }),
    )
  }

  /** Resolves the legs of a live order, which name their instruments by broker symbol. */
  async resolveOrderLegs(orderLegs: readonly JsonObject[], underlying: string): Promise<ResolvedLeg[]> {
    const legs = orderLegs.map((leg) => {
      const symbol = jsonText(leg.symbol)
      const type = jsonText(leg['instrument-type'])
      const action = jsonText(leg.action)
      const quantity = Number(jsonText(leg.quantity))
      if (!symbol || !type || !action || !Number.isInteger(quantity) || quantity <= 0) {
        throw new Error('Live order leg is missing its symbol, instrument-type, action, or whole-number quantity')
      }
      return { symbol, type, action, quantity }
    })
    const symbolsOf = (type: string) => legs.filter((leg) => leg.type === type).map((leg) => leg.symbol)
    // An equity option leg already names its contract; pricing needs only the underlying's option ticks.
    const { equities, futures, futureOptions } = this.prefetch({
      equities: [...symbolsOf('Equity'), ...(symbolsOf('Equity Option').length ? [underlying] : [])],
      futures: symbolsOf('Future'),
      futureOptions: symbolsOf('Future Option'),
    })
    const resolve = async ({ symbol, type }: (typeof legs)[number]): Promise<InstrumentDetail> => {
      switch (type) {
        case 'Equity':
          return equityDetail((await equities).get(symbol)!, symbol, false)
        case 'Future':
          return futureDetail((await futures).get(symbol)!, symbol)
        case 'Equity Option':
          return {
            kind: type,
            symbol,
            streamerSymbol: symbol,
            label: symbol,
            tickSizes: parseTickSizes((await equities).get(underlying)!['option-tick-sizes'], underlying),
          }
        case 'Future Option': {
          const root = jsonText((await futureOptions).get(symbol)!['root-symbol'])
          if (!root) throw new Error(`Tastytrade instrument ${symbol} is missing root-symbol`)
          const contract = (await this.futureOptionChain(root)).find((candidate) => candidate.symbol === symbol)
          if (!contract) throw new Error(`${symbol} is not in the ${root} option chain`)
          return optionDetail(contract, root, type)
        }
        default:
          throw new Error(`Replacement pricing is not supported for ${type} legs`)
      }
    }
    return Promise.all(legs.map(async (leg) => ({ detail: await resolve(leg), action: leg.action, quantity: leg.quantity })))
  }

  /**
   * Starts one list request per instrument type. The equities request also carries the underlyings
   * of equity option chains not yet cached, since building a chain needs its underlying's tick schedule.
   */
  private prefetch(symbols: { equities: string[]; futures: string[]; futureOptions?: string[]; optionUnderlyings?: string[] }) {
    const uncached = (symbols.optionUnderlyings ?? []).filter((symbol) => !this.chains.get(equityChainKey(symbol)))
    return {
      equities: this.bySymbol('/instruments/equities', [...symbols.equities, ...uncached]),
      futures: this.bySymbol('/instruments/futures', symbols.futures),
      futureOptions: this.bySymbol('/instruments/future-options', symbols.futureOptions ?? []),
    }
  }

  /** Instruments of one type by symbol, fetched in one list request. */
  private async bySymbol(path: string, symbols: readonly string[]): BySymbol {
    const unique = [...new Set(symbols)]
    if (!unique.length) return new Map()
    const rows = await requestItems(this.client, path, { query: { 'symbol[]': unique } })
    const found = new Map(rows.map((row) => [jsonText(row.symbol) ?? '', row]))
    const missing = unique.filter((symbol) => !found.has(symbol))
    if (missing.length) throw new Error(`Tastytrade has no instrument for: [${missing.join(', ')}]`)
    return found
  }

  private optionContract(
    spec: SpecLike,
    symbol: string,
    kind: 'Equity Option' | 'Future Option',
    chain: ChainContract[],
  ): InstrumentDetail {
    // The schemas guarantee all three option fields are present together.
    const { option_type: optionType, expiration_date: expiration } = spec
    const strike = new Decimal(spec.strike_price!)
    const noun = kind === 'Equity Option' ? 'option' : 'futures option'
    const onDate = chain.filter((contract) => contract.expiration === expiration)
    if (!onDate.length) {
      const dates = [...new Set(chain.map((contract) => contract.expiration))].sort()
      throw new Error(`No ${noun}s found for ${symbol} expiration ${expiration}. Available: [${dates.join(', ')}]`)
    }
    const sameSide = onDate.filter((contract) => contract.optionType === optionType)
    const matches = sameSide.filter((contract) => strike.eq(contract.strike))
    const wanted = `${symbol} ${expiration} ${optionType} ${decimalText(strike)}`
    if (matches.length > 1) throw new Error(`Ambiguous ${noun} contract: ${wanted}`)
    if (!matches[0]) {
      const strikes = [...new Set(sameSide.map((candidate) => candidate.strike))].sort((a, b) => new Decimal(a).comparedTo(b))
      const title = noun.charAt(0).toUpperCase() + noun.slice(1)
      throw new Error(`${title} not found: ${wanted}. Available strikes: [${strikes.join(', ')}]`)
    }
    return optionDetail(matches[0], symbol, kind)
  }

  /** The equity option chain, with the underlying's option tick schedule attached to each contract. */
  private equityOptionChain(symbol: string, equities: BySymbol): Promise<ChainContract[]> {
    return this.cachedChain(equityChainKey(symbol), async () => {
      const [chains, underlying] = await Promise.all([
        requestItems(this.client, `/option-chains/${encodeURIComponent(symbol)}/nested`),
        equities,
      ])
      const tickSizes = parseTickSizes(underlying.get(symbol)!['option-tick-sizes'], symbol)
      return chains.flatMap((chain) =>
        jsonObjects(chain.expirations).flatMap((expiration) =>
          strikeContracts(expiration, jsonText(chain['shares-per-contract']), tickSizes),
        ),
      )
    })
  }

  /** The futures option chain; each expiration carries its own tick schedule. */
  private futureOptionChain(symbol: string): Promise<ChainContract[]> {
    const root = symbol.replace(/^\//, '')
    return this.cachedChain(`future_option_chain:${root}`, async () => {
      const data = await requestObject(this.client, `/futures-option-chains/${encodeURIComponent(root)}/nested`)
      return jsonObjects(data['option-chains']).flatMap((chain) =>
        jsonObjects(chain.expirations).flatMap((expiration) =>
          strikeContracts(expiration, undefined, parseTickSizes(expiration['tick-sizes'], symbol)),
        ),
      )
    })
  }

  private cachedChain(key: string, load: () => Promise<ChainContract[]>): Promise<ChainContract[]> {
    const cached = this.chains.get(key)
    if (cached) return Promise.resolve(cached)
    return sharedLoad(this.pendingChains, key, async () => {
      const contracts = await load()
      this.chains.set(key, contracts)
      return contracts
    })
  }
}

function equityDetail(equity: JsonObject, symbol: string, requestedIndex: boolean): InstrumentDetail {
  // The broker's flag decides: SPX named without instrument_type is still a quote-only index.
  const isIndex = requestedIndex || equity['is-index'] === true
  const streamerSymbol = jsonText(equity['streamer-symbol'])
  if (isIndex && !streamerSymbol) throw new Error(`Index is missing streamer symbol: ${symbol}`)
  return {
    kind: isIndex ? 'Index' : 'Equity',
    symbol,
    streamerSymbol: isIndex ? streamerSymbol! : symbol,
    label: symbol,
    tickSizes: parseTickSizes(equity['tick-sizes'], symbol),
  }
}

function futureDetail(future: JsonObject, symbol: string): InstrumentDetail {
  const streamerSymbol = jsonText(future['streamer-symbol'])
  if (!streamerSymbol) throw new Error(`Future contract is missing streamer symbol: ${symbol}`)
  const size = jsonDecimal(future['tick-size'])
  return {
    kind: 'Future',
    symbol: jsonText(future.symbol) ?? symbol,
    streamerSymbol,
    label: symbol,
    // A future has one fixed tick: a single unbounded tier.
    tickSizes: size?.gt(0) ? [{ threshold: null, value: size }] : [],
  }
}

function strikeContracts(expiration: JsonObject, sharesPerContract: string | undefined, tickSizes: TickSize[]): ChainContract[] {
  const date = jsonText(expiration['expiration-date'])
  if (!date) return []
  return jsonObjects(expiration.strikes).flatMap((strike) => {
    const price = jsonDecimal(strike['strike-price'])
    if (!price) return []
    return (['C', 'P'] as const).flatMap((optionType) => {
      const side = optionType === 'C' ? 'call' : 'put'
      const symbol = jsonText(strike[side])
      const streamerSymbol = jsonText(strike[`${side}-streamer-symbol`])
      if (!symbol || !streamerSymbol) return []
      return [{ expiration: date, strike: decimalText(price), optionType, symbol, streamerSymbol, sharesPerContract, tickSizes }]
    })
  })
}

function optionDetail(contract: ChainContract, underlying: string, kind: 'Equity Option' | 'Future Option'): InstrumentDetail {
  const sharesPerContract = jsonDecimal(contract.sharesPerContract)
  return {
    kind,
    symbol: contract.symbol,
    streamerSymbol: contract.streamerSymbol,
    label: `${underlying} ${contract.optionType}${contract.strike} ${contract.expiration}`,
    tickSizes: contract.tickSizes,
    sharesPerContract: sharesPerContract?.gt(0) ? sharesPerContract : undefined,
  }
}
