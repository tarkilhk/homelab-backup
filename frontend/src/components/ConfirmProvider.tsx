import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Button } from './ui/button'

type ConfirmVariant = 'default' | 'danger'

export interface ConfirmOptions {
  title?: string
  description?: string
  confirmText?: string
  cancelText?: string
  variant?: ConfirmVariant
}

interface ConfirmState extends Required<ConfirmOptions> {
  open: boolean
}

type Resolver = (value: boolean) => void

interface ConfirmContextValue {
  confirm: (options: ConfirmOptions) => Promise<boolean>
}

const ConfirmContext = createContext<ConfirmContextValue | null>(null)

export function useConfirm(): ConfirmContextValue['confirm'] {
  const ctx = useContext(ConfirmContext)
  if (!ctx) throw new Error('useConfirm must be used within a ConfirmProvider')
  return ctx.confirm
}

export const ConfirmProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [state, setState] = useState<ConfirmState>({
    open: false,
    title: 'Confirm',
    description: '',
    confirmText: 'OK',
    cancelText: 'Cancel',
    variant: 'default',
  })
  const resolverRef = useRef<Resolver | null>(null)

  const close = useCallback((result: boolean) => {
    const resolve = resolverRef.current
    resolverRef.current = null
    setState((s) => ({ ...s, open: false }))
    if (resolve) resolve(result)
  }, [])

  const confirm = useCallback((options: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      resolverRef.current = resolve
      setState({
        open: true,
        title: options.title ?? 'Confirm',
        description: options.description ?? '',
        confirmText: options.confirmText ?? 'OK',
        cancelText: options.cancelText ?? 'Cancel',
        variant: options.variant ?? 'default',
      })
    })
  }, [])

  // Close on Escape
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!state.open) return
      if (e.key === 'Escape') {
        e.preventDefault()
        close(false)
      }
      if (e.key === 'Enter') {
        // Enter confirms by default
        e.preventDefault()
        close(true)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [state.open, close])

  // Focus the confirm button when opening
  const confirmBtnRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (state.open) {
      setTimeout(() => confirmBtnRef.current?.focus(), 0)
    }
  }, [state.open])

  const value = useMemo(() => ({ confirm }), [confirm])

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      {state.open
        ? createPortal(
            <div className="fixed inset-0 z-50 flex items-center justify-center">
              <div className="absolute inset-0 bg-black/50" onClick={() => close(false)} aria-hidden="true" />
              <div
                role="dialog"
                aria-modal="true"
                aria-labelledby="confirm-title"
                aria-describedby="confirm-desc"
                className="relative z-10 w-[min(92vw,420px)] rounded-md border bg-background text-foreground shadow-lg p-4 md:p-5 surface-card"
              >
                <div className="space-y-4">
                  {state.title ? (
                    <h2 id="confirm-title" className="text-lg font-semibold">
                      {state.title}
                    </h2>
                  ) : null}
                  {state.description ? (
                    <p id="confirm-desc" className="text-sm text-muted-foreground whitespace-pre-wrap">
                      {state.description}
                    </p>
                  ) : null}
                  <div className="flex justify-end gap-2 pt-2">
                    <Button variant="outline" onClick={() => close(false)}>
                      {state.cancelText}
                    </Button>
                    <Button
                      ref={confirmBtnRef}
                      variant={state.variant === 'danger' ? 'cancel' : 'default'}
                      onClick={() => close(true)}
                    >
                      {state.confirmText}
                    </Button>
                  </div>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </ConfirmContext.Provider>
  )
}


