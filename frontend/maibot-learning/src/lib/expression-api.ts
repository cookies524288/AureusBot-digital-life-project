/**
 * 表达方式管理 API
 *
 * 请求样板（认证、解析、错误格式化）由 @/lib/http 的请求客户端承担；
 * 本文件只声明 endpoint、业务错误文案与响应体 success 标记的解包规则。
 * 公开函数遵循 throw 契约：成功返回数据，失败抛 ApiError。
 */
import { backendApi, requireSuccess } from '@/lib/http'
import type {
  ChatInfo,
  ChatListResponse,
  Expression,
  ExpressionCreateRequest,
  ExpressionCreateResponse,
  ExpressionDeleteResponse,
  ExpressionDetailResponse,
  ExpressionClearResponse,
  ExpressionClusterListResponse,
  ExpressionClusterMemberListResponse,
  ExpressionExportItem,
  ExpressionExportResponse,
  ExpressionImportResponse,
  ExpressionGroupListResponse,
  ExpressionListResponse,
  ExpressionStats,
  ExpressionStatsResponse,
  ExpressionUpdateRequest,
  ExpressionUpdateResponse,
  LegacyExpressionImportPreviewResponse,
  LegacyExpressionImportResponse,
} from '@/types/expression'
import type { VectorSpaceList } from '@/types/vector-space'

const API_BASE = '/api/webui/expression'

export async function getExpressionVectorSpaces(): Promise<VectorSpaceList> {
  const data = await backendApi.get<VectorSpaceList>(`${API_BASE}/runtime/vectors/spaces`, {
    errorMessage: '获取表达向量库失败',
  })
  return requireSuccess(data, '获取表达向量库失败')
}

export async function deleteExpressionVectorSpace(spaceId: string): Promise<{ success: boolean }> {
  const data = await backendApi.delete<{ success: boolean }>(
    `${API_BASE}/runtime/vectors/spaces/${encodeURIComponent(spaceId)}`,
    { errorMessage: '删除表达向量库失败' }
  )
  return requireSuccess(data, '删除表达向量库失败')
}

export interface ExpressionVectorBuildProgress {
  status: 'disabled' | 'unconfigured' | 'waiting_profile' | 'pending' | 'running' | 'completed'
  completed: number
  total: number
  percent: number
}

export function getExpressionVectorBuildProgress(): Promise<ExpressionVectorBuildProgress> {
  return backendApi.get<ExpressionVectorBuildProgress>(`${API_BASE}/vector-build-progress`, {
    errorMessage: '获取表达库构建进度失败',
  })
}

/**
 * 获取聊天列表
 */
export async function getChatList(params: { include_legacy?: boolean } = {}): Promise<ChatInfo[]> {
  const data = await backendApi.get<ChatListResponse>(`${API_BASE}/chats`, {
    query: { include_legacy: params.include_legacy ? true : undefined },
    errorMessage: '获取聊天列表失败',
  })
  return requireSuccess(data, '获取聊天列表失败').data
}

/**
 * 获取可作为导入目标的全部聊天流。
 */
export async function getExpressionChatTargets(
  params: { include_legacy?: boolean } = {}
): Promise<ChatInfo[]> {
  const data = await backendApi.get<ChatListResponse>(`${API_BASE}/chat-targets`, {
    query: { include_legacy: params.include_legacy ? true : undefined },
    errorMessage: '获取导入目标聊天流失败',
  })
  return requireSuccess(data, '获取导入目标聊天流失败').data
}

/**
 * 获取表达共享组列表
 */
export async function getExpressionGroups(
  params: { include_legacy?: boolean } = {}
): Promise<ExpressionGroupListResponse['data']> {
  const data = await backendApi.get<ExpressionGroupListResponse>(`${API_BASE}/groups`, {
    query: { include_legacy: params.include_legacy ? true : undefined },
    errorMessage: '获取表达共享组失败',
  })
  return requireSuccess(data, '获取表达共享组失败').data
}

/**
 * 获取表达方式列表
 */
export async function getExpressionList(params: {
  page?: number
  page_size?: number
  search?: string
  chat_id?: string
  chat_ids?: string[]
  include_legacy?: boolean
  sort_by?: 'time' | 'time_asc' | 'created' | 'created_asc'
}): Promise<ExpressionListResponse> {
  const data = await backendApi.get<ExpressionListResponse>(`${API_BASE}/list`, {
    query: {
      page: params.page || undefined,
      page_size: params.page_size || undefined,
      search: params.search || undefined,
      chat_id: params.chat_id || undefined,
      include_legacy: params.include_legacy ? true : undefined,
      sort_by: params.sort_by,
      chat_ids: params.chat_ids,
    },
    errorMessage: '获取表达方式列表失败',
  })
  return requireSuccess(data, '获取表达方式列表失败')
}

/**
 * 按聊天导出表达方式。导出的 JSON 不包含 session_id。
 */
export async function exportExpressions(params: {
  chat_id: string
  ids?: number[]
}): Promise<ExpressionExportResponse> {
  return backendApi.post<ExpressionExportResponse>(`${API_BASE}/export`, {
    body: params,
    errorMessage: '导出表达方式失败',
  })
}

/**
 * 将表达方式 JSON 导入到指定聊天。
 */
