import { Link, NavLink } from 'react-router-dom'
import { cn } from '../lib/cn'
import { Home, Target, ListChecks, Timer, Settings } from 'lucide-react'
import logoUrl from '../assets/homelab-backup-logo.png'

type Item = { to: string; label: string; icon: React.ComponentType<any> }

const groups: { header: string; items: Item[] }[] = [
  {
    header: 'Core',
    items: [
      { to: '/', label: 'Dashboard', icon: Home },
    ],
  },
  {
    header: 'Features',
    items: [
      { to: '/targets', label: 'Targets', icon: Target },
      { to: '/jobs', label: 'Jobs', icon: ListChecks },
      { to: '/runs', label: 'Runs', icon: Timer },
    ],
  },
  {
    header: 'Settings',
    items: [
      { to: '/options', label: 'Options', icon: Settings },
    ],
  },
]

export default function Sidebar() {
  return (
    <div className="h-full flex flex-col">
      {/* Brand */}
      <div className="px-4 h-20 flex items-center">
        <Link to="/" className="flex items-center gap-3 group">
          <img
            src={logoUrl}
            alt="Homelab Backup"
            className="h-12 w-12 rounded-full ring-2 ring-[hsl(var(--accent))] shadow-sm"
          />
          <div className="font-semibold tracking-tight group-hover:text-[hsl(var(--accent))] transition-colors">
            Homelab Backup
          </div>
        </Link>
      </div>
      <nav className="px-2 pt-6 pb-8 overflow-y-auto">
        {groups.map((g) => (
          <div key={g.header} className="mb-6">
            <div className="mx-2 mb-3 mt-1 inline-flex items-center rounded-md bg-[hsl(var(--accent)/.08)] px-2.5 py-1.5 text-xs font-bold uppercase tracking-wide text-[hsl(var(--accent)/.95)] ring-1 ring-[hsl(var(--accent)/.25)] shadow-[inset_0_1px_0_hsl(var(--accent)/.15)]">
              {g.header}
            </div>
            <ul className="space-y-2">
              {g.items.map((it) => (
                <li key={it.to}>
                  <NavLink
                    to={it.to}
                    end={it.to === '/'}
                    className={({ isActive }) => cn(
                      'group flex items-center gap-3 rounded-md px-3 py-2.5 text-[15px] font-medium transition-colors',
                      'sidebar-hover',
                      isActive && 'sidebar-selected border-l-2 border-[hsl(var(--accent))]')}
                  >
                    <it.icon className="h-4 w-4 text-[hsl(var(--accent))]" aria-hidden="true" />
                    <span>{it.label}</span>
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
    </div>
  )
}


