import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import i18next from 'i18next'
import { createRoot } from 'react-dom/client'
import { initReactI18next } from 'react-i18next'

import { AnnouncerProvider } from '@/components/ui/announcer'
import { installMillenniumIconStyle } from '@/components/ui/millennium-icon-set'
import { Toaster } from '@/components/ui/toaster'
import { LearningPage } from '@/routes/resource/learning'

import zh from './i18n/locales/zh.json'
import './index.css'
import './styles/controls.css'
import './styles/millennium.css'
import './styles/mobile.css'
import './styles/surfaces.css'
import './styles/data-browser.css'
import './styles/workspace.css'
import './styles/management.css'
import './host.css'

await i18next.use(initReactI18next).init({
  lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false },
})

const rootRoute = createRootRoute({ component: Outlet })
const protectedRoute = createRoute({ getParentRoute: () => rootRoute, id: 'protected', component: Outlet })
const learningRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: '/resource/learning',
  validateSearch: (search: Record<string, unknown>) => ({
    tab: search.tab === 'jargon' || search.tab === 'emoji' ? search.tab : 'expression',
  } as { tab: 'expression' | 'jargon' | 'emoji' }),
  component: LearningPage,
})
const tab = new URLSearchParams(location.search).get('tab')
const router = createRouter({
  routeTree: rootRoute.addChildren([protectedRoute.addChildren([learningRoute])]),
  history: createMemoryHistory({ initialEntries: [`/resource/learning${tab ? `?tab=${encodeURIComponent(tab)}` : ''}`] }),
})

declare module '@tanstack/react-router' {
  interface Register { router: typeof router }
}

router.subscribe('onResolved', ({ toLocation }) => {
  const current = new URL(location.href)
  current.searchParams.set('tab', toLocation.search.tab ?? 'expression')
  history.replaceState(null, '', current)
})

const setTheme = (theme: unknown) => document.documentElement.classList.toggle('dark', theme === 'dark')
setTheme(localStorage.getItem('aureus-theme'))
window.addEventListener('message', event => {
  if (event.origin === location.origin && event.source === parent && event.data?.type === 'aureus-theme') {
    setTheme(event.data.theme)
  }
})
document.documentElement.dataset.dashboardStyle = 'millennium'
installMillenniumIconStyle()

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <AnnouncerProvider>
      <RouterProvider router={router} />
      <Toaster />
    </AnnouncerProvider>
  </QueryClientProvider>,
)
