import type { ButtonHTMLAttributes } from 'react'
import { cn } from '../lib/cn'

type Variant = 'accent' | 'success' | 'warning' | 'danger' | 'outline'

export default function IconButton({
  className,
  variant = 'accent',
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const base = 'inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--accent))] disabled:opacity-50 disabled:pointer-events-none transition-transform active:scale-[.98]'
  const styles: Record<Variant, string> = {
    accent: 'bg-[hsl(var(--accent))] text-[hsl(var(--accent-foreground))] hover:opacity-95',
    success: 'bg-green-600 text-white hover:bg-green-700',
    warning: 'bg-amber-500 text-black hover:bg-amber-600',
    danger: 'bg-red-600 text-white hover:bg-red-700',
    outline: 'border hover:bg-[color:var(--accent-soft)]',
  }
  return (
    <button className={cn(base, styles[variant], className)} {...props}>
      {children}
    </button>
  )
}


