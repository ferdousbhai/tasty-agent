import {
  TastytradeApiError,
  TastytradeAuthError,
  TastytradeOutcomeUnknownError,
  TastytradeRequestError,
  TastytradeTransportError,
} from './errors.js'
import { brokerMessageText, jsonItems, jsonObject, jsonObjects, jsonText, type JsonObject, type JsonValue } from './json.js'
import { tastytradeApiVersion } from './versions.js'

export const TASTYTRADE_API_BASE = 'https://api.tastyworks.com'
export const TASTYTRADE_SANDBOX_API_BASE = 'https://api.cert.tastyworks.com'

const DEFAULT_TIMEOUT_MS = 20_000
// Catalog-sized reads (a full option chain) are several MB; anything past this is refused unbuffered.
const MAX_RESPONSE_BYTES = 16 * 1024 * 1024
const MAX_ERROR_BYTES = 64 * 1024
const MAX_BROKER_MESSAGES = 10

/**
 * Admits one broker request. Every call the client makes passes through `acquire` first, so a
 * single gate instance is the one place a rate limit is enforced.
 */
export interface RequestGate {
  acquire(): Promise<void>
}

export type AccessToken = { token: string; expiresAt: number }

/**
 * Holds the OAuth access token between requests. Only a settled token is ever stored: a pending
 * refresh is request-scoped I/O on Workers and must not be shared across requests (a client
 * instance shares its own in-flight refresh, so build one client per Worker request).
 */
export interface TokenStore {
  get(): AccessToken | undefined
  set(token: AccessToken | undefined): void
}

export function memoryTokenStore(): TokenStore {
  let current: AccessToken | undefined
  return {
    get: () => current,
    set: (token) => {
      current = token
    },
  }
}

export type QueryValue = string | number | boolean | readonly string[] | undefined

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  query?: Record<string, QueryValue>
  body?: JsonValue
  /** Aborts the request early (for example a submission deadline); the timeout still applies. */
  signal?: AbortSignal
  /** Return the parsed body as sent (with `pagination` beside `data`) instead of its `data` member. */
  raw?: boolean
}

/** A credential value, or a function that reads it only when a token actually has to be minted. */
export type Secret = string | (() => Promise<string>)

/**
 * How the client authenticates: an OAuth grant it refreshes itself, or an access token the caller
 * minted (for example a per-request token from the account holder), which is never refreshed and
 * never retried on 401.
 */
export type TastytradeCredentials =
  | { clientSecret: Secret; refreshToken: Secret; accessToken?: never }
  | { accessToken: string; clientSecret?: never; refreshToken?: never }

export type TastytradeClientOptions = TastytradeCredentials & {
  apiBase?: string
  gate?: RequestGate
  tokenStore?: TokenStore
  userAgent?: string
  timeoutMs?: number
  fetch?: typeof fetch
  now?: () => number
}

export interface TastytradeClient {
  /**
   * Calls one endpoint and returns the response's `data` member, or with `raw` the whole body ({} for an empty body).
   * A mutation (any non-GET other than a dry run) that fails ambiguously is raised as
   * `TastytradeOutcomeUnknownError`, because the broker may have applied it.
   */
  request(path: string, options?: RequestOptions): Promise<JsonValue>
}

/** A list endpoint's `items`. */
export async function requestItems(client: TastytradeClient, path: string, options?: RequestOptions): Promise<JsonObject[]> {
  return jsonItems(await client.request(path, options), `Tastytrade ${path}`)
}

/** A single-object endpoint's `data`. */
export async function requestObject(client: TastytradeClient, path: string, options?: RequestOptions): Promise<JsonObject> {
  const data = jsonObject(await client.request(path, options))
  if (!data) throw new Error(`Tastytrade ${path}: response is not an object`)
  return data
}

const OPEN_GATE: RequestGate = { acquire: async () => {} }

