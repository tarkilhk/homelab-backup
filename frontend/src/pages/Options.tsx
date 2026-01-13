import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { STORAGE_KEYS, type ThemeMode, applyAccent, applyTheme, getStoredAccent, setStoredAccent, getResolvedTheme } from '../lib/theme'
import { HexColorPicker } from 'react-colorful'
import AppCard from '../components/ui/AppCard'
import { cn } from '../lib/cn'
import { api, type RetentionPolicy, type RetentionRule } from '../api/client'
import { Button } from '../components/ui/button'
import { toast } from 'sonner'

export default function OptionsPage() {
  const qc = useQueryClient()
  const [theme, setTheme] = useState<ThemeMode>('system')
  const [accentLightHex, setAccentLightHex] = useState<string>('#7c3aed')
  const [accentDarkHex, setAccentDarkHex] = useState<string>('#7c3aed')
  const [accentEditingTheme, setAccentEditingTheme] = useState<'light' | 'dark'>(() => getResolvedTheme())
  const [tempAccentHex, setTempAccentHex] = useState<string | null>(null)
  const [showPicker, setShowPicker] = useState<boolean>(false)

  // Retention policy state
  const [retentionEnabled, setRetentionEnabled] = useState<boolean>(false)
  const [dailyWindow, setDailyWindow] = useState<number>(7)
  const [weeklyWindow, setWeeklyWindow] = useState<number>(4)
  const [monthlyWindow, setMonthlyWindow] = useState<number>(6)

  // Fetch current settings
  const { data: settings, isLoading: settingsLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  })

  // Parse retention policy from settings
  useEffect(() => {
    if (settings?.global_retention_policy_json) {
      try {
        const policy: RetentionPolicy = JSON.parse(settings.global_retention_policy_json)
        if (policy.rules && policy.rules.length > 0) {
          setRetentionEnabled(true)
          for (const rule of policy.rules) {
            if (rule.unit === 'day') setDailyWindow(rule.window)
            else if (rule.unit === 'week') setWeeklyWindow(rule.window)
            else if (rule.unit === 'month') setMonthlyWindow(rule.window)
          }
        } else {
          setRetentionEnabled(false)
        }
      } catch {
        setRetentionEnabled(false)
      }
    } else {
      setRetentionEnabled(false)
    }
  }, [settings])

  // Build retention policy JSON from current state
  const buildRetentionPolicyJson = (): string | null => {
    if (!retentionEnabled) return null
    const rules: RetentionRule[] = []
    if (dailyWindow > 0) rules.push({ unit: 'day', window: dailyWindow, keep: 1 })
    if (weeklyWindow > 0) rules.push({ unit: 'week', window: weeklyWindow, keep: 1 })
    if (monthlyWindow > 0) rules.push({ unit: 'month', window: monthlyWindow, keep: 1 })
    if (rules.length === 0) return null
    return JSON.stringify({ rules })
  }

  // Save settings mutation
  const saveSettingsMut = useMutation({
    mutationFn: () => api.updateSettings({ global_retention_policy_json: buildRetentionPolicyJson() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['settings'] })
      toast.success('Retention settings saved')
    },
    onError: (err) => {
      toast.error(`Failed to save settings: ${(err as Error).message}`)
    },
  })

  // Two curated palettes tuned for each theme (pastel for light, vivid for dark)
  const lightPalette = useMemo(() => ([
    { name: 'Blue', hex: '#93c5fd' },
    { name: 'Green', hex: '#86efac' },
    { name: 'Teal', hex: '#99f6e4' },
    { name: 'Orange', hex: '#fdba74' },
    { name: 'Purple', hex: '#c4b5fd' },
    { name: 'Red', hex: '#fca5a5' },
  ]), [])
  const darkPalette = useMemo(() => ([
    { name: 'Blue', hex: '#3b82f6' },
    { name: 'Green', hex: '#22c55e' },
    { name: 'Teal', hex: '#14b8a6' },
    { name: 'Orange', hex: '#f59e0b' },
    { name: 'Purple', hex: '#8b5cf6' },
    { name: 'Red', hex: '#ef4444' },
  ]), [])
  const palette = accentEditingTheme === 'light' ? lightPalette : darkPalette

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

  // Keep the editing theme in sync with the chosen theme (and OS for system)
  useEffect(() => {
    const resolved = getResolvedTheme(theme)
    setAccentEditingTheme(resolved)
    // Update when system preference changes
    if (theme === 'system') {
      const m = window.matchMedia('(prefers-color-scheme: dark)')
      const onChange = () => setAccentEditingTheme(getResolvedTheme('system'))
      m.addEventListener?.('change', onChange)
      return () => m.removeEventListener?.('change', onChange)
    }
  }, [theme])

  const saveTheme = (next: ThemeMode) => {
    setTheme(next)
    localStorage.setItem(STORAGE_KEYS.theme, next)
    applyTheme(next)
    setAccentEditingTheme(getResolvedTheme(next))
  }

  const saveAccent = (nextHex: string, forTheme: 'light' | 'dark') => {
    if (forTheme === 'light') setAccentLightHex(nextHex)
    else setAccentDarkHex(nextHex)
    setStoredAccent(forTheme, nextHex, theme)
  }

  const customHex = accentEditingTheme === 'light' ? accentLightHex : accentDarkHex
  const isLockedByTheme = theme !== 'system'
  const isCustomSelected = useMemo(() => {
    const paletteHexes = palette.map((p) => p.hex.toLowerCase())
    return !paletteHexes.includes(customHex.toLowerCase())
  }, [palette, customHex])

  return (
    <div className="space-y-6">
      {/* Hero header to match Dashboard */}
      <div className="relative overflow-hidden rounded-2xl p-6 border surface-card">
        <h1 className="text-2xl font-semibold">Options</h1>
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
            {isLockedByTheme ? (
              <div className="text-xs text-muted-foreground">
                Editing {accentEditingTheme} accent (based on selected theme)
              </div>
            ) : (
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
            )}
            <div className="flex gap-2">
              {palette.map((c) => (
                <button
                  key={c.hex}
                  aria-label={c.name}
                  title={c.name}
                  className="h-9 w-9 rounded-full ring-2 ring-transparent cursor-pointer shadow-sm data-[active=true]:ring-foreground data-[active=true]:ring-offset-2 data-[active=true]:ring-offset-background"
                  style={{ background: c.hex }}
                  data-active={!showPicker && customHex.toLowerCase() === c.hex.toLowerCase()}
                  onClick={() => {
                    // Selecting a preset should deselect custom and close picker without saving custom temp
                    if (showPicker) {
                      setShowPicker(false)
                      setTempAccentHex(null)
                    }
                    saveAccent(c.hex, accentEditingTheme)
                  }}
                />
              ))}
              {/* Custom pill (moved to the end/right) */}
              <button
                type="button"
                aria-label="Custom color"
                title="Custom"
                className={cn(
                  'h-9 inline-flex items-center gap-2 rounded-full border px-3 text-sm font-medium cursor-pointer ring-2 ring-transparent',
                  'hover:bg-[hsl(var(--accent)/.08)]',
                  (showPicker || isCustomSelected) && 'ring-foreground ring-offset-2 ring-offset-background bg-[hsl(var(--accent)/.10)]'
                )}
                onClick={() => {
                  setShowPicker((prev) => {
                    const next = !prev
                    if (next) setTempAccentHex(customHex)
                    else setTempAccentHex(null)
                    return next
                  })
                }}
              >
                <span
                  className="h-4 w-4 rounded-full shadow-sm"
                  style={{ background: customHex }}
                />
                <span>Custom</span>
              </button>
            </div>
          </div>
          {showPicker && (
            <div className="mt-4 grid gap-2 sm:grid-cols-[auto_1fr] items-start">
              <HexColorPicker
                color={tempAccentHex ?? customHex}
                onChange={(hex) => {
                  setTempAccentHex(hex)
                  // Preview only if editing theme is currently active
                  if (getResolvedTheme(theme) === accentEditingTheme) {
                    applyAccent(hex)
                  }
                }}
              />
              <div className="sm:ml-4">
                <div className="text-xs text-muted-foreground">Selected ({accentEditingTheme}): {tempAccentHex ?? customHex}</div>
                <div className="mt-2 inline-flex items-center gap-2">
                  <span className="rounded-full border border-[hsl(var(--accent)/.35)] bg-[hsl(var(--accent)/.12)] text-[hsl(var(--accent))] px-2 py-0.5 text-xs">Preview</span>
                  <button className="btn-primary rounded-md px-3 py-1 text-xs shadow">Primary</button>
                </div>
                <div className="mt-3 flex gap-2">
                  <button
                    className="rounded-md px-3 py-1 text-xs border border-red-500/30 bg-red-500/10 text-red-600 hover:bg-red-500/15"
                    onClick={() => {
                      // Revert preview to stored accent of active theme
                      const resolved = getResolvedTheme(theme)
                      const stored = getStoredAccent(resolved)
                      applyAccent(stored)
                      setTempAccentHex(null)
                      setShowPicker(false)
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    className="btn-primary rounded-md px-3 py-1 text-xs shadow"
                    onClick={() => {
                      if (tempAccentHex) {
                        saveAccent(tempAccentHex, accentEditingTheme)
                      }
                      setTempAccentHex(null)
                      setShowPicker(false)
                    }}
                  >
                    Save
                  </button>
                </div>
              </div>
            </div>
          )}
        </AppCard>
      </div>

      {/* Retention Policy */}
      <AppCard title="Backup Retention" description="Automatically clean up old backups to save disk space">
        {settingsLoading ? (
          <div className="text-sm text-muted-foreground">Loading...</div>
        ) : (
          <div className="space-y-4">
            {/* Enable toggle */}
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-gray-300"
                checked={retentionEnabled}
                onChange={(e) => setRetentionEnabled(e.target.checked)}
              />
              <span className="text-sm font-medium">Enable retention cleanup</span>
            </label>

            {retentionEnabled && (
              <div className="grid gap-4 sm:grid-cols-3">
                {/* Daily */}
                <div className="space-y-1">
                  <label className="text-sm font-medium">Daily backups</label>
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground">Keep 1 per day for last</span>
                    <input
                      type="number"
                      min={0}
                      max={365}
                      className="w-16 border rounded px-2 py-1 text-sm"
                      value={dailyWindow}
                      onChange={(e) => setDailyWindow(Math.max(0, parseInt(e.target.value) || 0))}
                    />
                    <span className="text-sm text-muted-foreground">days</span>
                  </div>
                  <p className="text-xs text-muted-foreground">Set to 0 to skip daily tier</p>
                </div>

                {/* Weekly */}
                <div className="space-y-1">
                  <label className="text-sm font-medium">Weekly backups</label>
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground">Keep 1 per week for last</span>
                    <input
                      type="number"
                      min={0}
                      max={52}
                      className="w-16 border rounded px-2 py-1 text-sm"
                      value={weeklyWindow}
                      onChange={(e) => setWeeklyWindow(Math.max(0, parseInt(e.target.value) || 0))}
                    />
                    <span className="text-sm text-muted-foreground">weeks</span>
                  </div>
                  <p className="text-xs text-muted-foreground">Set to 0 to skip weekly tier</p>
                </div>

                {/* Monthly */}
                <div className="space-y-1">
                  <label className="text-sm font-medium">Monthly backups</label>
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground">Keep 1 per month for last</span>
                    <input
                      type="number"
                      min={0}
                      max={120}
                      className="w-16 border rounded px-2 py-1 text-sm"
                      value={monthlyWindow}
                      onChange={(e) => setMonthlyWindow(Math.max(0, parseInt(e.target.value) || 0))}
                    />
                    <span className="text-sm text-muted-foreground">months</span>
                  </div>
                  <p className="text-xs text-muted-foreground">Set to 0 to skip monthly tier</p>
                </div>
              </div>
            )}

            {retentionEnabled && (
              <div className="rounded-lg border bg-muted/30 p-3 text-sm">
                <p className="font-medium">Summary</p>
                <p className="text-muted-foreground mt-1">
                  {dailyWindow > 0 && `Keep 1 backup per day for the last ${dailyWindow} days. `}
                  {weeklyWindow > 0 && `Keep 1 backup per week for the last ${weeklyWindow} weeks. `}
                  {monthlyWindow > 0 && `Keep 1 backup per month for the last ${monthlyWindow} months. `}
                  {dailyWindow === 0 && weeklyWindow === 0 && monthlyWindow === 0 && 'No retention rules configured — all backups will be kept.'}
                </p>
                <p className="text-muted-foreground mt-1 text-xs">
                  A backup can satisfy multiple tiers. Cleanup runs after each backup and nightly at 3 AM.
                </p>
              </div>
            )}

            <div className="flex gap-2">
              <Button
                type="button"
                disabled={saveSettingsMut.isPending}
                onClick={() => saveSettingsMut.mutate()}
              >
                {saveSettingsMut.isPending ? 'Saving...' : 'Save Retention Settings'}
              </Button>
            </div>
          </div>
        )}
      </AppCard>
    </div>
  )
}


