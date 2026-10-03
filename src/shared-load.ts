/**
 * Runs `load` once per key while it is in flight; concurrent callers share its promise. The map
 * must be scoped to one request on Workers, where a pending promise cannot cross requests.
 */
export function sharedLoad<T>(pending: Map<string, Promise<unknown>>, key: string, load: () => Promise<T>): Promise<T> {
  let promise = pending.get(key) as Promise<T> | undefined
  if (!promise) {
    promise = load().finally(() => pending.delete(key))
    pending.set(key, promise)
  }
  return promise
}