export function createTastytradeClient(options: TastytradeClientOptions): TastytradeClient {
  const apiBase = (options.apiBase ?? TASTYTRADE_API_BASE).replace(/\/+$/, '')
  const gate = options.gate ?? OPEN_GATE
  const tokens = options.tokenStore ?? memoryTokenStore()
  const userAgent = options.userAgent ?? 'tasty-agent'
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const doFetch = options.fetch ?? ((input, init) => fetch(input, init))
  const now = options.now ?? Date.now

  async function refreshAccessToken(): Promise<string> {
    const [clientSecret, refreshToken] = await Promise.all([reveal(options.clientSecret!), reveal(options.refreshToken!)])
    await gate.acquire()
    let response: Response
    try {
      response = await doFetch(`${apiBase}/oauth/token`, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'User-Agent': userAgent },
        body: JSON.stringify({ grant_type: 'refresh_token', client_secret: clientSecret, refresh_token: refreshToken }),
        signal: AbortSignal.timeout(timeoutMs),
      })
    } catch (error) {
      throw new TastytradeAuthError('unreachable', 'Tastytrade OAuth token refresh failed without a response', undefined, {
        cause: error,
      })
    }
    if (!response.ok) {
      const messages = await brokerMessages(response)
      const detail = messages.length ? `: ${messages.join('; ')}` : ''
      throw new TastytradeAuthError(
        'refused',
        `Tastytrade OAuth token refresh was refused (${response.status})${detail}. ` +
          'Check TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN.',
        response.status,
      )
    }
    const payload = jsonObject(await readBoundedJson(response, MAX_ERROR_BYTES, '/oauth/token'))
    const token = jsonText(payload?.access_token)
    const lifetimeSeconds = payload?.expires_in
    if (!token) throw new TastytradeAuthError('missing-token', 'Tastytrade OAuth token response has no access_token')
    if (typeof lifetimeSeconds !== 'number' || !(lifetimeSeconds > 0)) {
      throw new TastytradeAuthError('invalid-lifetime', 'Tastytrade OAuth token response has no positive expires_in')
    }
    const lifetimeMs = lifetimeSeconds * 1000
    // Retire the token early enough that it outlives any request it is handed to.
    const skewMs = Math.min(timeoutMs, lifetimeMs * 0.1)
    tokens.set({ token, expiresAt: now() + lifetimeMs - skewMs })
    return token
  }

  // Concurrent requests on one client share a single refresh.
  let pendingRefresh: Promise<string> | undefined
  async function accessToken(): Promise<string> {
    if (options.accessToken !== undefined) return options.accessToken
    const cached = tokens.get()
    if (cached && now() < cached.expiresAt) return cached.token
    pendingRefresh ??= refreshAccessToken().finally(() => {
      pendingRefresh = undefined
    })
    return pendingRefresh
  }

  async function send(path: string, request: RequestOptions, token: string): Promise<Response> {
    await gate.acquire()
    const headers: Record<string, string> = {
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
      'User-Agent': userAgent,
    }
    const version = tastytradeApiVersion(path)
    if (version) headers['Accept-Version'] = version
    if (request.body !== undefined) headers['Content-Type'] = 'application/json'
    try {
      return await doFetch(`${apiBase}${path}${buildQuery(request.query)}`, {
        method: request.method ?? 'GET',
        headers,
        body: request.body === undefined ? undefined : JSON.stringify(request.body),
        signal: request.signal ? AbortSignal.any([request.signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
      })
    } catch (error) {
      throw new TastytradeTransportError(redactEndpoint(path), error)
    }
  }

  async function call(path: string, request: RequestOptions): Promise<JsonValue> {
    const method = request.method ?? 'GET'
    const endpoint = redactEndpoint(path)
    let token = await accessToken()
    let response = await send(path, request, token)
    // A revoked or early-expired token is retried once, but only for reads (a mutation is never
    // sent twice) and only for a grant the client can refresh itself.
    if (response.status === 401 && method === 'GET' && options.accessToken === undefined) {
      await response.body?.cancel()
      if (tokens.get()?.token === token) tokens.set(undefined)
      token = await accessToken()
      response = await send(path, request, token)
    }
    if (!response.ok) {
      throw new TastytradeApiError(response.status, endpoint, await brokerMessages(response))
    }
    if (response.status === 204) {
      await response.body?.cancel()
      return {}
    }
    const payload = await readBoundedJson(response, MAX_RESPONSE_BYTES, endpoint)
    if (payload === undefined) return {}
    if (request.raw) return payload
    const body = jsonObject(payload)
    return body && 'data' in body ? (body.data ?? {}) : payload
  }

  return {
    async request(path, request = {}) {
      if (!path.startsWith('/')) throw new Error(`Tastytrade path must start with '/': ${path}`)
      const mutation = (request.method ?? 'GET') !== 'GET' && !path.endsWith('/dry-run')
      try {
        return await call(path, request)
      } catch (error) {
        if (mutation && error instanceof TastytradeRequestError && error.ambiguous) {
          throw new TastytradeOutcomeUnknownError(
            `Tastytrade may have applied this change, but no definite answer arrived (${error.message}). ` +
              'Check the current state (e.g. list_orders) before trying again.',
            { cause: error },
          )
        }
        throw error
      }
    },
  }
}

function reveal(secret: Secret): Promise<string> {
  return typeof secret === 'string' ? Promise.resolve(secret) : secret()
}

export function buildQuery(query: Record<string, QueryValue> | undefined): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined) continue
    if (Array.isArray(value)) {
      for (const item of value) params.append(key, item)
    } else {
      params.append(key, String(value))
    }
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

function redactEndpoint(path: string): string {
  return path.split('?', 1)[0]!.replace(/\/accounts\/[^/]+/g, '/accounts/{account}')
}

async function readBoundedText(response: Response, maxBytes: number, label: string): Promise<string> {
  const declared = Number(response.headers.get('Content-Length'))
  if (Number.isFinite(declared) && declared > maxBytes) {
    await response.body?.cancel()
    throw new Error(`${label}: response exceeds ${maxBytes} bytes`)
  }
  if (!response.body) return ''
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let text = ''
  let total = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      total += value.byteLength
      if (total > maxBytes) {
        await reader.cancel()
        throw new Error(`${label}: response exceeds ${maxBytes} bytes`)
      }
      text += decoder.decode(value, { stream: true })
    }
  } finally {
    reader.releaseLock()
  }
  return text + decoder.decode()
}

async function readBoundedJson(response: Response, maxBytes: number, label: string): Promise<JsonValue | undefined> {
  const text = await readBoundedText(response, maxBytes, label)
  if (!text.trim()) return undefined
  try {
    return JSON.parse(text) as JsonValue
  } catch (cause) {
    throw new Error(`${label}: response is not valid JSON`, { cause })
  }
}

/** The broker's own explanation from an error body: `{error: {code, message, errors: [...]}}`. */
async function brokerMessages(response: Response): Promise<string[]> {
  let payload: JsonValue | undefined
  try {
    payload = await readBoundedJson(response, MAX_ERROR_BYTES, 'error')
  } catch {
    return []
  }
  const error = jsonObject(jsonObject(payload)?.error)
  if (!error) return []
  const nested = jsonObjects(error.errors)
  const messages = (nested.length ? nested : [error]).map(brokerMessageText).filter((text) => text !== undefined)
  return messages.slice(0, MAX_BROKER_MESSAGES)
}
