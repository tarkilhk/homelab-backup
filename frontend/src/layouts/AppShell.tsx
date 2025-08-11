import { useState, type PropsWithChildren } from 'react'
import { Link } from 'react-router-dom'
import { Menu, X } from 'lucide-react'
import { Toaster } from 'sonner'
import Sidebar from '../components/Sidebar'
import { cn } from '../lib/cn'

export default function AppShell({ children, header }: PropsWithChildren<{ header?: React.ReactNode }>) {
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  return (
    <div className="min-h-screen md:grid md:grid-cols-[260px_1fr]">
      <aside className="hidden md:block border-r bg-background sidebar-surface sticky top-0 h-screen">
        <Sidebar />
      </aside>

      <aside className={cn('fixed inset-0 z-50 md:hidden', mobileNavOpen ? '' : 'pointer-events-none')}>
        <div
          className={cn('absolute inset-0 bg-black/50 transition-opacity', mobileNavOpen ? 'opacity-100' : 'opacity-0')}
          onClick={() => setMobileNavOpen(false)}
        />
        <div
          className={cn(
            'relative h-full w-64 bg-background border-r shadow-lg transition-transform',
            mobileNavOpen ? 'translate-x-0' : '-translate-x-full'
          )}
        >
          <button
            aria-label="Close navigation"
            className="absolute top-2 right-2 p-2 rounded hover:bg-muted"
            onClick={() => setMobileNavOpen(false)}
          >
            <X className="h-5 w-5" />
          </button>
          <Sidebar onNavigate={() => setMobileNavOpen(false)} />
        </div>
      </aside>

      <div className="flex flex-col min-w-0">
        <div className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <div className="px-4 md:px-6 h-14 flex items-center gap-3">
            <button
              aria-label="Toggle navigation"
              aria-expanded={mobileNavOpen}
              onClick={() => setMobileNavOpen(true)}
              className="md:hidden mr-1 px-2 py-1 rounded hover:bg-[color:var(--accent-soft)]"
            >
              <Menu className="h-5 w-5" />
            </button>
            <Link to="/" className="font-bold tracking-tight">Homelab Backup</Link>
            <div className="ml-auto" />
          </div>
          {header ? <div className="px-4 md:px-6 py-2 border-t bg-muted/20">{header}</div> : null}
        </div>
        <div className="min-w-0">
          <div className="px-4 md:px-6 py-6">{children}</div>
        </div>
      </div>

      <Toaster richColors position="top-right" />
    </div>
  )
}


