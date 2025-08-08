import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './index.css'
import App from './App.tsx'
import OptionsPage from './pages/Options.tsx'
import { initThemeFromStorage } from './lib/theme'
import DashboardPage from './pages/Dashboard.tsx'
import TargetsPage from './pages/Targets.tsx'
import RunsPage from './pages/Runs.tsx'
import TargetSchedulePage from './pages/TargetSchedule.tsx'

initThemeFromStorage()

const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'targets', element: <TargetsPage /> },
      { path: 'targets/:id/schedule', element: <TargetSchedulePage /> },
      { path: 'runs', element: <RunsPage /> },
      { path: 'options', element: <OptionsPage /> },
    ],
  },
])

const queryClient = new QueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
