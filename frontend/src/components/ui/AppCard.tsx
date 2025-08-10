import { motion } from 'framer-motion'
import type { PropsWithChildren } from 'react'
import { cn } from '../../lib/cn'

type Props = PropsWithChildren<{
  title?: string
  description?: string
  className?: string
  headerRight?: React.ReactNode
  onTitleClick?: () => void
}>

export default function AppCard({ title, description, className, headerRight, onTitleClick, children }: Props) {
  const isInteractive = Boolean(onTitleClick)
  const hasHeaderContent = Boolean(title || description || headerRight)
  return (
    <motion.section
      initial={{ y: 6, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ duration: 0.18, ease: 'easeOut' }}
      whileHover={isInteractive ? { y: -2 } : undefined}
      className={cn(
        // Only show accent rings for interactive cards. Non-clickable cards
        // should not display a focus-within ring when inner inputs are focused.
        'rounded-2xl border surface-card shadow-[0_6px_20px_rgba(0,0,0,.35)] transition-shadow',
        isInteractive && 'ring-0 hover:ring-2 ring-[hsl(var(--accent))]',
        className
      )}
    >
      {hasHeaderContent && (
        <div
          className={cn(
            'border-b px-5 py-3.5 flex items-center gap-2 bg-gradient-to-r from-[hsl(var(--accent)/.10)] to-transparent',
            onTitleClick && 'cursor-pointer hover:bg-[hsl(var(--accent)/.14)]'
          )}
          onClick={onTitleClick}
          role={onTitleClick ? 'button' : undefined}
          tabIndex={onTitleClick ? 0 : undefined}
          onKeyDown={(e) => {
            if (!onTitleClick) return
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              onTitleClick()
            }
          }}
        >
          {(title || description) && (
            <div className="font-semibold tracking-tight">
              {title}
              {description && <div className="text-xs font-normal text-muted-foreground">{description}</div>}
            </div>
          )}
          <div className={cn((title || description) ? 'ml-auto' : undefined)}>{headerRight}</div>
        </div>
      )}
      <div className="px-5 py-4">{children}</div>
    </motion.section>
  )
}


