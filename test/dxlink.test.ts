import { describe, expect, it } from 'vitest'

import { collectFeedEvents, compactRows, FEED_FIELDS } from '../src/tastytrade/dxlink.js'

/** A scripted DXLink peer: answers each client frame the way the real server does. */
class FakeDxLink extends EventTarget {
  static instances: FakeDxLink[] = []
  readonly sent: Record<string, unknown>[] = []
  closed = false

  constructor(
    readonly url: string,
    private readonly script: (frame: Record<string, unknown>, reply: (frame: unknown) => void) => void,
  ) {
    super()
    FakeDxLink.instances.push(this)
    queueMicrotask(() => this.dispatchEvent(new Event('open')))
  }

  send(data: string) {
    const frame = JSON.parse(data) as Record<string, unknown>
    this.sent.push(frame)
    this.script(frame, (reply) =>
      queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(reply) }))),
    )
  }

  close() {
    this.closed = true
  }
}

function socketClass(script: ConstructorParameters<typeof FakeDxLink>[1]) {
  return class extends FakeDxLink {
    constructor(url: string) {
      super(url, script)
    }
  } as unknown as new (url: string) => WebSocket
}

const greeksPeer = (rows: unknown[]) =>
  socketClass((frame, reply) => {
    if (frame.type === 'AUTH') {
      reply({ type: 'AUTH_STATE', channel: 0, state: 'UNAUTHORIZED' })
      reply({ type: 'AUTH_STATE', channel: 0, state: 'AUTHORIZED' })
    }
    if (frame.type === 'CHANNEL_REQUEST') reply({ type: 'CHANNEL_OPENED', channel: frame.channel, service: 'FEED', parameters: {} })
    if (frame.type === 'FEED_SETUP') {
      reply({
        type: 'FEED_CONFIG',
        channel: frame.channel,
        dataFormat: 'COMPACT',
        eventFields: { Greeks: FEED_FIELDS.Greeks },
      })
    }
    if (frame.type === 'FEED_SUBSCRIPTION') reply({ type: 'FEED_DATA', channel: frame.channel, data: ['Greeks', rows] })
  })

describe('dxlink', () => {
  it('authorizes, subscribes, and returns the latest event per symbol', async () => {
    const { events, timedOut } = await collectFeedEvents({
      quoteToken: { token: 't', url: 'wss://dxlink.example' },
      subscriptions: { Greeks: ['.A', '.B'] },
      timeoutMs: 1000,
      isComplete: (collected) => collected.Greeks.size === 2,
      webSocket: greeksPeer(['.A', 1.5, 0.3, 0.5, 0.1, -0.02, 0.01, 0.2, '.B', 2.5, 0.4, -0.4, 0.1, -0.03, -0.01, 0.3]),
    })
    expect(timedOut).toBe(false)
    expect(events.Greeks.get('.A')).toMatchObject({ delta: 0.5, volatility: 0.3 })
    expect(events.Greeks.get('.B')).toMatchObject({ delta: -0.4 })
    const socket = FakeDxLink.instances.at(-1)!
    expect(socket.closed).toBe(true)
    expect(socket.sent.find((frame) => frame.type === 'FEED_SUBSCRIPTION')).toMatchObject({
      add: [
        { type: 'Greeks', symbol: '.A' },
        { type: 'Greeks', symbol: '.B' },
      ],
    })
  })

  it('times out with what arrived, skipping empty-snapshot rows', async () => {
    const { events, timedOut } = await collectFeedEvents({
      quoteToken: { token: 't', url: 'wss://dxlink.example' },
      subscriptions: { Greeks: ['.A', '.B'] },
      timeoutMs: 50,
      isComplete: (collected) => collected.Greeks.size === 2,
      webSocket: greeksPeer(['.A', 1.5, 0.3, 0.5, 0.1, -0.02, 0.01, 0.2, '.B', 'NaN', 'NaN', 'NaN', 'NaN', 'NaN', 'NaN', 'NaN']),
    })
    expect(timedOut).toBe(true)
    expect([...events.Greeks.keys()]).toEqual(['.A'])
  })

  it('fails when the token is refused', async () => {
    const refused = socketClass((frame, reply) => {
      if (frame.type === 'AUTH') {
        reply({ type: 'AUTH_STATE', channel: 0, state: 'AUTHORIZED' })
        reply({ type: 'AUTH_STATE', channel: 0, state: 'UNAUTHORIZED' })
      }
    })
    await expect(
      collectFeedEvents({
        quoteToken: { token: 't', url: 'wss://dxlink.example' },
        subscriptions: { Greeks: ['.A'] },
        timeoutMs: 1000,
        webSocket: refused,
      }),
    ).rejects.toThrow('DXLink authorization failed')
  })

  it('rejects malformed COMPACT batches', () => {
    expect(() => compactRows('Greeks', ['Greeks', ['.A', 1]])).toThrow('malformed row batch')
    expect(() => compactRows('Greeks', ['Quote', []])).toThrow('does not match its channel')
  })
})
