import { motion } from 'framer-motion'
import { cn } from '../lib/cn'

export default function StatCard({
  label,
  value,
  trend,
  icon: Icon,
  className,
  onClick,
}: {
  label: string
  value: string | number
  trend?: { delta: string; color?: 'green' | 'red' | 'yellow' }
  icon?: React.ComponentType<any>
  className?: string
  onClick?: () => void
}) {
  return (
    <motion.div
      whileHover={{ y: -2 }}
      className={cn(
        'rounded-2xl border surface-card p-6 shadow-[0_10px_30px_rgba(0,0,0,.45)]',
        'ring-0 hover:ring-2 focus-visible:ring-2 ring-accent transition-shadow',
        onClick && 'cursor-pointer',
        className,
      )}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      <div className="flex items-center gap-3">
        {Icon ? <Icon className="h-5 w-5 text-[hsl(var(--accent))]" aria-hidden="true" /> : null}
        <div className="text-sm text-muted-foreground">{label}</div>
      </div>
      <div className="mt-2 text-5xl font-extrabold tracking-tight drop-shadow">{value}</div>
      {trend && (
        <div className={cn('mt-2 inline-flex items-center rounded-full px-2 py-0.5 text-xs',
          trend.color === 'green' && 'bg-green-500/10 text-green-500',
          trend.color === 'red' && 'bg-red-500/10 text-red-500',
          trend.color === 'yellow' && 'bg-yellow-500/10 text-yellow-500',
        )}>{trend.delta}</div>
      )}
    </motion.div>
  )
}


