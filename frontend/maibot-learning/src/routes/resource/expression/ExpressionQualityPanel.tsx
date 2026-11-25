import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { backendApi } from '@/lib/http'

interface QualityItem {
  id: number
  status: string
  reason: string
  recorded_at: string
  source_label: string
  text: string
  detail: string
  actions: Array<[string, string]>
}

interface QualityResponse {
  enabled: boolean
  items: QualityItem[]
  total: number
  sources: Array<{ key: string; label: string }>
}

type QualityAction = 'rescued' | 'withdrawn'

interface QualityPreview {
  id: number
  scope: string
  text: string
  detail: string
  version: string
}

const statuses = { passed: '模型通过', rejected: '模型拒绝', error: '质检错误' }
const reasons = {
  matched: '情境与说法匹配', mismatch: '情境不匹配', too_specific: '说法过于特指',
  no_route: '没有可用质检器官', blocked: '预算或安全网关拒绝', failed: '质检调用失败', invalid: '质检结果无效',
}

export function ExpressionQualityPanel() {
  const [result, setResult] = useState<QualityResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<number | null>(null)
  const [source, setSource] = useState('')
  const generation = useRef(0)
  const [reloadToken, setReloadToken] = useState(0)
  const [offset, setOffset] = useState(0)
  const [pageInput, setPageInput] = useState('1')
  const [status, setStatus] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [preview, setPreview] = useState<QualityPreview | null>(null)
  const [previewAction, setPreviewAction] = useState<QualityAction | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const load = useCallback(async () => {
    const current = ++generation.current
    try {
      setError(null)
      const params = new URLSearchParams({
        offset: String(offset), limit: '20', ...(source ? { scope: source } : {}),
        ...(status ? { status } : {}), ...(start ? { start } : {}), ...(end ? { end } : {}),
      })
      const data = await backendApi.get<QualityResponse>(`/api/resources/learning/quality?${params}`, { cache: 'no-store' })
      if (current === generation.current) setResult(data)
    } catch (reason) {
      if (current === generation.current) setError(reason instanceof Error ? reason.message : '读取质检记录失败')
    }
  }, [end, offset, reloadToken, source, start, status])

  useEffect(() => {
    void load()
    return () => { generation.current++ }
  }, [load])

  const prepare = async (item: QualityItem, action: QualityAction) => {
    setPending(item.id)
    setActionError(null)
    try {
      const candidate = await backendApi.post<QualityPreview>(
        `/api/resources/learning/quality/${item.id}`, { body: { action },
        })
      setPreview(candidate)
      setPreviewAction(action)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '读取质检操作预览失败')
    } finally {
      setPending(null)
    }
  }

  const confirm = async () => {
    if (!preview || !previewAction) return
    setPending(preview.id)
    setActionError(null)
    try {
      await backendApi.post(`/api/resources/learning/quality/${preview.id}`, {
        body: { action: previewAction, version: preview.version },
      })
      setPreview(null)
      setPreviewAction(null)
      setReloadToken(value => value + 1)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '质检记录维护失败')
    } finally {
      setPending(null)
    }
  }

  const currentPage = Math.floor(offset / 20) + 1
  const totalPages = Math.max(1, Math.ceil((result?.total ?? 0) / 20))
  const applyFilter = () => { setPageInput('1'); setOffset(0) }

  const changePage = (nextPage: number) => {
    if (!Number.isInteger(nextPage) || nextPage < 1 || nextPage > totalPages) return
    setPageInput(String(nextPage))
    setOffset((nextPage - 1) * 20)
  }

  const closePreview = () => {
    setPreview(null)
    setPreviewAction(null)
    setActionError(null)
  }

  return (
    <Card className="flex min-h-0 flex-1 flex-col overflow-auto p-4">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-lg font-semibold">表达质检记录</h2>
          <p className="text-muted-foreground text-sm">模型质量数据与来源记录；不决定表达是否可用于回复。</p></div>
        <div className="flex items-center gap-2">
          <select aria-label="质检来源" value={source} onChange={event => { setSource(event.target.value); setPageInput('1'); setOffset(0) }} className="h-9 rounded border bg-background px-2 text-sm">
            <option value="">全部获准来源</option>
            {(result?.sources ?? []).map(item => <option key={item.key} value={item.key}>{item.label}</option>)}
          </select>
          <Button variant="outline" size="sm" onClick={() => setReloadToken(value => value + 1)}>刷新</Button>
        </div>
      </div>
      <div className="mb-4 flex flex-wrap items-end gap-2">
        <label className="grid gap-1 text-xs">状态
          <select aria-label="质检状态" value={status} onChange={event => { setStatus(event.target.value); setPageInput('1'); setOffset(0) }} className="h-9 rounded border bg-background px-2 text-sm">
            <option value="">全部状态</option><option value="passed">模型通过</option>
            <option value="rejected">模型拒绝</option><option value="error">质检错误</option>
          </select>
        </label>
        <label className="grid gap-1 text-xs">开始日期（UTC）<input aria-label="开始日期（UTC）" type="date" value={start} onChange={event => { setStart(event.target.value); setPageInput('1'); setOffset(0) }} className="h-9 rounded border bg-background px-2 text-sm" /></label>
        <label className="grid gap-1 text-xs">结束日期（UTC）<input aria-label="结束日期（UTC）" type="date" value={end} onChange={event => { setEnd(event.target.value); setPageInput('1'); setOffset(0) }} className="h-9 rounded border bg-background px-2 text-sm" /></label>
        <Button variant="outline" size="sm" onClick={applyFilter}>查询</Button>
        <span role="status" className="text-muted-foreground text-sm">共 {result?.total ?? 0} 条</span>
      </div>
      {error && <p role="alert" className="text-destructive mb-3 text-sm">{error}</p>}
      {actionError && !preview && <p role="alert" className="text-destructive mb-3 text-sm">{actionError}</p>}
      {!result && !error && <p role="status" className="text-muted-foreground">正在读取质检记录。</p>}
      {result && !result.enabled && !error && <p className="text-muted-foreground">当前没有可用的质检记录。</p>}
      <div className="space-y-2">
        {result?.items.map(item => <div key={item.id} className="rounded border p-3 text-sm">
          <div className="flex items-center justify-between gap-2"><strong>{statuses[item.status as keyof typeof statuses] ?? item.status}</strong>
            {item.status === 'rejected' && item.actions.length === 0 && <Button size="xs" disabled={pending !== null} onClick={() => void prepare(item, 'rescued')}>挽回预览</Button>}
            {item.status === 'rejected' && item.actions.length === 1 && item.actions[0][0] === 'rescued' && <Button size="xs" variant="outline" disabled={pending !== null} onClick={() => void prepare(item, 'withdrawn')}>撤回挽回预览</Button>}</div>
          <p>{item.text}</p><p>{item.detail}</p><p className="text-muted-foreground">{reasons[item.reason as keyof typeof reasons] ?? item.reason} · {item.source_label} · {new Date(item.recorded_at).toLocaleString('zh-CN')}</p>
          {item.actions.map(([action, time]) => <p key={`${action}-${time}`} className="text-muted-foreground">{action === 'rescued' ? '已挽回' : '已撤回挽回'} · {new Date(time).toLocaleString('zh-CN')}</p>)}
        </div>)}
      </div>
      {result && <div className="mt-4 flex items-center justify-between border-t pt-3 text-sm">
        <span>共 {result.total} 条 · 第 {result.total === 0 ? 0 : currentPage}/{result.total === 0 ? 0 : totalPages} 页</span>
        <div className="flex items-center gap-2"><Button size="xs" variant="outline" disabled={offset === 0} onClick={() => changePage(currentPage - 1)}>上一页</Button>
          <label className="flex items-center gap-1">跳页<input aria-label="质检记录跳页" type="number" min="1" max={totalPages} value={pageInput} onChange={event => setPageInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') changePage(Number(pageInput)) }} className="h-8 w-16 rounded border bg-background px-2" /></label>
          <Button size="xs" variant="outline" onClick={() => changePage(Number(pageInput))}>跳转</Button>
          <Button size="xs" variant="outline" disabled={offset + 20 >= result.total} onClick={() => changePage(currentPage + 1)}>下一页</Button></div>
      </div>}
      {preview && previewAction && <Dialog open onOpenChange={open => { if (!open && pending === null) closePreview() }}>
        <DialogContent aria-label="确认质检记录维护">
          <DialogHeader>
            <DialogTitle>{previewAction === 'rescued' ? '确认挽回质检条目' : '确认撤回挽回'}</DialogTitle>
            <DialogDescription>此操作只管理当前来源下的学习条目，不改变模型质检记录，也不成为人工批准门槛。</DialogDescription>
          </DialogHeader>
          <p>来源：{result?.sources.find(item => item.key === preview.scope)?.label ?? '当前获准来源'}</p><p>{preview.text}</p><p>{preview.detail}</p>
          {actionError && <p role="alert" className="text-destructive">{actionError}</p>}
          <div className="flex justify-end gap-2"><Button size="sm" variant="outline" disabled={pending !== null} onClick={closePreview}>取消</Button>
            <Button size="sm" disabled={pending !== null} onClick={() => void confirm()}>确认</Button></div>
        </DialogContent></Dialog>}
    </Card>
  )
}
