import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Database, Trash2 } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useToast } from '@/hooks/use-toast'
import { deleteExpressionVectorSpace, getExpressionVectorBuildProgress, getExpressionVectorSpaces } from '@/lib/expression-api'

const SPACE_STATES = { active: '使用中', syncing: '同步中', saved: '未使用' }
const VECTOR_REFRESH_INTERVAL_MS = 30_000
const SYNC_STATES = {
  syncing: '正在同步表达向量',
  failed: '同步失败',
  ready: '当前补建任务已结束',
  empty: '尚未建立向量索引',
}
const BUILD_STATES = {
  disabled: '构建已停用', unconfigured: '未配置向量模型', waiting_profile: '等待本机向量配置',
  pending: '等待补建', running: '正在补建', completed: '补建已完成',
}

export function ExpressionVectorSpaces() {
  const [open, setOpen] = useState(false)
  const { toast } = useToast()
  const queryClient = useQueryClient()
  const spaces = useQuery({
    queryKey: ['expression', 'vector-spaces'],
    queryFn: getExpressionVectorSpaces,
    enabled: open,
    refetchInterval: open ? VECTOR_REFRESH_INTERVAL_MS : false,
  })
  const progress = useQuery({
    queryKey: ['expression', 'vector-build-progress'],
    queryFn: getExpressionVectorBuildProgress,
    enabled: open,
    refetchInterval: open ? VECTOR_REFRESH_INTERVAL_MS : false,
  })
  const deletion = useMutation({
    mutationFn: deleteExpressionVectorSpace,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['expression', 'vector-spaces'] }),
        queryClient.invalidateQueries({ queryKey: ['expression', 'clusters'] }),
      ])
      toast({ title: '已删除未使用的向量库' })
    },
    onError: (error) => toast({ title: '删除失败', description: error.message, variant: 'destructive' }),
  })

  return (
    <>
      <Button variant="outline" size="sm" className="h-8 gap-2" onClick={() => setOpen(true)}>
        <Database className="h-4 w-4" />
        向量库
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-5xl">
          <DialogHeader>
            <DialogTitle>表达向量库</DialogTitle>
          </DialogHeader>
          <Button
            variant="outline"
            size="sm"
            className="w-fit"
            disabled={spaces.isFetching || progress.isFetching}
            onClick={() => {
              void spaces.refetch()
              void progress.refetch()
            }}
          >
            刷新
          </Button>
          {spaces.data?.state && (
            <p className="text-sm text-muted-foreground">{SYNC_STATES[spaces.data.state]}</p>
          )}
          {progress.data && (
            <p className="text-sm text-muted-foreground">
              {BUILD_STATES[progress.data.status]} · {progress.data.completed}/{progress.data.total} · {progress.data.percent}%
            </p>
          )}
          {progress.error && <p role="alert" className="text-sm text-destructive">{progress.error.message}</p>}
          {(spaces.error || spaces.data?.last_error) && (
            <p className="break-words text-sm text-destructive">
              {spaces.error?.message || spaces.data?.last_error}
            </p>
          )}
          <div className="max-h-[60vh] overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>模型</TableHead>
                  <TableHead>维度</TableHead>
                  <TableHead>表达数量</TableHead>
                  <TableHead>向量大小</TableHead>
                  <TableHead>最后使用</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {spaces.data?.items.map((space) => (
                  <TableRow key={space.space_id}>
                    <TableCell className="max-w-64">
                      <div className="truncate" title={space.embedding_fingerprint.model_identifier}>
                        {space.embedding_fingerprint.model_identifier || space.embedding_fingerprint.model || '-'}
                      </div>
                      <div className="truncate text-xs text-muted-foreground" title={space.embedding_fingerprint.base_url}>
                        {space.embedding_fingerprint.provider || '-'}
                        {space.embedding_fingerprint.base_url && ` · ${space.embedding_fingerprint.base_url}`}
                      </div>
                    </TableCell>
                    <TableCell>{space.embedding_fingerprint.dimension || '-'}</TableCell>
                    <TableCell>{space.vector_count}</TableCell>
                    <TableCell>{(space.size_bytes / 1024 / 1024).toFixed(2)} MB</TableCell>
                    <TableCell className="whitespace-nowrap">
                      {space.last_used_at ? new Date(space.last_used_at * 1000).toLocaleString() : '-'}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">{SPACE_STATES[space.state]}</TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label="删除未使用的向量库"
                        title={space.can_delete ? undefined : '当前向量空间不可删除'}
                        disabled={!space.can_delete || deletion.isPending}
                        onClick={() => deletion.mutate(space.space_id)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
                {!spaces.data?.items.length && (
                  <TableRow>
                    <TableCell colSpan={7} className="py-6 text-center text-muted-foreground">
                      {spaces.isPending ? '正在读取向量库' : spaces.error ? '向量库读取失败' : '暂无向量库'}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
