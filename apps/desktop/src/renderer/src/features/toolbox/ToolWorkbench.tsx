import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Loader2,
  Play,
  Wrench
} from 'lucide-react'
import type { ActionInfo, WorkspaceFile } from '@shared/index'
import { cn } from '@/lib/utils'
import { Badge, Button, Card, ProgressBar, Select, TextInput } from '@/components/ui'
import { SchemaForm, defaultsFromSchema } from '@/components/SchemaForm'
import { PresetBar } from '@/components/PresetBar'
import { useWorkspace } from '@/store/workspace'
import { useJobs } from '@/store/jobs'
import { useCapabilities } from '@/hooks/useCore'

/**
 * 通用工具工作台。
 *
 * PDF 工具箱与文档转换页的结构完全一致：左侧工具列表、右侧参数表单、
 * 候选文件与输出设置。与其写两遍，不如抽成一个组件 ——
 * 参数表单本来就已经由后端 `paramsSchema` 生成，这里只需要再统一
 * 文件筛选、输出设置与提交逻辑。
 *
 * 关键点：**聚合动作从动作元数据读取**（`ActionInfo.aggregate`），
 * 而不是在前端硬编码"哪个 id 是合并"。后端新增一个聚合动作时，
 * 界面会自动给出正确的交互。
 */

export interface ToolDefinition {
  id: string
  icon: React.ReactNode
  blurb: string
}

interface ToolWorkbenchProps {
  title: string
  icon: React.ReactNode
  tools: ToolDefinition[]
  /** 该工作台接受哪些工作区文件 */
  acceptFilter: (file: WorkspaceFile) => boolean
  /** 没有候选文件时的说明 */
  emptyHint: string
  /** 文件类型名称，用于文案（例如「PDF」「文档」） */
  fileNoun: string
}

