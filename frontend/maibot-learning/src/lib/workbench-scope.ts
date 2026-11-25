/**
 * 主页面以内联方式加载工作台时，只使用全局范围：
 * 不显示聊天范围面板，列表和导入导出固定走全局数据。
 */
export const workbenchGlobalScope = window.__aureusWorkbenchGlobalScope === true
