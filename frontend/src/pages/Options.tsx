import { useEffect, useMemo, useState } from 'react'
import { STORAGE_KEYS, type ThemeMode, applyAccent, applyTheme, getStoredAccent, setStoredAccent, getResolvedTheme } from '../lib/theme'
import { HexColorPicker } from 'react-colorful'
import AppCard from '../components/ui/AppCard'
import { cn } from '../lib/cn'

export default function OptionsPage() {
  const [theme, setTheme] = useState<ThemeMode>('system')
  const [accentLightHex, setAccentLightHex] = useState<string>('#7c3aed')
  const [accentDarkHex, setAccentDarkHex] = useState<string>('#7c3aed')
  const [accentEditingTheme, setAccentEditingTheme] = useState<'light' | 'dark'>(() => getResolvedTheme())
  const [showPicker, setShowPicker] = useState<boolean>(false)

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
    const light = getStoredAccent('light')
    const dark = getStoredAccent('dark')
    setTheme(t)
    setAccentLightHex(light)
    setAccentDarkHex(dark)
    // Apply the resolved theme's accent (applyTheme already does this, but ensure on mount)
    applyAccent(getResolvedTheme(t) === 'light' ? light : dark)
  }, [])

  const saveTheme = (next: ThemeMode) => {
    setTheme(next)
    localStorage.setItem(STORAGE_KEYS.theme, next)
    applyTheme(next)
  }

  const saveAccent = (nextHex: string, forTheme: 'light' | 'dark') => {
    if (forTheme === 'light') setAccentLightHex(nextHex)
    else setAccentDarkHex(nextHex)
    setStoredAccent(forTheme, nextHex, theme)
  }

  return (
    <div className="space-y-6">
      {/* Hero header to match Dashboard */}
      <div className="relative overflow-hidden rounded-2xl p-6 border surface-card">
        <h1 className="text-3xl font-extrabold tracking-tight">Options</h1>
        <p className="text-sm text-muted-foreground">Personalize your experience.</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Theme */}
        <AppCard title="Theme" description="Choose how the app looks">
          <div className="grid grid-cols-3 gap-3 max-w-md">
            {(['light','dark','system'] as ThemeMode[]).map((m) => (
              <label key={m} className="block">
                <input
                  type="radio"
                  name="theme"
                  className="sr-only"
                  checked={theme === m}
                  onChange={() => saveTheme(m)}
                />
                <div
                  className={cn(
                    'rounded-xl border px-4 py-3 text-sm font-medium text-center cursor-pointer select-none',
                    'hover:bg-[hsl(var(--accent)/.08)]',
                    theme === m && 'ring-2 ring-accent bg-[hsl(var(--accent)/.10)]'
                  )}
                  aria-pressed={theme === m}
                >
                  {m.charAt(0).toUpperCase() + m.slice(1)}
                </div>
              </label>
            ))}
          </div>
        </AppCard>

        {/* Accent */}
        <AppCard
          title="Accent color"
          description="Used for buttons and badges"
          headerRight={
            <div className="inline-flex items-center gap-2">
              <span className="rounded-full border border-[hsl(var(--accent)/.35)] bg-[hsl(var(--accent)/.12)] text-[hsl(var(--accent))] px-2 py-0.5 text-xs">Preview</span>
              <button className="btn-primary rounded-md px-3 py-1 text-xs shadow">Primary</button>
            </div>
          }
        >
          <div className="flex items-center gap-3 flex-wrap">
            <div className="inline-flex rounded-md border overflow-hidden">
              {(['light','dark'] as const).map((tKey) => (
                <button
                  key={tKey}
                  type="button"
                  onClick={() => setAccentEditingTheme(tKey)}
                  className={cn(
                    'px-3 py-1.5 text-xs font-medium',
                    accentEditingTheme === tKey ? 'bg-[hsl(var(--accent)/.12)] text-foreground' : 'bg-background text-muted-foreground'
                  )}
                  aria-pressed={accentEditingTheme === tKey}
                >
                  {tKey === 'light' ? 'Light accent' : 'Dark accent'}
                </button>
              ))}
            </div>
            <div className="flex gap-2">
              {palette.map((c) => (
                <button
                  key={c.hex}
                  aria-label={c.name}
                  title={c.name}
                  className="h-9 w-9 rounded-full ring-2 ring-transparent data-[active=true]:ring-foreground cursor-pointer shadow-sm"
                  style={{ background: c.hex }}
                  data-active={(accentEditingTheme === 'light' ? accentLightHex : accentDarkHex).toLowerCase() === c.hex.toLowerCase()}
                  onClick={() => saveAccent(c.hex, accentEditingTheme)}
                />
              ))}
            </div>
            <button
              className="h-9 rounded-md px-3 border hover:bg-muted"
              onClick={() => setShowPicker((v) => !v)}
            >
              {showPicker ? 'Close' : 'Custom…'}
            </button>
          </div>
          {showPicker && (
            <div className="mt-4 grid gap-2 sm:grid-cols-[auto_1fr] items-start">
              <HexColorPicker
                color={accentEditingTheme === 'light' ? accentLightHex : accentDarkHex}
                onChange={(hex) => saveAccent(hex, accentEditingTheme)}
              />
              <div className="sm:ml-4">
                <div className="text-xs text-muted-foreground">
                  Selected ({accentEditingTheme}): {accentEditingTheme === 'light' ? accentLightHex : accentDarkHex}
                </div>
                <div className="mt-2 inline-flex items-center gap-2">
                  <span className="rounded-full border border-[hsl(var(--accent)/.35)] bg-[hsl(var(--accent)/.12)] text-[hsl(var(--accent))] px-2 py-0.5 text-xs">Preview</span>
                  <button className="btn-primary rounded-md px-3 py-1 text-xs shadow">Primary</button>
                </div>
              </div>
            </div>
          )}
        </AppCard>
      </div>
    </div>
  )
}