export async function importExpressions(params: {
  chat_id?: string
  is_global?: boolean
  expressions: ExpressionExportItem[]
}): Promise<ExpressionImportResponse> {
  return backendApi.post<ExpressionImportResponse>(`${API_BASE}/import`, {
    body: params,
    errorMessage: '导入表达方式失败',
  })
}

/**
 * 清除指定聊天下的全部表达方式。
 */
export async function clearExpressions(params: {
  chat_id: string
}): Promise<ExpressionClearResponse> {
  return backendApi.post<ExpressionClearResponse>(`${API_BASE}/clear`, {
    body: params,
    errorMessage: '清除表达方式失败',
  })
}

/**
 * 预览旧版数据库表达方式导入。
 */
export async function previewLegacyExpressionImport(params: {
  db_path: string
}): Promise<LegacyExpressionImportPreviewResponse> {
  return backendApi.post<LegacyExpressionImportPreviewResponse>(
    `${API_BASE}/legacy-import/preview`,
    {
      body: params,
      errorMessage: '预览旧版导入失败',
    }
  )
}

/**
 * 上传旧版数据库并预览表达方式导入。
 */
export async function previewLegacyExpressionImportFile(
  file: File
): Promise<LegacyExpressionImportPreviewResponse> {
  const formData = new FormData()
  formData.append('file', file)
  return backendApi.post<LegacyExpressionImportPreviewResponse>(
    `${API_BASE}/legacy-import/preview-file`,
    {
      body: formData,
      errorMessage: '预览旧版导入失败',
    }
  )
}

/**
 * 按映射从旧版数据库导入表达方式。
 */
export async function importLegacyExpressions(params: {
  db_path: string
  mappings: Array<{
    old_chat_id: string
    target_chat_id?: string | null
    target_chat_ids?: string[]
  }>
}): Promise<LegacyExpressionImportResponse> {
  return backendApi.post<LegacyExpressionImportResponse>(`${API_BASE}/legacy-import/import`, {
    body: params,
    errorMessage: '旧版导入失败',
  })
}

/**
 * 获取表达方式详细信息
 */
export async function getExpressionDetail(expressionId: number): Promise<Expression> {
  const data = await backendApi.get<ExpressionDetailResponse>(`${API_BASE}/${expressionId}`, {
    errorMessage: '获取表达方式详情失败',
  })
  return requireSuccess(data, '获取表达方式详情失败').data
}

/**
 * 创建表达方式
 */
export async function createExpression(data: ExpressionCreateRequest): Promise<Expression> {
  const responseData = await backendApi.post<ExpressionCreateResponse>(`${API_BASE}/`, {
    body: data,
    errorMessage: '创建表达方式失败',
  })
  return requireSuccess(responseData, '创建表达方式失败').data
}

/**
 * 更新表达方式（增量更新）
 */
export async function updateExpression(
  expressionId: number,
  data: ExpressionUpdateRequest
): Promise<Expression | Record<string, never>> {
  const responseData = await backendApi.patch<ExpressionUpdateResponse>(
    `${API_BASE}/${expressionId}`,
    {
      body: data,
      errorMessage: '更新表达方式失败',
    }
  )
  return requireSuccess(responseData, '更新表达方式失败').data || {}
}

/**
 * 删除表达方式
 */
export async function deleteExpression(expressionId: number): Promise<Record<string, never>> {
  const data = await backendApi.delete<ExpressionDeleteResponse>(`${API_BASE}/${expressionId}`, {
    errorMessage: '删除表达方式失败',
  })
  requireSuccess(data, '删除表达方式失败')
  return {}
}

/**
 * 批量删除表达方式
 */
export async function batchDeleteExpressions(
  expressionIds: number[]
): Promise<Record<string, never>> {
  const data = await backendApi.post<ExpressionDeleteResponse>(`${API_BASE}/batch/delete`, {
    body: { ids: expressionIds },
    errorMessage: '批量删除表达方式失败',
  })
  requireSuccess(data, '批量删除表达方式失败')
  return {}
}

/**
 * 获取表达方式统计数据
 */
export async function getExpressionStats(
  params: { include_legacy?: boolean } = {}
): Promise<ExpressionStats> {
  const data = await backendApi.get<ExpressionStatsResponse>(`${API_BASE}/stats/summary`, {
    query: { include_legacy: params.include_legacy ? true : undefined },
    errorMessage: '获取统计数据失败',
  })
  return requireSuccess(data, '获取统计数据失败').data
}

/**
 * 获取表达向量聚类摘要。
 */
export async function getExpressionClusters(): Promise<ExpressionClusterListResponse> {
  const data = await backendApi.get<ExpressionClusterListResponse>(`${API_BASE}/clusters`, {
    errorMessage: '获取表达聚类失败',
  })
  return requireSuccess(data, '获取表达聚类失败')
}

/**
 * 获取指定表达聚类的成员。
 */
export async function getExpressionClusterMembers(params: {
  cluster_id: number
  profile_marker: string
  offset: number
  limit: number
}): Promise<ExpressionClusterMemberListResponse> {
  const data = await backendApi.get<ExpressionClusterMemberListResponse>(
    `${API_BASE}/clusters/${params.cluster_id}/members`,
    {
      query: { profile_marker: params.profile_marker, offset: params.offset, limit: params.limit },
      errorMessage: '获取表达聚类成员失败',
    }
  )
  return requireSuccess(data, '获取表达聚类成员失败')
}
