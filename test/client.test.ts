import { describe, expect, it, vi } from 'vitest'

import { buildQuery, createTastytradeClient } from '../src/tastytrade/client.js'
import {
  TastytradeApiError,
  TastytradeAuthError,
  TastytradeOutcomeUnknownError,
  TastytradeTransportError,
} from '../src/tastytrade/errors.js'
import { IntervalGate } from '../src/tastytrade/gate.js'
import { tastytradeApiVersion } from '../src/tastytrade/versions.js'

type Sent = { url: string; init: RequestInit }

function harness(responder: (sent: Sent) => Response | Promise<Response>) {
  const sent: Sent[] = []
  let gateCalls = 0
  let clock = 1_000_000
  const client = createTastytradeClient({
    clientSecret: 'secret',
    refreshToken: 'refresh',
    gate: {
      acquire: async () => {
        gateCalls += 1
      },
    },
    now: () => clock,
    fetch: async (input, init) => {
      const entry = { url: String(input), init: init ?? {} }
      sent.push(entry)
      if (entry.url.endsWith('/oauth/token')) return Response.json({ access_token: `token-${sent.length}`, expires_in: 900 })
      return responder(entry)
    },
  })
  return {
    client,
    sent,
    gateCalls: () => gateCalls,
    advance: (ms: number) => {
      clock += ms
    },
  }
}

const header = (sent: Sent, name: string) => new Headers(sent.init.headers).get(name)

describe('tastytrade client', () => {
  it('refreshes once, reuses the token, unwraps data, and gates every request', async () => {
    const { client, sent, gateCalls } = harness(() => Response.json({ data: { items: [] }, context: '/x' }))
    expect(await client.request('/market-metrics')).toEqual({ items: [] })
    await client.request('/market-metrics')
    expect(sent.map((entry) => new URL(entry.url).pathname)).toEqual(['/oauth/token', '/market-metrics', '/market-metrics'])
    expect(header(sent[1]!, 'Authorization')).toBe('Bearer token-1')
    expect(header(sent[2]!, 'Authorization')).toBe('Bearer token-1')
    expect(gateCalls()).toBe(3)
  })

  it('refreshes a token past its expiry', async () => {
    const { client, sent, advance } = harness(() => Response.json({ data: {} }))
    await client.request('/a')
    advance(900_000)
    await client.request('/b')
    expect(sent.filter((entry) => entry.url.endsWith('/oauth/token'))).toHaveLength(2)
  })

  it('sends the endpoint family API version', async () => {
    const { client, sent } = harness(() => Response.json({ data: {} }))
    await client.request('/accounts/5WT1/orders/live')
    await client.request('/market-metrics')
    expect(header(sent[1]!, 'Accept-Version')).toBe('20260427')
    expect(header(sent[2]!, 'Accept-Version')).toBeNull()
  })

  it('retries a read once on 401 with a fresh token', async () => {
    let calls = 0
    const { client, sent } = harness(() => (++calls === 1 ? Response.json({}, { status: 401 }) : Response.json({ data: { ok: true } })))
    expect(await client.request('/a')).toEqual({ ok: true })
    expect(sent.filter((entry) => entry.url.endsWith('/oauth/token'))).toHaveLength(2)
  })

  it('never resends a mutation after a 401', async () => {
    const { client, sent } = harness(() => Response.json({}, { status: 401 }))
    await expect(client.request('/accounts/1/orders', { method: 'POST', body: {} })).rejects.toBeInstanceOf(TastytradeApiError)
    expect(sent.filter((entry) => entry.url.includes('/orders'))).toHaveLength(1)
  })

  it('carries broker error messages and classifies ambiguity', async () => {
    const { client } = harness(({ url }) =>
      url.includes('/bad')
        ? Response.json({ error: { code: 'validation_error', message: 'x', errors: [{ code: 'price_off_tick', message: 'Price is off tick' }] } }, { status: 422 })
        : Response.json({}, { status: 503 }),
    )
    const refused = await client.request('/accounts/5WT1/bad').catch((error: unknown) => error)
    expect(refused).toBeInstanceOf(TastytradeApiError)
    expect((refused as TastytradeApiError).ambiguous).toBe(false)
    expect((refused as Error).message).toBe('Tastytrade 422 for /accounts/{account}/bad: price_off_tick: Price is off tick')
    const unavailable = (await client.request('/down').catch((error: unknown) => error)) as TastytradeApiError
    expect(unavailable.ambiguous).toBe(true)
  })

  it('shares one token refresh among concurrent requests', async () => {
    const { client, sent } = harness(() => Response.json({ data: {} }))
    await Promise.all([client.request('/a'), client.request('/b'), client.request('/c')])
    expect(sent.filter((entry) => entry.url.endsWith('/oauth/token'))).toHaveLength(1)
  })

  it('reports an unanswered mutation as an unknown outcome, but not a read or a dry run', async () => {
    const { client } = harness(() => Response.json({}, { status: 504 }))
    await expect(client.request('/accounts/1/orders/9', { method: 'DELETE' })).rejects.toBeInstanceOf(TastytradeOutcomeUnknownError)
    await expect(client.request('/accounts/1/orders/dry-run', { method: 'POST', body: {} })).rejects.toBeInstanceOf(
      TastytradeApiError,
    )
    await expect(client.request('/accounts/1/orders/live')).rejects.toBeInstanceOf(TastytradeApiError)
  })

  it('reports a request that got no response as a transport error', async () => {
    const { client } = harness(() => {
      throw new TypeError('fetch failed')
    })
    await expect(client.request('/a')).rejects.toBeInstanceOf(TastytradeTransportError)
  })

  it('explains a refused refresh token', async () => {
    const client = createTastytradeClient({
      clientSecret: 's',
      refreshToken: 'r',
      fetch: async () => Response.json({ error: { code: 'invalid_grant', message: 'Grant revoked' } }, { status: 401 }),
    })
    const error = await client.request('/a').catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(TastytradeAuthError)
    expect(error).toMatchObject({ reason: 'refused', status: 401 })
    expect((error as Error).message).toContain('invalid_grant: Grant revoked')
  })

  it('names a malformed token response by what is missing', async () => {
    const client = createTastytradeClient({ clientSecret: 's', refreshToken: 'r', fetch: async () => Response.json({ access_token: 't' }) })
    await expect(client.request('/a')).rejects.toMatchObject({ reason: 'invalid-lifetime' })
  })

  it('returns the whole body when asked, keeping pagination beside data', async () => {
    const page = { data: { items: [] }, pagination: { 'total-items': 3 } }
    const { client } = harness(() => Response.json(page))
    expect(await client.request('/accounts/1/orders', { raw: true })).toEqual(page)
  })

  it('encodes array parameters as repeated keys', () => {
    expect(buildQuery({ equity: ['AAPL', 'MSFT'], 'equity-option': ['AAPL  261218C00150000'], skip: undefined, n: 2 })).toBe(
      '?equity=AAPL&equity=MSFT&equity-option=AAPL++261218C00150000&n=2',
    )
  })
})

