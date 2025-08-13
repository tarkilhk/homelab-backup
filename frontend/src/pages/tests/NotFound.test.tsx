import React from 'react'
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { ConfirmProvider } from '../../components/ConfirmProvider'
import App from '../../App'
import Dashboard from '../Dashboard'
import NotFound from '../NotFound'
import { screen as globalScreen } from '@testing-library/react'

function renderWithRouter(route: string) {
  const qc = new QueryClient()
  const router = createMemoryRouter([
    {
      path: '/',
      element: <App />,
      children: [
        { index: true, element: <Dashboard /> },
        { path: '*', element: <NotFound /> },
      ],
    },
  ], { initialEntries: [route] })
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <RouterProvider router={router} />
      </ConfirmProvider>
    </QueryClientProvider>
  )
}

describe('NotFound page', () => {
  it('renders on unknown route and shows a Go home button', async () => {
    renderWithRouter('/some/unknown/path')
    await screen.findByText('Page not found')
    expect(globalScreen.getByText('Go home')).toBeTruthy()
  })
})



