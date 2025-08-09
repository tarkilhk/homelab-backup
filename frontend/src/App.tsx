import AppShell from './layouts/AppShell'
import PageTransition from './components/PageTransition'
import { Outlet } from 'react-router-dom'

export default function App() {
  // Legacy wrapper for nested routes; keep API and router shape intact
  return (
    <AppShell>
      <PageTransition>
        <Outlet />
      </PageTransition>
    </AppShell>
  )
}
