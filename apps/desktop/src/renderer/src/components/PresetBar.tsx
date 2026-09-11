import { useCallback, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bookmark, Check, Loader2, Save, Trash2 } from 'lucide-react'
import type { Preset } from '@shared/index'
import { cn } from '@/lib/utils'
import { Button, TextInput } from '@/components/ui'

/**
 * 参数预设条。
 *
 * 解决的问题很具体：办公处理里同一套参数会被反复使用（公司公章水印、
 * 扫描件转可搜索 PDF 的固定 dpi……），每次重填一遍既烦又容易填错，
 * 而且不同人填出来的结果还不一致。存成预设之后一键套用。
 *
 * 设计上的两个要点：
 *
 * 1. **预设按动作隔离**。切换工具时只显示该动作的预设 —— 把图片水印的
 *    参数套到 PDF 水印上是没有意义的，混在一起只会让人误选。
 * 2. **保存是"就地更新"**。同一个动作下同名即覆盖。用户调好参数后点
 *    「保存到当前预设」，而不是每次都新建一条。否则很快会攒出一堆
 *    名字不同、内容一样的记录，预设就从"省事"变成"负担"了。
 */
export function PresetBar({
  actionId,
  params,
  onApply
}: {
  actionId: string
  /** 当前表单里的参数，保存预设时写入 */
  params: Record<string, unknown>
  /** 套用预设：把预设参数合并进当前表单 */
  onApply: (params: Record<string, unknown>) => void
}): React.JSX.Element {
  const queryClient = useQueryClient()
  const [activeId, setActiveId] = useState<string | null>(null)
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)

  const presetsQuery = useQuery<Preset[]>({
    queryKey: ['presets', actionId],
    queryFn: () => window.docforge.presets.list(actionId),
    staleTime: 10_000
  })

  const presets = presetsQuery.data ?? []

  /* 切换动作时清掉选中态 —— 否则会显示"已套用 xx"，但那条预设根本不属于当前动作 */
  useEffect(() => {
    setActiveId(null)
    setNaming(false)
    setName('')
    setError(null)
  }, [actionId])

  useEffect(() => {
    if (!flash) return
    const timer = window.setTimeout(() => setFlash(null), 2200)
    return () => window.clearTimeout(timer)
  }, [flash])

  const invalidate = useCallback(async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: ['presets', actionId] })
  }, [queryClient, actionId])

  const saveMutation = useMutation({
    mutationFn: (presetName: string) =>
      window.docforge.presets.save({ action: actionId, name: presetName, params }),
    onSuccess: async (preset) => {
      setActiveId(preset.id)
      setNaming(false)
      setName('')
      setError(null)
      setFlash(`已保存「${preset.name}」`)
      await invalidate()
    },
    onError: (err: Error) => setError(err.message)
  })

  const removeMutation = useMutation({
    mutationFn: (presetId: string) => window.docforge.presets.remove(presetId),
    onSuccess: async (_data, presetId) => {
      if (activeId === presetId) setActiveId(null)
      setFlash('已删除')
      await invalidate()
    },
    onError: (err: Error) => setError(err.message)
  })

  const apply = (preset: Preset): void => {
    onApply(preset.params)
    setActiveId(preset.id)
    setFlash(`已套用「${preset.name}」`)
  }

  /** 已有同名预设时，按钮文案变成"更新"，让用户知道不会新增一条 */
  const duplicate = presets.some((p) => p.name === name.trim() && name.trim().length > 0)
  const activePreset = presets.find((p) => p.id === activeId) ?? null

  return (
    <div className="rounded-xl border border-hairline-2 bg-panel-2 p-2.5">
      <div className="mb-2 flex items-center gap-1.5">
        <Bookmark size={12} className="text-aurora-violet" strokeWidth={1.9} />
        <span className="text-[11.5px] font-medium text-ink-2">参数预设</span>
        {activePreset && (
          <span className="ml-1 rounded-full bg-aurora-cyan/12 px-1.5 py-px text-[10px] text-aurora-cyan">
            {activePreset.name}
          </span>
        )}
        <button
          type="button"
          onClick={() => {
            setNaming((v) => !v)
            setError(null)
          }}
          className="ml-auto flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10.5px] text-ink-4 transition-colors hover:bg-panel-2 hover:text-ink-2"
        >
          <Save size={10} />
          存为预设
        </button>
      </div>

      {presets.length === 0 ? (
        <p className="text-[10.5px] leading-relaxed text-ink-4">
          还没有预设。把参数调好后点「存为预设」，下次就能一键套用 ——
          固定不变的参数存起来，每次只改要变的那一两项。
        </p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {presets.map((preset) => {
            const active = preset.id === activeId
            return (
              <span
                key={preset.id}
                className={cn(
                  'group flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10.5px] transition-colors',
                  active
                    ? 'border-aurora-cyan/45 bg-aurora-cyan/10 text-aurora-cyan'
                    : 'border-hairline-2 text-ink-3 hover:border-hairline hover:text-ink-2'
                )}
              >
                <button
                  type="button"
                  onClick={() => apply(preset)}
                  title={`套用「${preset.name}」`}
                  className="max-w-[140px] truncate"
                >
                  {preset.name}
                </button>
                <button
                  type="button"
                  title={`删除「${preset.name}」`}
                  disabled={removeMutation.isPending}
                  onClick={() => removeMutation.mutate(preset.id)}
                  className="text-ink-4 opacity-0 transition-opacity group-hover:opacity-100 hover:text-err"
                >
                  <Trash2 size={10} />
                </button>
              </span>
            )
          })}
        </div>
      )}

      {naming && (
        <div className="mt-2 space-y-1.5">
          <div className="flex gap-1.5">
            <TextInput
              value={name}
              onChange={setName}
              placeholder="预设名，例如「公司公章」"
            />
            <Button
              size="sm"
              variant="primary"
              className="shrink-0"
              disabled={saveMutation.isPending || name.trim().length === 0}
              onClick={() => saveMutation.mutate(name.trim())}
            >
              {saveMutation.isPending ? (
                <Loader2 size={11} className="animate-spin" />
              ) : (
                <Check size={11} />
              )}
              {duplicate ? '更新' : '保存'}
            </Button>
          </div>
          <p className="text-[10px] leading-relaxed text-ink-4">
            {duplicate
              ? '已有同名预设，保存会覆盖它的参数。'
              : '保存后可在任何任务里一键套用；后续用 --param 指定的项会覆盖预设。'}
          </p>
        </div>
      )}

      {error && <p className="mt-1.5 text-[10.5px] text-err">{error}</p>}
      {flash && !error && <p className="mt-1.5 text-[10.5px] text-ok">{flash}</p>}
    </div>
  )
}
