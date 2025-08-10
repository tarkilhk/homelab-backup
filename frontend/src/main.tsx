import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider, QueryCache, MutationCache } from '@tanstack/react-query'
import { toast } from 'sonner'
import './index.css'
// Routes are centralized in src/routes.tsx
import { initThemeFromStorage } from './lib/theme'
import { ConfirmProvider } from './components/ConfirmProvider'
import { getRoutes } from './routes'

initThemeFromStorage()

const router = createBrowserRouter(getRoutes() as any)

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error) => {
      const message = (error as any)?.message || 'Request failed'
      toast.error(message)
    },
  }),
  mutationCache: new MutationCache({
    onError: (error) => {
      const message = (error as any)?.message || 'Request failed'
      toast.error(message)
    },
  }),
  defaultOptions: {
    queries: {
      retry: false,
    },
    mutations: {
      retry: false,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ConfirmProvider>
        <RouterProvider router={router} />
      </ConfirmProvider>
    </QueryClientProvider>
  </StrictMode>,
)
