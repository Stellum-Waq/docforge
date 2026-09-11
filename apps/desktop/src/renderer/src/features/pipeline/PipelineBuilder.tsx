import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircle,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  FileText,
  Loader2,
  Play,
  Plus,
  Trash2,
  Workflow,
  X
} from 'lucide-react'
import type { ActionInfo, WorkspaceFile } from '@shared/index'
import { cn } from '@/lib/utils'
import { Badge, Button, Card, Select, TextInput } from '@/components/ui'
import { SchemaForm, defaultsFromSchema } from '@/components/SchemaForm'
import { useWorkspace } from '@/store/workspace'
import { useJobs } from '@/store/jobs'
import { useCapabilities } from '@/hooks/useCore'

/**
 * 多步骤流水线构建器。
 *
 * 把「上一步的产物直接喂给下一步」这件事变成可以点出来的东西。办公场景里
 * 真正费时间的往往不是单次转换，而是一串固定动作 ——
 * 扫描件 → 可搜索 PDF → 加水印 → 压缩 → 抽取前两页。手动做要来回拖五遍。
 *
 * ## 几个刻意的设计决定
 *
 * 1. **在界面上就做类型链校验**。动作元数据里已经有 `accepts` 和 `outputExt`，
 *    完全够用。把第 3 步的类型不匹配留到任务真正跑起来才报错，等于白等前面
 *    几步（可能包含几分钟的 OCR）。这里实时算出来，问题当场就能看见。
 * 2. **步骤就是动作**，没有任何"流水线专用动作"。内核里注册了新动作，
 *    这里自动就能选到 —— 不存在"功能做了但流水线里用不了"的情况。
 * 3. **参数表单由 `paramsSchema` 生成**，和单动作工作台共用同一个组件，
 *    同一套参数在哪里填都长一样。
 */

interface Step {
  /** 本地唯一 id，用于 React key 与增删排序（动作 id 可能重复出现） */
  uid: string
  action: string
  params: Record<string, unknown>
}

/** 与内核 `docforge/actions/pipeline.py` 里的 STEPS_KEY 保持一致 */
const STEPS_KEY = '__steps'

let uidSeq = 0
const nextUid = (): string => `step-${++uidSeq}`

