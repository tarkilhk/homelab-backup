function parsePossiblyNaiveUtc(input: string | number | Date): Date {
  if (input instanceof Date) return input
  if (typeof input === 'number') return new Date(input)
  let s = String(input).trim()
  // Convert "YYYY-MM-DD HH:mm:ss(.fffff)" to ISO by inserting 'T'
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(s)) {
    s = s.replace(' ', 'T')
  }
  // Normalize fractional seconds to at most 3 digits (ms)
  s = s.replace(/(\.\d{3})\d+/, '$1')
  // Ensure a timezone designator — treat naive strings as UTC
  const hasTz = /[zZ]$/.test(s) || /[+-]\d{2}:?\d{2}$/.test(s)
  if (!hasTz) s += 'Z'
  return new Date(s)
}

export function formatLocalDateTime(value: string | number | Date): string {
  try {
    const date = parsePossiblyNaiveUtc(value)
    if (Number.isNaN(date.getTime())) return '—'
    const y = date.getFullYear()
    const m = String(date.getMonth() + 1).padStart(2, '0')
    const d = String(date.getDate()).padStart(2, '0')
    const hh = String(date.getHours()).padStart(2, '0')
    const mm = String(date.getMinutes()).padStart(2, '0')
    const ss = String(date.getSeconds()).padStart(2, '0')
    return `${y}-${m}-${d}, ${hh}:${mm}:${ss}`
  } catch {
    return '—'
  }
}

