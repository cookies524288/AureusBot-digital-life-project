import { useId } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

import { cn } from '@/lib/utils'
import { Button } from './button'
import { Input } from './input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './select'

interface DataPaginationProps {
  total: number
  page: number
  pageSize: number
  onPageChange: (page: number) => void
  onPageSizeChange?: (pageSize: number) => void
  pageSizeOptions?: number[]
  disabled?: boolean
  className?: string
}

/** 保留首尾页和当前位置附近的页码；省略的页码通过跳转框访问。 */
function getPageItems(page: number, pages: number): (number | 'gap')[] {
  if (pages <= 5) return Array.from({ length: pages }, (_, index) => index + 1)

  const start = Math.max(1, Math.min(page - 1, pages - 2))
  const visible = [...new Set([1, start, start + 1, start + 2, pages])].sort((a, b) => a - b)
  const items: (number | 'gap')[] = []
  for (const current of visible) {
    const previous = items[items.length - 1]
    if (typeof previous === 'number' && current - previous > 1) {
      items.push(current - previous === 2 ? previous + 1 : 'gap')
    }
    items.push(current)
  }
  return items
}

function PageJump({
  page,
  pages,
  disabled,
  onPageChange,
}: {
  page: number
  pages: number
  disabled: boolean
  onPageChange: (page: number) => void
}) {
  return (
    <form
      className="flex items-center gap-1"
      onSubmit={(event) => {
        event.preventDefault()
        const target = Number(new FormData(event.currentTarget).get('page'))
        if (!disabled && Number.isInteger(target) && target >= 1 && target <= pages) onPageChange(target)
      }}
    >
      <Input
        key={page}
        name="page"
        aria-label="跳转页码"
        type="number"
        controlSize="xs"
        min={1}
        max={pages}
        step={1}
        required
        defaultValue={page}
        disabled={disabled}
        className="w-16"
      />
      <Button type="submit" variant="outline" size="xs" disabled={disabled}>跳转</Button>
    </form>
  )
}

function DataPagination({
  total,
  page,
  pageSize,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [10, 20, 50, 100],
  disabled = false,
  className,
}: DataPaginationProps) {
  const sizeId = useId()
  const totalPages = Math.ceil(total / pageSize)
  const currentPage = Math.max(1, Math.min(page, totalPages))
  const unavailable = disabled || total === 0
  const sizes = [...new Set([...pageSizeOptions, pageSize])].sort((a, b) => a - b)

  return (
    <nav
      aria-label="数据分页"
      data-dashboard-data-pagination="true"
      className={cn(
        'flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 px-3 py-2 text-xs',
        className
      )}
    >
      <span className="text-muted-foreground whitespace-nowrap" aria-live="polite">
        {total.toLocaleString()} 条 · 第 {total === 0 ? 0 : currentPage}/{totalPages} 页
      </span>
      <div className="flex flex-wrap items-center gap-2">
        {onPageSizeChange && (
          <div className="flex items-center gap-1.5">
            <label htmlFor={sizeId} className="text-muted-foreground whitespace-nowrap">
              每页
            </label>
            <Select
              value={String(pageSize)}
              onValueChange={(value) => onPageSizeChange(Number(value))}
              disabled={disabled}
            >
              <SelectTrigger id={sizeId} controlSize="xs" className="w-16 px-2 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {sizes.map((size) => (
                  <SelectItem key={size} value={String(size)}>
                    {size}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-1">
          <Button
            type="button"
            variant="outline"
            size="icon-xs"
            aria-label="上一页"
            disabled={unavailable || currentPage <= 1}
            onClick={() => onPageChange(currentPage - 1)}
          >
            <ChevronLeft />
          </Button>
          {getPageItems(currentPage, Math.max(1, totalPages)).map((item, index) =>
            item === 'gap' ? <span key={`gap-${index}`} aria-hidden="true">…</span> : (
              <Button
                key={item}
                type="button"
                variant="outline"
                size="xs"
                className="min-w-[var(--control-height-xs)]"
                aria-label={`第 ${item} 页`}
                aria-current={item === currentPage ? 'page' : undefined}
                aria-pressed={item === currentPage}
                disabled={unavailable}
                onClick={() => onPageChange(item)}
              >
                {item}
              </Button>
            )
          )}
          <Button
            type="button"
            variant="outline"
            size="icon-xs"
            aria-label="下一页"
            disabled={unavailable || currentPage >= totalPages}
            onClick={() => onPageChange(currentPage + 1)}
          >
            <ChevronRight />
          </Button>
          <PageJump page={currentPage} pages={Math.max(1, totalPages)}
            disabled={unavailable} onPageChange={onPageChange} />
        </div>
      </div>
    </nav>
  )
}

export { DataPagination, type DataPaginationProps }
