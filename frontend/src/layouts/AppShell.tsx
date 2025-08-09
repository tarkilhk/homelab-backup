import type { PropsWithChildren } from 'react'
import Sidebar from '../components/Sidebar'
import { Toaster } from 'sonner'
import { Link } from 'react-router-dom'

export default function AppShell({ children, header }: PropsWithChildren<{ header?: React.ReactNode }>) {
  return (
    <div className="grid min-h-screen grid-cols-[260px_1fr]">
      <aside className="hidden md:block border-r bg-background sidebar-surface sticky top-0 h-screen">
        <Sidebar />
      </aside>
      <div className="flex flex-col min-w-0">
        <div className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <div className="px-4 md:px-6 h-14 flex items-center gap-3">
            <button aria-label="Toggle navigation" className="md:hidden mr-1 px-2 py-1 rounded hover:bg-[color:var(--accent-soft)]">
              <span className="sr-only">Toggle nav</span>
            </button>
            <Link to="/" className="font-bold tracking-tight">Homelab Backup</Link>
            <div className="ml-auto" />
          </div>
          {header ? <div className="px-4 md:px-6 py-2 border-t bg-muted/20">{header}</div> : null}
        </div>
        <div className="min-w-0">
          <div className="px-4 md:px-6 py-6">
            {children}
          </div>
        </div>
      </div>
      <Toaster richColors position="top-right" />
    </div>
  )
}