export function PipelineBuilder(): React.JSX.Element {
  const files = useWorkspace((s) => s.files)
  const addPaths = useWorkspace((s) => s.addPaths)
  const createJob = useJobs((s) => s.create)
  const caps = useCapabilities(true)

  const actionsQuery = useQuery<ActionInfo[]>({
    queryKey: ['actions'],
    queryFn: () => window.docforge.actions.list(),
    staleTime: 5 * 60_000
  })

  const [steps, setSteps] = useState<Step[]>([])
  const [picking, setPicking] = useState(false)
  const [suffix, setSuffix] = useState('')
  const [concurrency, setConcurrency] = useState(2)
  const [outputDir, setOutputDir] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * 可作为流水线步骤的动作。
   *
   * 排除聚合动作（合并/拼接需要一批文件，而流水线是单文件流动的），
   * 也排除流水线自身（不允许嵌套）。
   */
  const candidates = useMemo(
    () => (actionsQuery.data ?? []).filter((a) => !a.aggregate && a.id !== 'pipeline'),
    [actionsQuery.data]
  )

  const byId = useMemo(() => {
    const map = new Map<string, ActionInfo>()
    for (const item of candidates) map.set(item.id, item)
    return map
  }, [candidates])

  const addStep = (actionId: string): void => {
    const info = byId.get(actionId)
    setSteps((current) => [
      ...current,
      { uid: nextUid(), action: actionId, params: info ? defaultsFromSchema(info.paramsSchema) : {} }
    ])
    setPicking(false)
    setError(null)
  }

  const move = (index: number, delta: number): void => {
    setSteps((current) => {
      const target = index + delta
      if (target < 0 || target >= current.length) return current
      const next = [...current]
      const [item] = next.splice(index, 1)
      next.splice(target, 0, item)
      return next
    })
  }

  const updateParams = (uid: string, params: Record<string, unknown>): void => {
    setSteps((current) => current.map((s) => (s.uid === uid ? { ...s, params } : s)))
  }

  /* ------------------------------------------------------------------ */
  /* 类型链校验（与内核 validate_chain 的规则一致）                        */
  /* ------------------------------------------------------------------ */

  const chainIssue = useMemo((): string | null => {
    if (steps.length === 0) return null

    const source = files[0]
    // 没有文件时只做"步骤之间"的检查，不报"源文件不匹配"
    let ext = source ? source.ext : null

    for (let index = 0; index < steps.length; index += 1) {
      const step = steps[index]
      const info = byId.get(step.action)
      if (!info) return `第 ${index + 1} 步的动作已不可用`

      if (ext && info.accepts.length > 0 && !info.accepts.includes(ext)) {
        const previous = index === 0 ? '源文件' : `第 ${index} 步的输出`
        return (
          `第 ${index + 1} 步「${info.label}」不接受 .${ext}：` +
          `${previous}是 .${ext}，这一步需要 ` +
          info.accepts.map((e) => `.${e}`).join('、')
        )
      }

      // outputExt 为空表示"同源"，类型不变
      if (info.outputExt) ext = info.outputExt
    }

    return null
  }, [steps, byId, files])

  /** 最终产物扩展名：用来告诉用户"这批文件会变成什么" */
  const finalExt = useMemo((): string | null => {
    if (steps.length === 0) return null
    const last = byId.get(steps[steps.length - 1].action)
    if (!last) return null
    return last.outputExt ?? files[0]?.ext ?? null
  }, [steps, byId, files])

  const ready = steps.length > 0 && files.length > 0 && !chainIssue

  /* 工作区文件变了（或步骤变了）就把上一次的错误清掉，避免显示过期结论 */
  useEffect(() => setError(null), [steps.length, files.length])

  const run = async (): Promise<void> => {
    if (!ready) return
    setSubmitting(true)
    setError(null)
    try {
      await createJob({
        action: 'pipeline',
        files: files.map((f) => f.path),
        outputDir: outputDir.trim() || caps.data?.defaultOutputDir || undefined,
        // 隐藏参数：内核的 pipeline 动作从这里读步骤清单
        params: {
          [STEPS_KEY]: steps.map((s) => ({ action: s.action, params: s.params }))
        },
        suffix,
        concurrency,
        conflictPolicy: 'rename'
      })
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* ============ 左侧：步骤编排 ============ */}
      <div className="flex min-w-0 flex-1 flex-col p-5">
        <div className="mb-3 flex items-center gap-2.5">
          <Workflow size={15} className="text-aurora-cyan" strokeWidth={1.9} />
          <h2 className="text-[15px] font-semibold text-ink">多步骤流水线</h2>
          <Badge tone="aurora">{steps.length} 步</Badge>
          {finalExt && steps.length > 0 && (
            <span className="flex items-center gap-1 text-[11px] text-ink-4">
              产物 <span className="font-mono text-ink-2">.{finalExt}</span>
            </span>
          )}
        </div>

        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
          {steps.length === 0 ? (
            <div className="glass flex h-full flex-col items-center justify-center gap-2 text-center">
              <Workflow size={26} className="text-aurora-violet" strokeWidth={1.5} />
              <div className="text-[13.5px] font-medium text-ink">还没有步骤</div>
              <p className="max-w-sm text-[11.5px] leading-relaxed text-ink-3">
                按顺序添加动作即可：上一步的产物会自动成为下一步的输入。
                参数在界面上就能看到类型是否对得上，不会跑到第三步才失败。
              </p>
              <Button variant="primary" className="mt-2" onClick={() => setPicking(true)}>
                <Plus size={13} />
                添加第一步
              </Button>
            </div>
          ) : (
            steps.map((step, index) => {
              const info = byId.get(step.action)
              return (
                <div key={step.uid} className="glass-flat rounded-xl p-3">
                  <div className="flex items-center gap-2">
                    <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-aurora-cyan/12 font-mono text-[10.5px] text-aurora-cyan">
                      {index + 1}
                    </span>
                    <span className="text-[12.5px] font-medium text-ink">
                      {info?.label ?? step.action}
                    </span>
                    {info && (
                      <span className="font-mono text-[10px] text-ink-4">{step.action}</span>
                    )}

                    <div className="ml-auto flex items-center gap-1">
                      <IconButton
                        title="上移"
                        disabled={index === 0}
                        onClick={() => move(index, -1)}
                      >
                        <ArrowUp size={11} />
                      </IconButton>
                      <IconButton
                        title="下移"
                        disabled={index === steps.length - 1}
                        onClick={() => move(index, 1)}
                      >
                        <ArrowDown size={11} />
                      </IconButton>
                      <IconButton
                        title="移除这一步"
                        danger
                        onClick={() =>
                          setSteps((current) => current.filter((s) => s.uid !== step.uid))
                        }
                      >
                        <Trash2 size={11} />
                      </IconButton>
                    </div>
                  </div>

                  {info && Object.keys(info.paramsSchema?.properties ?? {}).length > 0 ? (
                    <div className="mt-2.5 border-t border-hairline/40 pt-2.5">
                      <SchemaForm
                        schema={info.paramsSchema}
                        value={step.params}
                        onChange={(next) => updateParams(step.uid, next)}
                      />
                    </div>
                  ) : (
                    <div className="mt-1.5 text-[10.5px] text-ink-4">这一步没有可调参数</div>
                  )}
                </div>
              )
            })
          )}

          {steps.length > 0 && (
            <button
              type="button"
              onClick={() => setPicking(true)}
              className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-hairline/70 py-2.5 text-[11.5px] text-ink-4 transition-colors hover:border-aurora-cyan/45 hover:text-aurora-cyan"
            >
              <Plus size={12} />
              追加一步
            </button>
          )}
        </div>
      </div>

      {/* ============ 右侧：候选文件与执行 ============ */}
      <aside className="w-[330px] shrink-0 overflow-y-auto border-l border-hairline/50 bg-void/40 p-4">
        <div className="space-y-3.5">
          {/* 类型链校验结果 */}
          {chainIssue ? (
            <div className="flex items-start gap-2 rounded-xl border border-err/30 bg-err/8 p-2.5 text-[11px] leading-relaxed text-err">
              <AlertCircle size={13} className="mt-0.5 shrink-0" />
              <span>{chainIssue}</span>
            </div>
          ) : steps.length > 0 && files.length > 0 ? (
            <div className="rounded-xl border border-ok/25 bg-ok/6 p-2.5 text-[11px] text-ok">
              类型链检查通过，可以执行
            </div>
          ) : null}

          <Card title="待处理文件" icon={<FileText size={13} strokeWidth={1.9} />}>
            {files.length === 0 ? (
              <div className="space-y-2">
                <p className="text-[10.5px] leading-relaxed text-ink-4">
                  把文件拖到窗口任意位置即可导入。
                </p>
                <Button
                  size="sm"
                  onClick={() => {
                    void window.docforge.files.pickFiles({ multi: true }).then((paths) => {
                      if (paths.length) void addPaths(paths)
                    })
                  }}
                >
                  选择文件
                </Button>
              </div>
            ) : (
              <div className="max-h-[200px] space-y-1 overflow-y-auto">
                {files.slice(0, 40).map((file) => (
                  <div key={file.id} className="flex items-center gap-2 text-[11px]">
                    <FileText size={11} className="shrink-0 text-ink-4" />
                    <span className="truncate text-ink-2" title={file.path}>
                      {file.name}
                    </span>
                    <span className="ml-auto shrink-0 font-mono text-[10px] text-ink-4">
                      .{file.ext}
                    </span>
                  </div>
                ))}
                {files.length > 40 && (
                  <div className="text-[10.5px] text-ink-4">还有 {files.length - 40} 个…</div>
                )}
              </div>
            )}
          </Card>

          {/* 步骤概览：一串箭头，一眼看清这条流水线在干什么 */}
          {steps.length > 0 && (
            <Card title="执行顺序" icon={<Workflow size={13} strokeWidth={1.9} />}>
              <div className="flex flex-wrap items-center gap-1">
                {steps.map((step, index) => (
                  <span key={step.uid} className="flex items-center gap-1">
                    {index > 0 && <ArrowRight size={10} className="text-ink-4" />}
                    <span className="rounded-md bg-panel-2/70 px-1.5 py-0.5 text-[10px] text-ink-2">
                      {byId.get(step.action)?.label ?? step.action}
                    </span>
                  </span>
                ))}
              </div>
            </Card>
          )}

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

              <div className="space-y-1.5">
                <span className="text-[11.5px] font-medium text-ink-2">并发数</span>
                <Select
                  value={String(concurrency)}
                  onChange={(v) => setConcurrency(Number(v))}
                  options={[1, 2, 3, 4, 6, 8].map((n) => ({
                    value: String(n),
                    label: `${n} 个文件同时跑流水线`
                  }))}
                />
              </div>
            </div>
          </Card>

          {error && (
            <div className="flex items-start gap-2 rounded-xl border border-err/30 bg-err/8 p-2.5 text-[11px] text-err">
              <AlertCircle size={13} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <Button
            variant="primary"
            size="lg"
            className="w-full"
            disabled={!ready || submitting}
            onClick={() => void run()}
          >
            {submitting ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
            对 {files.length} 个文件执行 {steps.length} 步
          </Button>

          <div className="rounded-lg border border-hairline/60 bg-panel-2/40 px-3 py-2.5 text-[10.5px] leading-relaxed text-ink-4">
            中间产物写在临时目录并在结束后自动清理，**输出目录里只会出现最终结果**；
            原文件不会被修改。
          </div>
        </div>
      </aside>

      {/* ============ 动作选择浮层 ============ */}
      {picking && (
        <div
          className="absolute inset-0 z-40 flex items-start justify-center bg-abyss/70 pt-24 backdrop-blur-sm"
          onClick={() => setPicking(false)}
        >
          <div
            className="glass max-h-[70%] w-[520px] overflow-hidden"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-center gap-2 border-b border-hairline/50 px-4 py-2.5">
              <Plus size={13} className="text-aurora-cyan" />
              <span className="text-[12.5px] font-semibold text-ink">选择一步动作</span>
              <button
                type="button"
                onClick={() => setPicking(false)}
                className="ml-auto text-ink-4 transition-colors hover:text-ink-2"
              >
                <X size={14} />
              </button>
            </div>

            <div className="max-h-[420px] overflow-y-auto p-2.5">
              {candidates.map((info) => (
                <button
                  key={info.id}
                  type="button"
                  onClick={() => addStep(info.id)}
                  className="flex w-full flex-col gap-0.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-panel-2/70"
                >
                  <span className="flex items-center gap-2">
                    <span className="text-[12px] font-medium text-ink">{info.label}</span>
                    <span className="font-mono text-[10px] text-ink-4">{info.id}</span>
                    {info.outputExt && (
                      <span className="ml-auto font-mono text-[10px] text-aurora-cyan">
                        → .{info.outputExt}
                      </span>
                    )}
                  </span>
                  <span className="text-[10.5px] leading-relaxed text-ink-4">
                    {info.description}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function IconButton({
  children,
  title,
  onClick,
  disabled,
  danger
}: {
  children: React.ReactNode
  title: string
  onClick: () => void
  disabled?: boolean
  danger?: boolean
}): React.JSX.Element {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'flex h-6 w-6 items-center justify-center rounded-md border border-transparent text-ink-4 transition-colors',
        disabled
          ? 'opacity-30'
          : danger
            ? 'hover:border-err/35 hover:bg-err/10 hover:text-err'
            : 'hover:border-hairline hover:bg-panel-2/70 hover:text-ink-2'
      )}
    >
      {children}
    </button>
  )
}
