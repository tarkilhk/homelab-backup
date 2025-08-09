export default function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: React.ReactNode }) {
  return (
    <div className="rounded-lg border bg-card p-8 text-center">
      <div className="text-lg font-medium">{title}</div>
      {hint && <div className="mt-1 text-sm text-muted-foreground">{hint}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}


