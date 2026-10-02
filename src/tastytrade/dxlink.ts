import { requestObject, type TastytradeClient } from './client.js'
import { jsonObject, jsonText, type JsonValue } from './json.js'

export type FeedEventType = 'Quote' | 'Trade' | 'Greeks'
export type FeedValue = number | string | undefined
export type FeedEvent = Record<string, FeedValue>
export type FeedEvents = Record<FeedEventType, Map<string, FeedEvent>>

const CHANNELS: Record<FeedEventType, number> = { Quote: 1, Trade: 3, Greeks: 7 }

export const FEED_FIELDS = {
  Quote: ['eventSymbol', 'bidPrice', 'askPrice', 'bidSize', 'askSize'],
  Trade: ['eventSymbol', 'price', 'change', 'size', 'dayVolume'],
  Greeks: ['eventSymbol', 'price', 'volatility', 'delta', 'gamma', 'theta', 'rho', 'vega'],
} as const satisfies Record<FeedEventType, readonly string[]>

const KEEPALIVE_TIMEOUT_SECONDS = 60
const KEEPALIVE_INTERVAL_MS = 30_000

export type QuoteToken = { token: string; url: string }

export async function loadQuoteToken(client: TastytradeClient): Promise<QuoteToken> {
  const data = await requestObject(client, '/api-quote-tokens')
  const token = jsonText(data.token)
  const url = jsonText(data['dxlink-url'])
  if (!token || !url?.startsWith('wss://')) throw new Error('Tastytrade quote token response is missing token or dxlink-url')
  return { token, url }
}

export class DxLinkError extends Error {
  override readonly name = 'DxLinkError'
}

export interface CollectFeedOptions {
  quoteToken: QuoteToken
  subscriptions: Partial<Record<FeedEventType, readonly string[]>>
  timeoutMs: number
  /** Ends collection early once it returns true; otherwise collection runs until the timeout. */
  isComplete?: (events: FeedEvents) => boolean
  webSocket?: new (url: string) => WebSocket
}

/**
 * Opens one short-lived DXLink connection, subscribes, and gathers the latest event per symbol.
 * Resolves with whatever arrived when `isComplete` holds or the timeout passes; rejects only when
 * the connection itself fails.
 */