describe('client credentials', () => {
  it('uses a caller-minted access token without refreshing or retrying it', async () => {
    const sent: string[] = []
    const client = createTastytradeClient({
      accessToken: 'member-token',
      fetch: async (input, init) => {
        sent.push(`${new URL(String(input)).pathname} ${new Headers(init?.headers).get('Authorization')}`)
        return Response.json({}, { status: 401 })
      },
    })
    await expect(client.request('/accounts/1/positions')).rejects.toBeInstanceOf(TastytradeApiError)
    expect(sent).toEqual(['/accounts/1/positions Bearer member-token'])
  })

  it('reads lazy secrets only when a token has to be minted', async () => {
    let reads = 0
    const secret = (value: string) => async () => {
      reads += 1
      return value
    }
    const client = createTastytradeClient({
      clientSecret: secret('s'),
      refreshToken: secret('r'),
      fetch: async (input, init) =>
        String(input).endsWith('/oauth/token')
          ? Response.json({ access_token: JSON.parse(String(init?.body)).refresh_token, expires_in: 900 })
          : Response.json({ data: {} }),
    })
    await client.request('/a')
    await client.request('/b')
    expect(reads).toBe(2)
  })

  it('aborts a request on the caller signal', async () => {
    const client = createTastytradeClient({
      accessToken: 't',
      // Like real fetch: an already-aborted signal rejects at once, a later abort rejects then.
      fetch: (_input, init) =>
        new Promise((_resolve, reject) => {
          if (init?.signal?.aborted) reject(init.signal.reason)
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason))
        }),
    })
    const controller = new AbortController()
    const pending = client.request('/accounts/1/orders', { method: 'POST', body: {}, signal: controller.signal })
    controller.abort(new Error('deadline'))
    // An aborted mutation may still have reached the broker.
    await expect(pending).rejects.toBeInstanceOf(TastytradeOutcomeUnknownError)
  })
})

describe('API versions', () => {
  it('uses each endpoint family version and leaves unversioned APIs alone', () => {
    expect(tastytradeApiVersion('/accounts/A1/balances')).toBe('20240501')
    expect(tastytradeApiVersion('/accounts/A1/positions?include-marks=true')).toBe('20240501')
    expect(tastytradeApiVersion('/option-chains/NVDA')).toBe('20250715')
    expect(tastytradeApiVersion('/accounts/A1/orders/live?per-page=200')).toBe('20260427')
    expect(tastytradeApiVersion('/accounts/A1/complex-orders/dry-run')).toBe('20260427')
    expect(tastytradeApiVersion('/market-metrics?symbols=NVDA')).toBeUndefined()
    expect(tastytradeApiVersion('/watchlists')).toBeUndefined()
  })
})

describe('interval gate', () => {
  it('spaces permits half a second apart in arrival order', async () => {
    vi.useFakeTimers()
    try {
      const gate = new IntervalGate()
      const granted: number[] = []
      const start = Date.now()
      const waits = [1, 2, 3].map(() => gate.acquire().then(() => granted.push(Date.now() - start)))
      await vi.advanceTimersByTimeAsync(1000)
      await Promise.all(waits)
      expect(granted).toEqual([0, 500, 1000])
    } finally {
      vi.useRealTimers()
    }
  })
})
