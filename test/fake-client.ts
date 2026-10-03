import { Decimal } from 'decimal.js'

import { Broker } from '../src/broker.js'
import { ChainCache, type InstrumentDetail } from '../src/instruments.js'
import { parseTickSizes } from '../src/tastytrade/tick-sizes.js'
import type { RequestOptions, TastytradeClient } from '../src/tastytrade/client.js'
import type { JsonValue } from '../src/tastytrade/json.js'

export type Call = { method: string; path: string; query?: RequestOptions['query']; body?: JsonValue }
/** Fixtures are plain object literals; the fake trusts them to be JSON-shaped. */
export type Route = (call: Call) => unknown

/**
 * A client whose responses come from `route`. Returning undefined fails the test loudly, so every
 * broker request a test makes must be one it expected.
 */
export function fakeClient(route: Route): TastytradeClient & { calls: Call[] } {
  const calls: Call[] = []
  return {
    calls,
    async request(path, options = {}) {
      const call: Call = { method: options.method ?? 'GET', path }
      if (options.query) call.query = options.query
      if (options.body !== undefined) call.body = options.body
      calls.push(call)
      const response = await route(call)
      if (response === undefined) throw new Error(`Unexpected request: ${call.method} ${path}`)
      return response as JsonValue
    },
  }
}

export function fakeBroker(route: Route, accountId = '5WT00001') {
  const client = fakeClient((call) => {
    if (call.path === '/customers/me/accounts') return { items: [{ account: { 'account-number': accountId } }] }
    return route(call)
  })
  return { client, broker: new Broker({ client, accountId: undefined, chains: new ChainCache() }) }
}

export function parseToolXml(text: string, tag: string): unknown {
  const match = new RegExp(`^<${tag}>([\\s\\S]*)</${tag}>$`).exec(text)
  if (!match) throw new Error(`Not a <${tag}> payload: ${text}`)
  return JSON.parse(match[1]!.replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&'))
}

/** Resolved instruments for pricing and output tests; tick schedules take the provider's JSON shape. */
export function equity(symbol: string, tickSizes: unknown = [{ value: '0.01' }]): InstrumentDetail {
  return { kind: 'Equity', symbol, streamerSymbol: symbol, label: symbol, tickSizes: parseTickSizes(tickSizes as never, symbol) }
}

export function option(symbol: string, optionTickSizes?: unknown): InstrumentDetail {
  return {
    kind: 'Equity Option',
    symbol,
    streamerSymbol: symbol,
    label: symbol,
    tickSizes: parseTickSizes(optionTickSizes as never, symbol),
    sharesPerContract: new Decimal(100),
  }
}

export function future(symbol: string): InstrumentDetail {
  return { kind: 'Future', symbol, streamerSymbol: symbol, label: symbol, tickSizes: [{ threshold: null, value: new Decimal('0.25') }] }
}