export function collectFeedEvents(options: CollectFeedOptions): Promise<{ events: FeedEvents; timedOut: boolean }> {
  const events: FeedEvents = { Quote: new Map(), Trade: new Map(), Greeks: new Map() }
  const wanted = (Object.keys(CHANNELS) as FeedEventType[]).filter((type) => options.subscriptions[type]?.length)
  if (!wanted.length) return Promise.resolve({ events, timedOut: false })

  const SocketClass = options.webSocket ?? WebSocket
  return new Promise((resolve, reject) => {
    const socket = new SocketClass(options.quoteToken.url)
    let settled = false
    let authorized = false
    let keepalive: ReturnType<typeof setInterval> | undefined
    const configured = new Set<number>()

    const finish = (error?: Error, timedOut = false) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (keepalive) clearInterval(keepalive)
      try {
        socket.close(1000, 'done')
      } catch {
        // Already closed.
      }
      if (error) reject(error)
      else resolve({ events, timedOut })
    }
    const timer = setTimeout(() => finish(undefined, true), options.timeoutMs)
    const send = (message: Record<string, JsonValue>) => {
      if (!settled) socket.send(JSON.stringify(message))
    }

    socket.addEventListener('open', () => {
      send({
        type: 'SETUP',
        channel: 0,
        keepaliveTimeout: KEEPALIVE_TIMEOUT_SECONDS,
        acceptKeepaliveTimeout: KEEPALIVE_TIMEOUT_SECONDS,
        version: '0.1-DXF-JS/0.3.0',
      })
      // dxLink reports an initial UNAUTHORIZED state before it answers AUTH, so both go out at once.
      send({ type: 'AUTH', channel: 0, token: options.quoteToken.token })
    })
    socket.addEventListener('error', () => finish(new DxLinkError('DXLink connection failed')))
    socket.addEventListener('close', () => finish(new DxLinkError('DXLink connection closed before data arrived')))
    socket.addEventListener('message', (event: MessageEvent) => {
      try {
        if (typeof event.data !== 'string') throw new DxLinkError('DXLink sent a non-text frame')
        const message = jsonObject(JSON.parse(event.data))
        if (!message) throw new DxLinkError('DXLink sent a non-object frame')
        handle(message)
        if (options.isComplete?.(events)) finish()
      } catch (error) {
        finish(error instanceof Error ? error : new DxLinkError(String(error)))
      }
    })

    function handle(message: Record<string, JsonValue>) {
      const channel = typeof message.channel === 'number' ? message.channel : -1
      switch (message.type) {
        case 'SETUP':
        case 'KEEPALIVE':
          return
        case 'AUTH_STATE':
          if (message.state === 'AUTHORIZED') {
            authorized = true
            for (const type of wanted) {
              send({ type: 'CHANNEL_REQUEST', channel: CHANNELS[type], service: 'FEED', parameters: { contract: 'AUTO' } })
            }
            keepalive = setInterval(() => send({ type: 'KEEPALIVE', channel: 0 }), KEEPALIVE_INTERVAL_MS)
          } else if (authorized || message.state !== 'UNAUTHORIZED') {
            throw new DxLinkError('DXLink authorization failed')
          }
          return
        case 'CHANNEL_OPENED': {
          const type = typeForChannel(channel)
          send({
            type: 'FEED_SETUP',
            channel,
            acceptAggregationPeriod: 0.1,
            acceptDataFormat: 'COMPACT',
            acceptEventFields: { [type]: [...FEED_FIELDS[type]] },
          })
          send({
            type: 'FEED_SUBSCRIPTION',
            channel,
            reset: true,
            add: (options.subscriptions[type] ?? []).map((symbol) => ({ type, symbol })),
          })
          return
        }
        case 'FEED_CONFIG': {
          const type = typeForChannel(channel)
          const fields = jsonObject(message.eventFields)?.[type]
          if (fields === undefined) return
          if (!Array.isArray(fields) || fields.join(',') !== FEED_FIELDS[type].join(',')) {
            throw new DxLinkError(`DXLink configured unexpected ${type} fields`)
          }
          configured.add(channel)
          return
        }
        case 'FEED_DATA': {
          const type = typeForChannel(channel)
          // A channel whose layout is not yet confirmed cannot be decoded safely.
          if (!configured.has(channel)) return
          for (const row of compactRows(type, message.data)) {
            const symbol = row.eventSymbol
            if (typeof symbol === 'string' && hasObservation(row)) events[type].set(symbol, row)
          }
          return
        }
        case 'ERROR':
          throw new DxLinkError(`DXLink error: ${jsonText(message.error) ?? 'unknown'} ${jsonText(message.message) ?? ''}`.trim())
        case 'CHANNEL_CLOSED':
          throw new DxLinkError('DXLink closed a feed channel')
        default:
          return
      }
    }
  })
}

function typeForChannel(channel: number): FeedEventType {
  const type = (Object.keys(CHANNELS) as FeedEventType[]).find((candidate) => CHANNELS[candidate] === channel)
  if (!type) throw new DxLinkError(`DXLink used unexpected channel ${channel}`)
  return type
}

/** COMPACT data is `[type, [v1, v2, ...], type, [...]]`, each value list holding whole rows. */
export function compactRows(type: FeedEventType, data: JsonValue | undefined): FeedEvent[] {
  if (!Array.isArray(data) || data.length % 2 !== 0) throw new DxLinkError('DXLink sent malformed feed data')
  const fields = FEED_FIELDS[type]
  const rows: FeedEvent[] = []
  for (let offset = 0; offset < data.length; offset += 2) {
    if (data[offset] !== type) throw new DxLinkError('DXLink feed data does not match its channel')
    const values = data[offset + 1]
    if (!Array.isArray(values) || values.length % fields.length !== 0) {
      throw new DxLinkError('DXLink sent a malformed row batch')
    }
    for (let start = 0; start < values.length; start += fields.length) {
      const row: FeedEvent = {}
      fields.forEach((field, index) => {
        row[field] = feedValue(values[start + index])
      })
      rows.push(row)
    }
  }
  return rows
}

/** dxFeed spells "no value" as null, "", or a non-finite string such as "NaN". */
function feedValue(value: JsonValue | undefined): FeedValue {
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined
  if (typeof value !== 'string' || !value.trim()) return undefined
  if (['nan', 'infinity', '-infinity'].includes(value.trim().toLowerCase())) return undefined
  return value
}

/** An empty snapshot arrives as a synthetic row whose every value slot is absent. */
function hasObservation(row: FeedEvent): boolean {
  return Object.entries(row).some(([field, value]) => field !== 'eventSymbol' && value !== undefined)
}
