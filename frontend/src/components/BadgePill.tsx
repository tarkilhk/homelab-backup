import { cn } from '../lib/cn'

export default function BadgePill({ children, color = 'accent', className }: { children: React.ReactNode; color?: 'accent' | 'green' | 'yellow' | 'red' | 'gray'; className?: string }) {
  const map = {
    accent: 'bg-[color:var(--accent-soft)] text-[hsl(var(--accent))]',
    green: 'bg-green-500/10 text-green-500',
    yellow: 'bg-amber-500/10 text-amber-600',
    red: 'bg-red-500/10 text-red-600',
    gray: 'bg-muted text-muted-foreground',
  }
  return <span className={cn('inline-flex items-center rounded-full px-2 py-0.5 text-xs', map[color], className)}>{children}</span>
}


