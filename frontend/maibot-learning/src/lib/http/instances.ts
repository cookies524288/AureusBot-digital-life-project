/**
 * 请求客户端的两个适配器实例。
 *
 * - backendApi：主后端（MaiBot 本体 HTTP API），Cookie 认证，401 跳转登录页
 * - statsApi：统计服务（Cloudflare Workers 上的问卷/插件统计），无认证
 */
import { createApiClient } from './client'

/** 统计服务地址（Cloudflare Workers） */

/** 主后端实例：浏览器同源 / Electron 动态后端 URL */
export const backendApi = createApiClient({
  resolveBaseUrl: () => '',
  auth: 'cookie',
  onUnauthorized: () => {
    if (window.parent === window) location.assign('/')
    else parent.postMessage({ type: 'aureus-login-required' }, location.origin)
  },
})

/** 统计服务实例：外部服务，不携带凭据 */

/**
 * 认证流程实例：携带 Cookie 但不配置 onUnauthorized——
 * 登录验证、认证状态探测中 401 是正常业务结果，必须透传后端信息而不是跳转登录页。
 */
