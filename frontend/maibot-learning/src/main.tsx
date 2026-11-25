import { MutationCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import i18next from 'i18next'
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { initReactI18next } from 'react-i18next'

import { AnnouncerProvider } from '@/components/ui/announcer'
import { MobileNavContext } from '@/components/layout/mobile-nav-context'
import { installMillenniumIconStyle } from '@/components/ui/millennium-icon-set'
import { Toaster } from '@/components/ui/toaster'
import { toast } from '@/hooks/use-toast'
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
  path: '/',
  validateSearch: (search: Record<string, unknown>) => ({
    tab: search.tab === 'jargon' || search.tab === 'emoji' ? search.tab : 'expression',
  } as { tab: 'expression' | 'jargon' | 'emoji' }),
  component: LearningHost,
})

function LearningHost() {
  const [headerActionsElement, setHeaderActionsElement] = useState<HTMLDivElement | null>(null)
  const [title, setTitleOverride] = useState<string | null>(null)
  return (
    <MobileNavContext.Provider value={{ menuSections: [], openSearch: () => {},
      setTabBarHidden: () => {}, headerActionsElement, setTitleOverride }}>
      <div className="flex h-full min-h-0 flex-col">
        <header className="flex min-h-10 shrink-0 items-center justify-between px-3 lg:hidden">
          <strong>{title ?? '学习'}</strong><div ref={setHeaderActionsElement} className="flex gap-2" />
        </header>
        <main className="min-h-0 flex-1"><LearningPage /></main>
      </div>
    </MobileNavContext.Provider>
  )
}
const router = createRouter({
  routeTree: rootRoute.addChildren([protectedRoute.addChildren([learningRoute])]),
  basepath: '/learning-workbench',
  history: createBrowserHistory(),
})

declare module '@tanstack/react-router' {
  interface Register { router: typeof router }
}

const setTheme = (theme: unknown) => document.documentElement.classList.toggle('dark', theme === 'dark')
setTheme(localStorage.getItem('aureus-theme'))
window.addEventListener('message', event => {
  if (event.origin === location.origin && event.source === parent && event.data?.type === 'aureus-theme') {
    setTheme(event.data.theme)
  }
})
document.documentElement.dataset.dashboardStyle = 'millennium'
installMillenniumIconStyle()

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
  mutationCache: new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      if (mutation.options.onError) return
      const title = mutation.meta?.errorTitle
      toast({ title: typeof title === 'string' ? title : '操作失败', description: error.message, variant: 'destructive' })
    },
  }),
})
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <AnnouncerProvider>
      <RouterProvider router={router} />
      <Toaster />
    </AnnouncerProvider>
  </QueryClientProvider>,
)