export function ToolWorkbench({
  title,
  icon,
  tools,
  acceptFilter,
  emptyHint,
  fileNoun
}: ToolWorkbenchProps): React.JSX.Element {
  const files = useWorkspace((s) => s.files)
  const addPaths = useWorkspace((s) => s.addPaths)
  const createJob = useJobs((s) => s.create)
  const caps = useCapabilities(true)

  const actionsQuery = useQuery<ActionInfo[]>({
    queryKey: ['actions'],
    queryFn: () => window.docforge.actions.list(),
    staleTime: 5 * 60_000
  })

  const [activeId, setActiveId] = useState<string>(tools[0]?.id ?? '')
  const [params, setParams] = useState<Record<string, unknown>>({})
  const [initializedFor, setInitializedFor] = useState<string | null>(null)
  const [suffix, setSuffix] = useState('')
  const [concurrency, setConcurrency] = useState(4)
  const [outputDir, setOutputDir] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const action = useMemo(
    () => actionsQuery.data?.find((item) => item.id === activeId),
    [actionsQuery.data, activeId]
  )

  useEffect(() => {
    if (!action || initializedFor === action.id) return
    setParams(defaultsFromSchema(action.paramsSchema))
    setInitializedFor(action.id)
  }, [action, initializedFor])

  /**
   * 候选文件。
   *
   * 关键：**按当前选中动作的 accepts 过滤**，而不是按工作台的整体范围。
   * 「文档转换」里既有"文档转 PDF"（不接受 PDF 输入）又有"PDF 转 Word"
   * （只接受 PDF），如果列表不跟着工具变，用户会看到一堆"该任务不接受 xxx 格式"
   * 的跳过记录 —— 功能是对的，但体验像是在报错。
   */
  const candidates = useMemo(() => {
    if (action) {
      const accepted = new Set(action.accepts)
      return files.filter((file) => accepted.has(file.ext))
    }
    return files.filter(acceptFilter)
  }, [files, action, acceptFilter])

  const isAggregate = action?.aggregate ?? false
  const enoughFiles = isAggregate ? candidates.length >= 2 : candidates.length >= 1

  const start = async (): Promise<void> => {
    if (!action || !enoughFiles) return
    setSubmitting(true)
    try {
      await createJob({
        action: action.id,
        files: candidates.map((f) => f.path),
        outputDir: outputDir.trim() || caps.data?.defaultOutputDir || undefined,
        params,
        suffix,
        // 聚合动作只有一个任务，并发数没有意义
        concurrency: isAggregate ? 1 : concurrency
      })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* ============ 左侧：工具列表 ============ */}
      <div className="flex w-[248px] shrink-0 flex-col border-r border-hairline bg-abyss">
        <div className="flex items-center gap-2 px-3.5 py-3">
          <span className="text-aurora-cyan">{icon}</span>
          <span className="text-[13px] font-semibold text-ink">{title}</span>
        </div>

        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2.5 pb-3">
          {tools.map((tool) => {
            const info = actionsQuery.data?.find((a) => a.id === tool.id)
            const active = tool.id === activeId
            const available = !actionsQuery.data || Boolean(info)
            return (
              <button
                key={tool.id}
                type="button"
                disabled={!available}
                onClick={() => setActiveId(tool.id)}
                className={cn(
                  'group flex w-full items-start gap-2.5 rounded-lg border px-2.5 py-2 text-left transition-colors',
                  active
                    ? 'border-aurora-cyan/35 bg-aurora-cyan/10'
                    : 'border-transparent hover:border-hairline-2 hover:bg-panel-2',
                  !available && 'opacity-40'
                )}
              >
                <span className={cn('mt-0.5 shrink-0', active ? 'text-aurora-cyan' : 'text-ink-4')}>
                  {tool.icon}
                </span>
                <span className="min-w-0 flex-1">
                  <span className={cn('block text-[12px] font-medium', active ? 'text-ink' : 'text-ink-2')}>
                    {info?.label ?? tool.id}
                  </span>
                  <span className="mt-0.5 block text-[10px] leading-relaxed text-ink-4">{tool.blurb}</span>
                </span>
              </button>
            )
          })}
        </div>

        <div className="border-t border-hairline px-3.5 py-2.5 text-[10px] leading-relaxed text-ink-4">
          工作区共 <span className="font-mono text-ink-2">{candidates.length}</span> 个{fileNoun}
        </div>
      </div>

      {/* ============ 右侧 ============ */}
      <div className="flex min-w-0 flex-1 flex-col p-5">
        {!action ? (
          <div className="flex flex-1 items-center justify-center text-[12px] text-ink-3">
            {actionsQuery.isLoading ? '正在读取功能定义…' : '内核未就绪'}
          </div>
        ) : candidates.length === 0 ? (
          <div className="flex flex-1 items-center justify-center">
            <div className="glass max-w-md px-8 py-9 text-center">
              <FileText size={24} className="mx-auto mb-3 text-aurora-cyan" strokeWidth={1.6} />
              <h2 className="text-[15px] font-semibold text-ink">
                还没有可用于「{action.label}」的文件
              </h2>
              <p className="mt-1 text-[12px] text-ink-3">
                {emptyHint}
                {action.accepts.length > 0 && (
                  <span className="mt-1 block text-ink-4">
                    该功能接受：{action.accepts.slice(0, 10).map((ext) => `.${ext}`).join(' ')}
                    {action.accepts.length > 10 ? ' …' : ''}
                  </span>
                )}
              </p>
              <Button
                variant="primary"
                className="mt-4"
                onClick={() => {
                  void window.docforge.files.pickFiles({ multi: true }).then((paths) => {
                    if (paths.length) void addPaths(paths)
                  })
                }}
              >
                选择文件
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="mb-4">
              <h2 className="text-[16px] font-semibold text-ink">{action.label}</h2>
              <p className="mt-1 text-[12px] text-ink-3">{action.description}</p>
              <div className="mt-2 flex items-center gap-2">
                <Badge tone={isAggregate ? 'aurora' : 'idle'}>
                  {isAggregate ? '合并模式（整批一个结果）' : '逐个文件处理'}
                </Badge>
                {isAggregate && candidates.length < 2 && <Badge tone="warn">至少需要 2 个文件</Badge>}
                {!isAggregate && <Badge tone="idle">{candidates.length} 个待处理</Badge>}
              </div>
            </div>

            <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
              <div className="min-h-0 overflow-y-auto pr-1">
                <div className="space-y-3">
                  <PresetBar
                    actionId={action.id}
                    params={params}
                    onApply={(presetParams) => {
                      // 合并而不是整体替换：预设里没写的参数沿用当前表单值，
                      // 这样"存预设时用默认值的那几项"不会因为套用而丢失
                      setParams((current) => ({ ...current, ...presetParams }))
                    }}
                  />
                  <Card title="参数" icon={<Wrench size={13} strokeWidth={1.9} />}>
                    <SchemaForm schema={action.paramsSchema} value={params} onChange={setParams} />
                  </Card>
                </div>
              </div>

              <div className="min-h-0 space-y-3.5 overflow-y-auto">
                <Card title="输出" icon={<FileText size={13} strokeWidth={1.9} />}>
                  <div className="space-y-3">
                    <div className="space-y-1.5">
                      <span className="text-[11.5px] font-medium text-ink-2">输出目录</span>
                      <div className="flex gap-2">
                        <TextInput
                          value={outputDir}
                          onChange={setOutputDir}
                          placeholder={caps.data?.defaultOutputDir ?? '默认输出目录'}
                          mono
                        />
                        <Button
                          size="sm"
                          className="shrink-0"
                          onClick={() => {
                            void window.docforge.files.pickDirectory().then((dir) => {
                              if (dir) setOutputDir(dir)
                            })
                          }}
                        >
                          选择
                        </Button>
                      </div>
                    </div>

                    <div className="space-y-1.5">
                      <span className="text-[11.5px] font-medium text-ink-2">文件名后缀</span>
                      <TextInput value={suffix} onChange={setSuffix} placeholder="留空则不加" />
                    </div>

                    {!isAggregate && (
                      <div>
                        <div className="mb-1.5 flex items-baseline justify-between">
                          <span className="text-[11.5px] font-medium text-ink-2">并发数</span>
                          <span className="font-mono text-[11px] text-ink-4">{concurrency}</span>
                        </div>
                        <ProgressBar value={(concurrency / 16) * 100} />
                        <Select
                          value={String(concurrency)}
                          onChange={(v) => setConcurrency(Number(v))}
                          options={[1, 2, 3, 4, 6, 8, 12, 16].map((n) => ({
                            value: String(n),
                            label: `${n} 个文件同时处理`
                          }))}
                        />
                      </div>
                    )}
                  </div>
                </Card>

                <Card title={`待处理${fileNoun}`} icon={<FileText size={13} strokeWidth={1.9} />}>
                  <div className="max-h-[220px] space-y-1 overflow-y-auto">
                    {candidates.slice(0, 40).map((file) => (
                      <div key={file.id} className="flex items-center gap-2 text-[11px]">
                        <FileText size={11} className="shrink-0 text-ink-4" />
                        <span className="truncate text-ink-2" title={file.path}>
                          {file.name}
                        </span>
                      </div>
                    ))}
                    {candidates.length > 40 && (
                      <div className="text-[10.5px] text-ink-4">还有 {candidates.length - 40} 个…</div>
                    )}
                  </div>
                </Card>

                <Button
                  variant="primary"
                  size="lg"
                  className="w-full"
                  disabled={submitting || !enoughFiles}
                  onClick={() => void start()}
                >
                  {submitting ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
                  {isAggregate ? `合并 ${candidates.length} 个文件` : `处理 ${candidates.length} 个文件`}
                </Button>

                <div className="rounded-lg border border-hairline-2 bg-panel-2 px-3 py-2.5 text-[10.5px] leading-relaxed text-ink-4">
                  结果会保存到输出目录，**原文件不会被修改**；同名文件自动重命名而不覆盖。
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

/** 供工具列表使用的小图标徽标 */
export function ToolBadge({
  ok,
  label
}: {
  ok: boolean
  label: string
}): React.JSX.Element {
  return (
    <span
      className={cn(
        'flex items-center gap-1 text-[10.5px]',
        ok ? 'text-ok' : 'text-warn'
      )}
    >
      {ok ? <CheckCircle2 size={11} /> : <AlertCircle size={11} />}
      {label}
    </span>
  )
}
