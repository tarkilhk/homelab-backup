import { isRouteErrorResponse, useRouteError } from 'react-router-dom'
import NotFoundPage from './NotFound'

export default function ErrorPage() {
  const error = useRouteError()
  if (isRouteErrorResponse(error) && error.status === 404) {
    return <NotFoundPage />
  }
  return (
    <div className="mx-auto max-w-xl text-center py-16">
      <h1 className="text-3xl font-bold tracking-tight mb-2">Something went wrong</h1>
      <p className="text-[color:var(--muted-foreground)]">
        An unexpected error occurred. Please try again.
      </p>
    </div>
  )
}



