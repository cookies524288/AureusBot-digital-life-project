import { MutationCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import i18next from 'i18next'
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { initReactI18next } from 'react-i18next'

import { AnnouncerProvider } from '@/components/ui/announcer'
import { MobileNavContext } from '@/components/layout/mobile-nav-context'
import { Toaster } from '@/components/ui/toaster'
import { toast } from '@/hooks/use-toast'
import { PortalContainerContext } from '@/lib/portal-container'
import { LearningPage } from '@/routes/resource/learning'

import zh from './i18n/locales/zh.json'
import './index.css'
import './styles/controls.css'
import './styles/mobile.css'
import './styles/surfaces.css'
import './styles/data-browser.css'
import './styles/workspace.css'
import './styles/management.css'
import './host.css'

declare global {
  interface Window {
    /** 主页面提供的 React 挂载节点（Shadow DOM 内的容器）。 */
    __aureusWorkbenchHost?: HTMLElement
    /** 主页面要求工作台只使用全局范围。 */
    __aureusWorkbenchGlobalScope?: boolean
  }
}

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
  // 页签只存在内存里，不读写主页面的地址栏。
  history: createMemoryHistory({ initialEntries: ['/'] }),
})

declare module '@tanstack/react-router' {
  interface Register { router: typeof router }
}

// 主页面传入 Shadow DOM 内的容器；独立 index.html / 开发服务器退回普通 #root。
const host = window.__aureusWorkbenchHost ?? document.getElementById('root')
if (!host) throw new Error('学习工作台缺少挂载宿主。')
// 影子树内没有 document.body，浮层改挂到 React 根节点；普通页面继续用 Radix 默认行为。
const portalContainer = host.getRootNode() instanceof ShadowRoot ? host : null

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
createRoot(host).render(
  <QueryClientProvider client={queryClient}>
    <PortalContainerContext.Provider value={portalContainer}>
      <AnnouncerProvider>
        <RouterProvider router={router} />
        <Toaster />
      </AnnouncerProvider>
    </PortalContainerContext.Provider>
  </QueryClientProvider>,
)
