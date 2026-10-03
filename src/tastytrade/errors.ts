export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** A request that failed; `ambiguous` means the broker may still have acted on it. */
export abstract class TastytradeRequestError extends Error {
  abstract readonly ambiguous: boolean
}

/** A response Tastytrade answered with a non-2xx status; a timeout or 5xx is ambiguous. */
export class TastytradeApiError extends TastytradeRequestError {
  override readonly name = 'TastytradeApiError'

  constructor(
    readonly status: number,
    readonly endpoint: string,
    readonly brokerMessages: readonly string[] = [],
  ) {
    const detail = brokerMessages.length ? `: ${brokerMessages.join('; ')}` : ''
    super(`Tastytrade ${status} for ${endpoint}${detail}`)
  }

  get ambiguous(): boolean {
    return this.status >= 500 || this.status === 408
  }
}

/** The request never produced a response (network failure or timeout). */
export class TastytradeTransportError extends TastytradeRequestError {
  override readonly name = 'TastytradeTransportError'
  readonly ambiguous = true

  constructor(readonly endpoint: string, cause: unknown) {
    super(`Tastytrade request to ${endpoint} failed without a response: ${errorMessage(cause)}`, { cause })
  }
}

/**
 * A mutation whose result is unknown: it may have been applied. It must never be retried blindly;
 * the caller has to look at the broker's state first.
 */
export class TastytradeOutcomeUnknownError extends Error {
  override readonly name = 'TastytradeOutcomeUnknownError'
}

export type TastytradeAuthFailure = 'unreachable' | 'refused' | 'missing-token' | 'invalid-lifetime'

/** The OAuth refresh failed; `reason` (and `status` when refused) let callers word it their own way. */
export class TastytradeAuthError extends Error {
  override readonly name = 'TastytradeAuthError'

  constructor(
    readonly reason: TastytradeAuthFailure,
    message: string,
    readonly status?: number,
    options?: ErrorOptions,
  ) {
    super(message, options)
  }
}
