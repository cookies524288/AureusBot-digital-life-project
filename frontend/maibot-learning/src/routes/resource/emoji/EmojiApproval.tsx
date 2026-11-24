import { useMutation, useQueryClient } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { reviewEmoji } from '@/lib/emoji-api'
import type { Emoji, EmojiVerdict } from '@/types/emoji'

const labels = { unreviewed: '未审', approved: '已通过', rejected: '已拒绝' }

export function EmojiApproval({ emoji }: { emoji: Emoji }) {
  const client = useQueryClient()
  const mutation = useMutation({
    mutationFn: (verdict: EmojiVerdict) => reviewEmoji(emoji.id, verdict),
    onSuccess: () => client.invalidateQueries({ queryKey: ['emoji'] }),
  })
  const status = labels[emoji.review ?? 'unreviewed']
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button type="button" size="xs" variant="outline" className="absolute right-1 bottom-1 z-10"
          aria-label={`表情审批 · ${status} · ${emoji.description || emoji.emoji_hash}`}>
          {status}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-44 space-y-2 p-2">
        <p className="text-xs">审批独立于登记与禁用</p>
        <div className="flex gap-2">
          <Button size="xs" disabled={mutation.isPending} onClick={() => mutation.mutate('approved')}>通过</Button>
          <Button size="xs" variant="outline" disabled={mutation.isPending} onClick={() => mutation.mutate('rejected')}>拒绝</Button>
        </div>
        {mutation.error && <p role="alert" className="text-destructive text-xs">{mutation.error.message}</p>}
      </PopoverContent>
    </Popover>
  )
}
