const NEW_YORK = 'America/New_York'

/** The New York calendar date (YYYY-MM-DD) at `instant`; US market calendars are keyed by it. */
export function nyDate(instant: Date): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: NEW_YORK, year: 'numeric', month: '2-digit', day: '2-digit' }).format(
    instant,
  )
}

/** `instant` as an ISO 8601 timestamp in New York local time with its UTC offset. */
export function nyIsoTime(instant: Date): string {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: NEW_YORK,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      timeZoneName: 'longOffset',
    })
      .formatToParts(instant)
      .map((part) => [part.type, part.value]),
  )
  const offset = parts.timeZoneName === 'GMT' ? '+00:00' : String(parts.timeZoneName).replace('GMT', '')
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}${offset}`
}

/** A rough human duration ("3 hours", "a day"), the way `humanize.naturaldelta` words it. */
export function naturalDelta(milliseconds: number): string {
  const seconds = Math.floor(Math.abs(milliseconds) / 1000)
  if (seconds < 1) return 'a moment'
  if (seconds < 60) return seconds === 1 ? 'a second' : `${seconds} seconds`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return minutes === 1 ? 'a minute' : `${minutes} minutes`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return hours === 1 ? 'an hour' : `${hours} hours`
  const days = Math.floor(hours / 24)
  return days === 1 ? 'a day' : `${days} days`
}
