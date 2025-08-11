export type ThemeMode = 'light' | 'dark' | 'system'

export type AccentKey = 'blue' | 'violet' | 'green' | 'orange' | 'pink' | 'teal'

export const ACCENT_HSL: Record<AccentKey, string> = {
  blue: '217 91% 60%',
  violet: '262 83% 58%',
  green: '142 71% 45%',
  orange: '27 96% 61%',
  pink: '330 81% 60%',
  teal: '174 60% 45%',
}

export const STORAGE_KEYS = {
  theme: 'hlb_theme',
  accent: 'hlb_accent', // legacy single-accent key
  accentLight: 'hlb_accent_light',
  accentDark: 'hlb_accent_dark',
} as const

export function resolveSystemPrefersDark(): boolean {
  return typeof window !== 'undefined' &&
    !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches)
}

export function applyTheme(mode: ThemeMode): void {
  const resolved: 'light' | 'dark' = mode === 'system' ? (resolveSystemPrefersDark() ? 'dark' : 'light') : mode
  document.documentElement.setAttribute('data-theme', resolved)
  // Ensure the accent tracks the resolved theme
  try {
    const accent = getStoredAccent(resolved)
    applyAccent(accent)
  } catch {
    // ignore storage issues (e.g., SSR)
  }
}

function hexToHsl(hex: string): string {
  // Remove #
  const clean = hex.replace('#', '')
  const bigint = parseInt(clean.length === 3
    ? clean.split('').map((c) => c + c).join('')
    : clean, 16)
  const r = ((bigint >> 16) & 255) / 255
  const g = ((bigint >> 8) & 255) / 255
  const b = (bigint & 255) / 255
  const max = Math.max(r, g, b), min = Math.min(r, g, b)
  let h = 0, s = 0
  const l = (max + min) / 2
  const d = max - min
  if (d !== 0) {
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    switch (max) {
      case r: h = (g - b) / d + (g < b ? 6 : 0); break
      case g: h = (b - r) / d + 2; break
      case b: h = (r - g) / d + 4; break
    }
    h /= 6
  }
  const hh = Math.round(h * 360)
  const ss = Math.round(s * 100)
  const ll = Math.round(l * 100)
  return `${hh} ${ss}% ${ll}%`
}

export function applyAccent(color: string): void {
  // Accept hex (#RRGGBB) or already-in-HSL "h s% l%"
  const hsl = color.startsWith('#') ? hexToHsl(color) : color
  document.documentElement.style.setProperty('--accent', hsl)
}

/** Get the currently resolved theme (after applying system preference if needed). */
export function getResolvedTheme(currentMode?: ThemeMode): 'light' | 'dark' {
  if (currentMode && currentMode !== 'system') return currentMode
  const attr = document.documentElement.getAttribute('data-theme')
  if (attr === 'light' || attr === 'dark') return attr
  return resolveSystemPrefersDark() ? 'dark' : 'light'
}

/** Read stored accent for a specific theme; falls back to legacy single-accent or default. */
export function getStoredAccent(theme: 'light' | 'dark'): string {
  try {
    const key = theme === 'light' ? STORAGE_KEYS.accentLight : STORAGE_KEYS.accentDark
    const themed = localStorage.getItem(key)
    if (themed) return themed
    const legacy = localStorage.getItem(STORAGE_KEYS.accent)
    return legacy ?? '#7c3aed'
  } catch {
    return '#7c3aed'
  }
}

/** Persist an accent for a specific theme and apply it immediately if that theme is active. */
export function setStoredAccent(theme: 'light' | 'dark', color: string, currentMode?: ThemeMode): void {
  try {
    const key = theme === 'light' ? STORAGE_KEYS.accentLight : STORAGE_KEYS.accentDark
    localStorage.setItem(key, color)
  } catch {
    // ignore
  }
  if (getResolvedTheme(currentMode) === theme) {
    applyAccent(color)
  }
}

export function initThemeFromStorage(): void {
  try {
    const storedTheme = (localStorage.getItem(STORAGE_KEYS.theme) as ThemeMode | null) ?? 'system'
    // Migrate legacy single-accent to per-theme if missing
    const legacy = localStorage.getItem(STORAGE_KEYS.accent)
    if (legacy) {
      if (!localStorage.getItem(STORAGE_KEYS.accentLight)) localStorage.setItem(STORAGE_KEYS.accentLight, legacy)
      if (!localStorage.getItem(STORAGE_KEYS.accentDark)) localStorage.setItem(STORAGE_KEYS.accentDark, legacy)
    }
    // Apply theme and corresponding accent
    applyTheme(storedTheme)
  } catch {
    // no-op in SSR or restricted environments
  }
}


