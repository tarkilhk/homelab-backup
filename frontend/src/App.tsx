import { Link, NavLink, Outlet } from 'react-router-dom'

export default function App() {
  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-50 border-b bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        <nav className="container-app flex h-20 items-center justify-between">
          <Link to="/" className="text-2xl font-extrabold tracking-tight">
            Homelab Backup
          </Link>
          <div className="flex items-center gap-3 md:gap-6">
            <NavLink
              to="/"
              end
              className={({ isActive }) =>
                `nav-link ${isActive ? 'nav-link-active' : ''}`
              }
            >
              Dashboard
            </NavLink>
            <NavLink
              to="/targets"
              className={({ isActive }) =>
                `nav-link ${isActive ? 'nav-link-active' : ''}`
              }
            >
              Targets
            </NavLink>
            <NavLink
              to="/jobs"
              className={({ isActive }) =>
                `nav-link ${isActive ? 'nav-link-active' : ''}`
              }
            >
              Jobs
            </NavLink>
            <NavLink
              to="/runs"
              className={({ isActive }) =>
                `nav-link ${isActive ? 'nav-link-active' : ''}`
              }
            >
              Runs
            </NavLink>
            <NavLink
              to="/options"
              className={({ isActive }) =>
                `nav-link ${isActive ? 'nav-link-active' : ''}`
              }
            >
              Options
            </NavLink>
          </div>
        </nav>
      </header>
      <main className="container-app">
        <Outlet />
      </main>
    </div>
  )
}
