import { DurableObject } from 'cloudflare:workers'

import { IntervalGate } from '../tastytrade/gate.js'

/**
 * The Worker's single rate gate. Isolates do not share memory, so the two-requests-per-second
 * budget lives in one Durable Object that every broker request waits on before it is sent.
 */
export class BrokerGate extends DurableObject {
  private readonly gate = new IntervalGate()

  async acquire(): Promise<void> {
    await this.gate.acquire()
  }
}
