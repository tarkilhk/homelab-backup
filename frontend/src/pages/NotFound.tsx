import { Link } from 'react-router-dom'
import { Ghost } from 'lucide-react'
import { Button } from '../components/ui/button'

export default function NotFoundPage() {
  return (
    <div className="mx-auto max-w-xl text-center py-24">
      <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full border border-[hsl(var(--accent)/.35)] bg-[hsl(var(--accent)/.10)] shadow-[0_6px_20px_rgba(0,0,0,.25)]">
        <Ghost className="h-7 w-7 text-[hsl(var(--accent))]" />
      </div>
      <p className="text-[color:var(--muted-foreground)] text-sm mb-1">404</p>
      <h1 className="text-3xl font-bold tracking-tight mb-2">Page not found</h1>
      <p className="text-[color:var(--muted-foreground)] mb-6">
        The page you’re looking for doesn’t exist or may have been moved.
      </p>
      <div className="mt-2">
        <Link to="/">
          <Button size="lg">Go home</Button>
        </Link>
      </div>
    </div>
  )
}



