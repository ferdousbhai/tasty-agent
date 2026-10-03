import { ChainCache, InstrumentResolver } from './instruments.js'
import { sharedLoad } from './shared-load.js'
import { SERVER_VERSION } from './version.js'
import {
  createTastytradeClient,
  requestItems,
  requestObject,
  type TastytradeClient,
  type TastytradeClientOptions,
} from './tastytrade/client.js'
import { loadQuoteToken, type QuoteToken } from './tastytrade/dxlink.js'
import { jsonObject, jsonText, type JsonObject } from './tastytrade/json.js'

const HOUR_MS = 60 * 60 * 1000

/** Values remembered across requests: settled values only, never promises. */
export type BrokerMemo = Record<string, { value: unknown; expiresAt: number }>

interface BrokerOptions {
  client: TastytradeClient
  /** TASTYTRADE_ACCOUNT_ID; may be omitted only when the grant exposes exactly one account. */
  accountId?: string | undefined
  chains: ChainCache
  memo?: BrokerMemo | undefined
}

/** One request's view of the broker: the client, instrument resolution, and the trading account. */
export class Broker {
  readonly client: TastytradeClient
  readonly instruments: InstrumentResolver
  private readonly accountId: string | undefined
  private readonly memo: BrokerMemo
  // Concurrent tool calls on one broker share a lookup; only settled values reach `memo`.
  private readonly pending = new Map<string, Promise<unknown>>()

  constructor(options: BrokerOptions) {
    this.client = options.client
    this.instruments = new InstrumentResolver(options.client, options.chains)
    // A blank TASTYTRADE_ACCOUNT_ID means unset.
    this.accountId = options.accountId?.trim() || undefined
    this.memo = options.memo ?? {}
  }

  /** The `/accounts/{number}` prefix for the trading account. */
  async accountPath(): Promise<string> {
    const accountNumber = await this.remember('account-number', Number.POSITIVE_INFINITY, async () => {
      const available = (await requestItems(this.client, '/customers/me/accounts'))
        .map((row) => jsonText(jsonObject(row.account)?.['account-number'] ?? row['account-number']))
        .filter((accountNumber) => accountNumber !== undefined)
      return selectAccount(available, this.accountId)
    })
    return `/accounts/${encodeURIComponent(accountNumber)}`
  }

  /** The DXLink token; it outlives a day of use, so refetching it per Greeks call would waste a request. */
  quoteToken(): Promise<QuoteToken> {
    return this.remember('quote-token', 12 * HOUR_MS, () => loadQuoteToken(this.client))
  }

  /** The equity market holiday calendar, which changes a few times a year. */
  holidayCalendar(): Promise<JsonObject> {
    return this.remember('holidays', 24 * HOUR_MS, () => requestObject(this.client, '/market-time/equities/holidays'))
  }

  private remember<T>(key: string, ttlMs: number, load: () => Promise<T>): Promise<T> {
    const cached = this.memo[key]
    if (cached && Date.now() < cached.expiresAt) return Promise.resolve(cached.value as T)
    return sharedLoad(this.pending, key, async () => {
      const value = await load()
      this.memo[key] = { value, expiresAt: Date.now() + ttlMs }
      return value
    })
  }
}

// Distributes over the credential union, which a plain Omit would collapse.
type ClientConfig = TastytradeClientOptions extends infer O ? (O extends unknown ? Omit<O, 'userAgent'> : never) : never
type BrokerConfig = ClientConfig & Omit<BrokerOptions, 'client'>

/** The production wiring shared by the stdio server, the Worker, and the live integration test. */
export function createBroker(config: BrokerConfig): Broker {
  const client = createTastytradeClient({ ...config, userAgent: `tasty-agent/${SERVER_VERSION}` })
  return new Broker({ client, accountId: config.accountId, chains: config.chains, memo: config.memo })
}

export function selectAccount(available: readonly string[], accountId: string | undefined): string {
  if (!available.length) throw new Error('No Tastytrade accounts are available for these credentials.')
  if (accountId) {
    if (!available.includes(accountId)) {
      throw new Error(`Account '${accountId}' not found. Available: [${available.join(', ')}]`)
    }
    return accountId
  }
  if (available.length > 1) {
    throw new Error(
      `TASTYTRADE_ACCOUNT_ID is required when credentials expose multiple accounts. Available: [${available.join(', ')}]`,
    )
  }
  return available[0]!
}
