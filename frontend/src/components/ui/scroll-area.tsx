import * as React from 'react'
import { cn } from '../../lib/cn'

// Minimal ScrollArea that composes a div with overflow-auto.
// Kept simple to avoid extra deps; style via className.
export function ScrollArea({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cn('overflow-auto', className)}>
      {children}
    </div>
  )
}


