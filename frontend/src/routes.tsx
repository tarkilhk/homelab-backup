import App from './App'
import DashboardPage from './pages/Dashboard'
import TargetsPage from './pages/Targets'
import GroupsPage from './pages/Groups'
import TagsPage from './pages/Tags'
import JobsPage from './pages/Jobs'
import RunsPage from './pages/Runs'
import RestorePage from './pages/Restore'
import OptionsPage from './pages/Options'
import ErrorPage from './pages/ErrorPage'
import NotFoundPage from './pages/NotFound'

export function getRoutes() {
  return [
    {
      path: '/',
      element: <App />,
      errorElement: <ErrorPage />,
      children: [
        { index: true, element: <DashboardPage /> },
        { path: 'targets', element: <TargetsPage /> },
        { path: 'groups', element: <GroupsPage /> },
        { path: 'tags', element: <TagsPage /> },
        { path: 'targets/:id/jobs', element: <JobsPage /> },
        { path: 'jobs', element: <JobsPage /> },
        { path: 'runs', element: <RunsPage /> },
        { path: 'restore', element: <RestorePage /> },
        { path: 'options', element: <OptionsPage /> },
        // Catch-all for unknown nested routes
        { path: '*', element: <NotFoundPage /> },
      ],
    },
  ] as const
}


