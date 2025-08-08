import { useEffect, useState } from 'react'
import { ACCENT_HSL, type AccentKey, STORAGE_KEYS, type ThemeMode, applyAccent, applyTheme } from '../lib/theme'

const ACCENTS: AccentKey[] = ['blue', 'violet', 'green', 'orange', 'pink', 'teal']

export default function OptionsMenu() {
  const [theme, setTheme] = useState<ThemeMode>('system')
  const [accent, setAccent] = useState<AccentKey>('violet')

  // Load from storage on mount
  useEffect(() => {
    const storedTheme = (localStorage.getItem(STORAGE_KEYS.theme) as ThemeMode | null) ?? 'system'
    const storedAccent = (localStorage.getItem(STORAGE_KEYS.accent) as AccentKey | null) ?? 'violet'
    setTheme(storedTheme)
    setAccent(storedAccent)
    applyTheme(storedTheme)
    applyAccent(storedAccent)

    // Keep system mode in sync with OS changes
    const m = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => {
      if (storedTheme === 'system') applyTheme('system')
    }
    m.addEventListener?.('change', onChange)
    return () => m.removeEventListener?.('change', onChange)
  }, [])

  const onChangeTheme = (next: ThemeMode) => {
    setTheme(next)
    localStorage.setItem(STORAGE_KEYS.theme, next)
    applyTheme(next)
  }

  const onChangeAccent = (next: AccentKey) => {
    setAccent(next)
    localStorage.setItem(STORAGE_KEYS.accent, next)
    applyAccent(next)
  }

  return (
    <div className="ml-auto flex items-center gap-3">
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">Theme</span>
        <select
          aria-label="Theme"
          className="border rounded-md h-8 px-2 bg-background"
          value={theme}
          onChange={(e) => onChangeTheme(e.target.value as ThemeMode)}
        >
          <option value="light">Light</option>
          <option value="dark">Dark</option>
          <option value="system">System</option>
        </select>
      </div>

      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">Accent</span>
        <div className="flex gap-2">
          {ACCENTS.map((k) => (
            <button
              key={k}
              aria-label={`Accent ${k}`}
              onClick={() => onChangeAccent(k)}
              className={`h-6 w-6 rounded-full ring-2 ring-transparent hover:ring-foreground`}
              style={{ background: `hsl(${ACCENT_HSL[k]})` }}
              data-active={accent === k}
            />
          ))}
        </div>
      </div>
    </div>
  )
}


