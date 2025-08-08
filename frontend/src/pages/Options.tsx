import { useEffect, useMemo, useRef, useState } from 'react'
import { STORAGE_KEYS, type ThemeMode, applyAccent, applyTheme } from '../lib/theme'

export default function OptionsPage() {
  const [theme, setTheme] = useState<ThemeMode>('system')
  const [accentHex, setAccentHex] = useState<string>('#7c3aed')
  const inputRef = useRef<HTMLInputElement | null>(null)

  const palette = useMemo(() => (
    [
      { name: 'Blue', hex: '#93c5fd' },
      { name: 'Green', hex: '#86efac' },
      { name: 'Turquoise', hex: '#99f6e4' },
      { name: 'Orange', hex: '#fdba74' },
      { name: 'Purple', hex: '#c4b5fd' },
      { name: 'Red', hex: '#fca5a5' },
    ]
  ), [])

  useEffect(() => {
    const t = (localStorage.getItem(STORAGE_KEYS.theme) as ThemeMode | null) ?? 'system'
    const a = localStorage.getItem(STORAGE_KEYS.accent) ?? '#7c3aed'
    setTheme(t)
    setAccentHex(a)
  }, [])

  const saveTheme = (next: ThemeMode) => {
    setTheme(next)
    localStorage.setItem(STORAGE_KEYS.theme, next)
    applyTheme(next)
  }

  const saveAccent = (nextHex: string) => {
    setAccentHex(nextHex)
    localStorage.setItem(STORAGE_KEYS.accent, nextHex)
    applyAccent(nextHex)
  }

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-semibold">Options</h1>

      <section className="space-y-3">
        <h2 className="text-lg font-medium">Theme</h2>
        <div className="flex gap-3 items-center">
          <label className="flex items-center gap-2 text-sm">
            <input type="radio" name="theme" value="light" checked={theme === 'light'} onChange={() => saveTheme('light')} />
            Light
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="radio" name="theme" value="dark" checked={theme === 'dark'} onChange={() => saveTheme('dark')} />
            Dark
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="radio" name="theme" value="system" checked={theme === 'system'} onChange={() => saveTheme('system')} />
            System
          </label>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium">Accent color</h2>
        <div className="flex items-center gap-3">
          <div className="flex gap-2">
            {palette.map((c) => (
              <button
                key={c.hex}
                aria-label={c.name}
                title={c.name}
                className="h-8 w-8 rounded-full ring-2 ring-transparent data-[active=true]:ring-foreground cursor-pointer"
                style={{ background: c.hex }}
                data-active={accentHex.toLowerCase() === c.hex.toLowerCase()}
                onClick={() => saveAccent(c.hex)}
              />
            ))}
          </div>
          <button
            className="h-8 rounded-md px-3 border hover:bg-muted"
            onClick={() => inputRef.current?.click()}
          >
            Custom…
          </button>
          <input
            ref={inputRef}
            aria-label="Custom accent color"
            type="color"
            value={accentHex}
            onChange={(e) => saveAccent(e.target.value)}
            className="hidden"
          />
          <div className="text-sm text-muted-foreground">Used for buttons and gradients</div>
        </div>
      </section>
    </div>
  )
}


