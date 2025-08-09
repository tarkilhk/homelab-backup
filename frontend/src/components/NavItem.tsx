import { NavLink } from 'react-router-dom'
import { cn } from '../lib/cn'

export default function NavItem({ to, icon: Icon, label, end }: { to: string; icon: React.ComponentType<any>; label: string; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) => cn(
        'group flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors',
        'hover:bg-[color:var(--accent-soft)]',
        isActive && 'sidebar-active border-l-2 border-[hsl(var(--accent))]')}
    >
      <Icon className="h-4 w-4 text-[hsl(var(--accent))]" aria-hidden="true" />
      <span>{label}</span>
    </NavLink>
  )
}


