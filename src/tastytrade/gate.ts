import type { RequestGate } from './client.js'

/** Two broker requests per second: Tastytrade publishes no rate, so this stays conservatively under it. */
export const BROKER_PERMIT_INTERVAL_MS = 500

/**
 * Spaces requests `intervalMs` apart within one process. Each caller reserves the next free slot
 * before waiting, so concurrent callers queue in arrival order instead of bursting.
 */
export class IntervalGate implements RequestGate {
  private nextPermitAt = 0

  constructor(
    private readonly intervalMs = BROKER_PERMIT_INTERVAL_MS,
    private readonly now: () => number = Date.now,
  ) {}

  async acquire(): Promise<void> {
    const now = this.now()
    const permitAt = Math.max(now, this.nextPermitAt)
    this.nextPermitAt = permitAt + this.intervalMs
    if (permitAt > now) await new Promise((resolve) => setTimeout(resolve, permitAt - now))
  }
}
