import { cn } from '../lib/cn'

export type MultiSelectOption = { value: string; label: string }

type Props = {
  options: MultiSelectOption[]
  values: string[]
  onChange: (nextValues: string[]) => void
  className?: string
  ariaLabel?: string
  onItemDoubleClick?: (value: string) => void
}

export default function MultiSelectList({ options, values, onChange, className, ariaLabel, onItemDoubleClick }: Props) {
  const toggle = (value: string) => {
    const has = values.includes(value)
    onChange(has ? values.filter((v) => v !== value) : [...values, value])
  }

  return (
    <div
      role="listbox"
      aria-multiselectable={true}
      aria-label={ariaLabel}
      className={cn('border rounded bg-background min-h-[8rem] max-h-64 overflow-auto p-1 focus:outline-none focus-visible:ring-2 ring-[hsl(var(--accent))]', className)}
      tabIndex={0}
    >
      {options.map((opt) => {
        const selected = values.includes(opt.value)
        return (
          <div
            key={opt.value}
            role="option"
            aria-selected={selected}
            tabIndex={-1}
            onClick={() => toggle(opt.value)}
            onDoubleClick={() => onItemDoubleClick?.(opt.value)}
            className={cn(
              'px-3 py-2 rounded cursor-pointer select-none',
              selected ? 'bg-[hsl(var(--accent)/.22)] text-[hsl(var(--accent-foreground))]' : 'hover:bg-[hsl(var(--accent)/.10)]'
            )}
          >
            {opt.label}
          </div>
        )
      })}
    </div>
  )
}


