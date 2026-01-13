import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { STORAGE_KEYS, type ThemeMode, applyAccent, applyTheme, getStoredAccent, setStoredAccent, getResolvedTheme } from '../lib/theme'
import { HexColorPicker } from 'react-colorful'
import AppCard from '../components/ui/AppCard'
import { cn } from '../lib/cn'
import { api, type RetentionPolicy, type RetentionRule } from '../api/client'
import { Button } from '../components/ui/button'
import { toast } from 'sonner'
import { Palette, Archive } from 'lucide-react'

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
    <div className="space-y-10">
      {/* Hero header to match Dashboard */}
      <div className="relative overflow-hidden rounded-2xl p-6 border surface-card">
        <h1 className="text-2xl font-semibold">Options</h1>
        <p className="text-sm text-muted-foreground">Personalize your experience.</p>
      </div>

      {/* ═══════════════════════════════════════════════════════════════════════
          APPEARANCE SECTION
          ═══════════════════════════════════════════════════════════════════════ */}
      <section aria-labelledby="appearance-heading">
        {/* Section header */}
        <div className="flex items-center gap-3 mb-5">
          <div className="flex items-center justify-center h-10 w-10 rounded-xl bg-gradient-to-br from-violet-500/20 to-fuchsia-500/20 border border-violet-500/20">
            <Palette className="h-5 w-5 text-violet-400" />
          </div>
          <div>
            <h2 id="appearance-heading" className="text-lg font-semibold tracking-tight">Appearance</h2>
            <p className="text-sm text-muted-foreground">Customize the look and feel of the application</p>
          </div>
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
      </section>

      {/* Visual separator */}
      <div className="relative">
        <div className="absolute inset-0 flex items-center" aria-hidden="true">
          <div className="w-full border-t border-border/40"></div>
        </div>
      </div>

      {/* ═══════════════════════════════════════════════════════════════════════
          DATA MANAGEMENT SECTION
          ═══════════════════════════════════════════════════════════════════════ */}
      <section aria-labelledby="data-heading">
        {/* Section header */}
        <div className="flex items-center gap-3 mb-5">
          <div className="flex items-center justify-center h-10 w-10 rounded-xl bg-gradient-to-br from-emerald-500/20 to-teal-500/20 border border-emerald-500/20">
            <Archive className="h-5 w-5 text-emerald-400" />
          </div>
          <div>
            <h2 id="data-heading" className="text-lg font-semibold tracking-tight">Data Management</h2>
            <p className="text-sm text-muted-foreground">Configure how your backups are stored and cleaned up</p>
          </div>
        </div>

        {/* Retention Policy */}
        <AppCard title="Backup Retention" description="Automatically clean up old backups to save disk space">
          {settingsLoading ? (
            <div className="text-sm text-muted-foreground">Loading...</div>
          ) : (
            <div className="space-y-5">
              {/* Enable toggle */}
              <label className="inline-flex items-center gap-3 cursor-pointer group">
                <div className="relative">
                  <input
                    type="checkbox"
                    className="sr-only peer"
                    checked={retentionEnabled}
                    onChange={(e) => setRetentionEnabled(e.target.checked)}
                  />
                  <div className="w-11 h-6 bg-muted rounded-full peer peer-checked:bg-accent transition-colors"></div>
                  <div className="absolute left-0.5 top-0.5 w-5 h-5 bg-white rounded-full shadow-sm transition-transform peer-checked:translate-x-5"></div>
                </div>
                <span className="text-sm font-medium group-hover:text-foreground transition-colors">Enable retention cleanup</span>
              </label>

              {retentionEnabled && (
                <div className="grid gap-5 sm:grid-cols-3 pt-2">
                  {/* Daily */}
                  <div className="rounded-xl border bg-card/50 p-4 space-y-3">
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-2 rounded-full bg-blue-400"></div>
                      <label className="text-sm font-semibold">Daily</label>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-muted-foreground">Keep</span>
                      <input
                        type="number"
                        min={0}
                        max={365}
                        className="w-16 border rounded-lg px-3 py-1.5 text-sm bg-background focus:ring-2 focus:ring-accent/50 focus:border-accent outline-none transition-shadow"
                        value={dailyWindow}
                        onChange={(e) => setDailyWindow(Math.max(0, parseInt(e.target.value) || 0))}
                      />
                      <span className="text-sm text-muted-foreground">days</span>
                    </div>
                    <p className="text-xs text-muted-foreground">One backup per day</p>
                  </div>

                  {/* Weekly */}
                  <div className="rounded-xl border bg-card/50 p-4 space-y-3">
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-2 rounded-full bg-amber-400"></div>
                      <label className="text-sm font-semibold">Weekly</label>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-muted-foreground">Keep</span>
                      <input
                        type="number"
                        min={0}
                        max={52}
                        className="w-16 border rounded-lg px-3 py-1.5 text-sm bg-background focus:ring-2 focus:ring-accent/50 focus:border-accent outline-none transition-shadow"
                        value={weeklyWindow}
                        onChange={(e) => setWeeklyWindow(Math.max(0, parseInt(e.target.value) || 0))}
                      />
                      <span className="text-sm text-muted-foreground">weeks</span>
                    </div>
                    <p className="text-xs text-muted-foreground">One backup per week</p>
                  </div>

                  {/* Monthly */}
                  <div className="rounded-xl border bg-card/50 p-4 space-y-3">
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-2 rounded-full bg-violet-400"></div>
                      <label className="text-sm font-semibold">Monthly</label>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-muted-foreground">Keep</span>
                      <input
                        type="number"
                        min={0}
                        max={120}
                        className="w-16 border rounded-lg px-3 py-1.5 text-sm bg-background focus:ring-2 focus:ring-accent/50 focus:border-accent outline-none transition-shadow"
                        value={monthlyWindow}
                        onChange={(e) => setMonthlyWindow(Math.max(0, parseInt(e.target.value) || 0))}
                      />
                      <span className="text-sm text-muted-foreground">months</span>
                    </div>
                    <p className="text-xs text-muted-foreground">One backup per month</p>
                  </div>
                </div>
              )}

              {retentionEnabled && (
                <div className="rounded-xl border border-accent/20 bg-accent/5 p-4 text-sm">
                  <p className="font-medium text-foreground flex items-center gap-2">
                    <svg className="h-4 w-4 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    Retention Summary
                  </p>
                  <p className="text-muted-foreground mt-2 leading-relaxed">
                    {dailyWindow > 0 && <span className="inline-flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-blue-400"></span> {dailyWindow} daily backups</span>}
                    {dailyWindow > 0 && (weeklyWindow > 0 || monthlyWindow > 0) && <span className="mx-2 text-border">•</span>}
                    {weeklyWindow > 0 && <span className="inline-flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-amber-400"></span> {weeklyWindow} weekly backups</span>}
                    {weeklyWindow > 0 && monthlyWindow > 0 && <span className="mx-2 text-border">•</span>}
                    {monthlyWindow > 0 && <span className="inline-flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-violet-400"></span> {monthlyWindow} monthly backups</span>}
                    {dailyWindow === 0 && weeklyWindow === 0 && monthlyWindow === 0 && 'No retention rules configured — all backups will be kept.'}
                  </p>
                  <p className="text-muted-foreground mt-2 text-xs">
                    A backup can satisfy multiple tiers. Cleanup runs after each backup and nightly at 3 AM.
                  </p>
                </div>
              )}

              <div className="pt-2">
                <Button
                  type="button"
                  disabled={saveSettingsMut.isPending}
                  onClick={() => saveSettingsMut.mutate()}
                  className="min-w-[160px]"
                >
                  {saveSettingsMut.isPending ? 'Saving...' : 'Save Retention Settings'}
                </Button>
              </div>
            </div>
          )}
        </AppCard>
      </section>
    </div>
  )
}


