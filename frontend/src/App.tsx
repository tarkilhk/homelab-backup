import { Link, Outlet } from 'react-router-dom'

export default function App() {
  return (
    <div className="min-h-full">
      <header className="border-b bg-background/70 backdrop-blur">
        <nav className="container-app flex items-center gap-6">
          <Link to="/" className="font-semibold">Homelab Backup</Link>
          <div className="flex gap-4 text-sm">
            <Link to="/" className="hover:underline">Dashboard</Link>
            <Link to="/targets" className="hover:underline">Targets</Link>
            {/* Jobs page can be added later; scheduling lives under a target */}
            <Link to="/runs" className="hover:underline">Runs</Link>
            <Link to="/options" className="hover:underline">Options</Link>
          </div>
        </nav>
      </header>
      <main className="container-app">
        <Outlet />
      </main>
    </div>
  )
}
