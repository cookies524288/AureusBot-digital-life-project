import { createContext, useContext } from 'react'

/**
 * 浮层挂载容器。
 *
 * 工作台以内联方式运行在主页面创建的 Shadow DOM 里，Radix 默认把弹窗、菜单
 * 挂到 document.body，样式会被隔离在 Shadow DOM 之外。主入口提供 React 根节点
 * 作为容器，浮层留在影子树内；普通浏览器页面不提供容器，沿用 document.body。
 */
export const PortalContainerContext = createContext<HTMLElement | null>(null)

export const usePortalContainer = () => useContext(PortalContainerContext)
