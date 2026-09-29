import {
  Navigate,
  Outlet,
  createRootRoute,
  createRoute,
  createRouter,
  redirect,
} from '@tanstack/react-router'
import { Shell } from './Shell'
import { LoginPage } from '../features/auth/LoginPage'
import { checkAuthenticated } from '../features/auth/auth-state'
import { PlanningPage } from '../features/planning/PlanningPage'
import { RequestsPage } from '../features/requests/RequestsPage'
import { EngineersPage } from '../features/engineers/EngineersPage'
import { DataPage } from '../features/data/DataPage'
import { EngineerRoutePage } from '../features/planning/EngineerRoutePage'
import { AnalyticsPage } from '../features/analytics/AnalyticsPage'
import { NormsPage } from '../features/norms/NormsPage'

function RootLayout() {
  return <Outlet />
}

const rootRoute = createRootRoute({
  component: RootLayout,
})

export const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: LoginPage,
  beforeLoad: async () => {
    if (await checkAuthenticated()) {
      throw redirect({ to: '/planning' })
    }
  },
})

export const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: '/_app',
  component: Shell,
  beforeLoad: async () => {
    if (!(await checkAuthenticated())) {
      throw redirect({ to: '/login' })
    }
  },
})

export const indexRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/',
  component: () => <Navigate to="/planning" />,
})

export const planningRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/planning',
  component: PlanningPage,
})

export const analyticsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/analytics',
  component: AnalyticsPage,
})

const engineerRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/routes/$planId/$engineerId',
  component: EngineerRoutePage,
})

export const requestsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/requests',
  validateSearch: (search: Record<string, unknown>) => ({
    q: typeof search.q === 'string' ? search.q : '',
    district: typeof search.district === 'string' ? search.district : '',
    priority: typeof search.priority === 'string' ? search.priority : '',
    request: typeof search.request === 'string' ? search.request : '',
  }),
  component: RequestsPage,
})

export const engineersRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/engineers',
  component: EngineersPage,
})

export const dataRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/data',
  component: DataPage,
})

export const normsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/norms',
  component: NormsPage,
})

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    indexRoute,
    planningRoute,
    analyticsRoute,
    engineerRoute,
    requestsRoute,
    engineersRoute,
    normsRoute,
    dataRoute,
  ]),
])

export const router = createRouter({ routeTree, defaultPreload: 'intent' })

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
