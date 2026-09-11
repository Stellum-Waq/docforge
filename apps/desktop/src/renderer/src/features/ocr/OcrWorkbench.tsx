import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'motion/react'
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  Cpu,
  FileSpreadsheet,
  FlaskConical,
  Info,
  Loader2,
  Play,
  ScanText,
  ShieldCheck,
  Sparkles,
  Table2,
  Type
} from 'lucide-react'
import type { ActionInfo, OcrEngineReport, OcrTestResult } from '@shared/index'
import { cn } from '@/lib/utils'
import { Badge, Button, Card, ProgressBar, Select, Switch, TextInput } from '@/components/ui'
import { SchemaForm, defaultsFromSchema } from '@/components/SchemaForm'
import { PresetBar } from '@/components/PresetBar'
import { useWorkspace } from '@/store/workspace'
import { useJobs } from '@/store/jobs'
import { useCapabilities } from '@/hooks/useCore'

type OcrActionId = 'image.ocr' | 'image.to_excel'

interface OcrWorkbenchProps {
  actionId: OcrActionId
}

/**
 * OCR 工作台（图片提取文字 / 图片转 Excel 共用）。
 *
 * 核心体验设计是「**先试一张，满意再批量**」：OCR 的引擎与参数对结果影响很大，
 * 如果只能批量跑完才知道效果，用户会浪费大量时间，云端模式下还会白白花钱。
 * 因此把单张试识别放在最显眼的位置，结果实时回显。
 */
export function OcrWorkbench({ actionId }: OcrWorkbenchProps): React.JSX.Element {
  const files = useWorkspace((s) => s.files)
  const addPaths = useWorkspace((s) => s.addPaths)
  const createJob = useJobs((s) => s.create)
  const caps = useCapabilities(true)

  const actionsQuery = useQuery<ActionInfo[]>({
    queryKey: ['actions'],
    queryFn: () => window.docforge.actions.list(),
    staleTime: 5 * 60_000
  })

  const engineQuery = useQuery<OcrEngineReport>({
    queryKey: ['ocr', 'engines'],
    queryFn: () => window.docforge.settings.ocrEngines(),
    staleTime: 30_000
  })

  const action = useMemo(
    () => actionsQuery.data?.find((item) => item.id === actionId),
    [actionsQuery.data, actionId]
  )

  const [params, setParams] = useState<Record<string, unknown>>({})
  const [initializedFor, setInitializedFor] = useState<string | null>(null)

  /* Schema 到达后用默认值初始化一次。之后不再覆盖用户的修改。 */
  useEffect(() => {
    if (!action || initializedFor === action.id) return
    setParams(defaultsFromSchema(action.paramsSchema))
    setInitializedFor(action.id)
  }, [action, initializedFor])

  const [suffix, setSuffix] = useState('')
  const [concurrency, setConcurrency] = useState(4)
  const [outputDir, setOutputDir] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<OcrTestResult | null>(null)
  const [testError, setTestError] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  /** 能处理的文件 */
  const candidates = useMemo(() => {
    if (!action) return []
    const accepted = new Set(action.accepts)
    return files.filter((f) => accepted.has(f.ext))
  }, [files, action])

  const sample = useMemo(
    () => candidates.find((f) => f.id === selectedId) ?? candidates[0] ?? null,
    [candidates, selectedId]
  )

  /* ---------------- 原图缩略图 ---------------- */

  const [thumbnail, setThumbnail] = useState<string | null>(null)
  useEffect(() => {
    if (!sample) {
      setThumbnail(null)
      return
    }
    let alive = true
    void window.docforge.preview
      .thumbnail({ filePath: sample.path, maxWidth: 460 })
      .then((url) => {
        if (alive) setThumbnail(url)
      })
      .catch(() => {
        if (alive) setThumbnail(null)
      })
    return () => {
      alive = false
    }
  }, [sample])

  /* ---------------- 试识别 ---------------- */

  const enginePref = String(params.engine ?? 'local')

  const runTest = useCallback(async () => {
    if (!sample || !action) return
    setTesting(true)
    setTestError(null)
    try {
      const result = await window.docforge.preview.ocrTest({
        filePath: sample.path,
        engine: enginePref as 'local' | 'cloud' | 'auto',
        // 转 Excel 时云端用 table 模式拿结构化表格，本地没有表格模式用 text
        mode:
          actionId === 'image.to_excel'
            ? enginePref === 'local'
              ? 'text'
              : 'table'
            : ((params.mode as 'text' | 'layout' | 'formula') ?? 'text'),
        tiling: (params.tiling as 'auto' | 'off' | 'always') ?? 'auto',
        detail: String(params.detail ?? 'original'),
        language: String(params.language ?? 'auto')
      })
      setTestResult(result)
    } catch (err) {
      setTestError((err as Error).message)
      setTestResult(null)
    } finally {
      setTesting(false)
    }
  }, [sample, action, actionId, enginePref, params.mode, params.tiling, params.detail, params.language])

  /* 参数变了就把上一次的试识别结果作废，避免"看着旧结果调新参数" */
  const paramsFingerprint = JSON.stringify({ enginePref, ...params })
  const lastFingerprint = useRef(paramsFingerprint)
  useEffect(() => {
    if (lastFingerprint.current !== paramsFingerprint) {
      lastFingerprint.current = paramsFingerprint
      setTestResult(null)
    }
  }, [paramsFingerprint])

  /* ---------------- 提交 ---------------- */

  const start = async (): Promise<void> => {
    if (candidates.length === 0) return
    setSubmitting(true)
    try {
      await createJob({
        action: actionId,
        files: candidates.map((f) => f.path),
        outputDir: outputDir.trim() || caps.data?.defaultOutputDir || undefined,
        params,
        suffix,
        concurrency,
        conflictPolicy: 'rename'
      })
    } finally {
      setSubmitting(false)
    }
  }

  /* ---------------- 渲染 ---------------- */

  if (!action) {
    return (
      <div className="flex flex-1 items-center justify-center text-[12px] text-ink-3">
        {actionsQuery.isLoading ? '正在读取功能定义…' : '内核未就绪，无法读取功能定义'}
      </div>
    )
  }

  if (candidates.length === 0) {
    return (
      <EmptyState
        icon={actionId === 'image.to_excel' ? <Table2 size={24} /> : <ScanText size={24} />}
        title={actionId === 'image.to_excel' ? '还没有可用于表格识别的图片' : '还没有可用于识别的图片'}
        hint="把图片拖到窗口任意位置，或点击下面的按钮选择"
        onPick={() => {
          void window.docforge.files.pickFiles({ multi: true }).then((paths) => {
            if (paths.length) void addPaths(paths)
          })
        }}
      />
    )
  }

  const engines = engineQuery.data?.engines ?? []
  const localEngine = engines.find((e) => e.offline)
  const cloudEngine = engines.find((e) => !e.offline)

  return (
    <div className="flex min-h-0 flex-1">
      {/* ============ 左侧：图片 + 试识别结果 ============ */}
      <div className="flex min-w-0 flex-1 flex-col p-5">
        <div className="mb-3 flex items-center gap-2.5">
          {actionId === 'image.to_excel' ? (
            <FileSpreadsheet size={15} className="text-aurora-cyan" strokeWidth={1.9} />
          ) : (
            <ScanText size={15} className="text-aurora-cyan" strokeWidth={1.9} />
          )}
          <h2 className="text-[15px] font-semibold text-ink">{action.label}</h2>
          <Badge tone="aurora">{candidates.length} 张待处理</Badge>
          {testing && (
            <span className="flex items-center gap-1.5 text-[11px] text-ink-4">
              <Loader2 size={11} className="animate-spin" />
              识别中
            </span>
          )}
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] gap-3">
          {/* 原图 */}
          <div className="glass relative flex items-center justify-center overflow-hidden p-3">
            <div className="grid-floor pointer-events-none absolute inset-0 opacity-25" />
            {thumbnail ? (
              <img
                src={thumbnail}
                alt={sample?.name ?? '预览'}
                className="relative max-h-full max-w-full rounded-lg object-contain shadow-xl"
                draggable={false}
              />
            ) : (
              <div className="relative text-[11.5px] text-ink-4">无法预览该图片</div>
            )}

            <div className="absolute inset-x-0 bottom-2 text-center">
              <span className="rounded-full bg-abyss/75 px-2.5 py-0.5 font-mono text-[10px] text-ink-3 backdrop-blur">
                {sample?.name}
              </span>
            </div>
          </div>

          {/* 识别结果 */}
          <div className="glass flex min-h-0 flex-col overflow-hidden">
            <div className="flex items-center gap-2 border-b border-hairline/45 px-3.5 py-2">
              <FlaskConical size={13} className="text-aurora-violet" strokeWidth={1.9} />
              <span className="text-[12px] font-semibold text-ink">试识别结果</span>
              {testResult && (
                <>
                  <Badge tone="ok">{testResult.engine.split('.').pop()}</Badge>
                  <span className="font-mono text-[10px] text-ink-4">
                    {testResult.lineCount} 行 · {testResult.elapsedMs}ms
                  </span>
                </>
              )}
              <Button
                size="sm"
                variant="primary"
                className="ml-auto"
                disabled={testing}
                onClick={() => void runTest()}
              >
                {testing ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
                试识别这一张
              </Button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto p-3.5">
              {testError ? (
                <div className="flex items-start gap-2 rounded-lg border border-err/30 bg-err/8 p-3 text-[11.5px] leading-relaxed text-err">
                  <AlertCircle size={14} className="mt-0.5 shrink-0" />
                  <span>{testError}</span>
                </div>
              ) : testResult ? (
                <>
                  {testResult.warnings.length > 0 && (
                    <div className="mb-2.5 space-y-1">
                      {testResult.warnings.map((warning, index) => (
                        <div
                          key={index}
                          className="flex items-start gap-2 rounded-lg border border-warn/25 bg-warn/8 px-2.5 py-1.5 text-[10.5px] leading-relaxed text-warn"
                        >
                          <Info size={11} className="mt-0.5 shrink-0" />
                          <span>{warning}</span>
                        </div>
                      ))}
                    </div>
                  )}

                  {testResult.tables.length > 0 && (
                    <div className="mb-2.5 rounded-lg border border-ok/30 bg-ok/6 px-2.5 py-1.5 text-[10.5px] text-ok">
                      识别出 {testResult.tables.length} 张表格（云端结构化）
                    </div>
                  )}

                  <pre className="selectable font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-ink-2">
                    {testResult.text || '（未识别到文字）'}
                  </pre>

                  {testResult.truncated && (
                    <div className="mt-2 text-[10.5px] text-ink-4">结果过长，预览已截断</div>
                  )}

                  <div className="mt-3 flex flex-wrap gap-3 border-t border-hairline/40 pt-2.5 text-[10.5px] text-ink-4">
                    <span>
                      图片 {testResult.width}×{testResult.height}
                    </span>
                    <span>切片 {testResult.tiles} 块</span>
                    <span>平均置信度 {(testResult.averageConfidence * 100).toFixed(1)}%</span>
                    {Boolean(testResult.usage?.estimatedCostUsd) && (
                      <span className="text-warn">
                        花费 ${Number(testResult.usage.estimatedCostUsd).toFixed(4)}
                      </span>
                    )}
                  </div>
                </>
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
                  <FlaskConical size={22} className="text-ink-4" strokeWidth={1.5} />
                  <div className="text-[11.5px] text-ink-3">还没试识别</div>
                  <div className="max-w-[240px] text-[10.5px] leading-relaxed text-ink-4">
                    建议先点「试识别这一张」确认效果，确认无误再批量处理 ——
                    这样不会因为参数不对而白跑一批
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* 样本切换 + 提交 */}
        <div className="mt-3 flex items-center gap-2.5">
          <span className="shrink-0 text-[11.5px] text-ink-3">样本</span>
          <div className="flex min-w-0 flex-1 gap-1.5 overflow-x-auto pb-1">
            {candidates.slice(0, 14).map((file) => (
              <button
                key={file.id}
                type="button"
                onClick={() => setSelectedId(file.id)}
                title={file.name}
                className={cn(
                  'shrink-0 rounded-md border px-2 py-1 text-[10.5px] transition-colors',
                  file.id === sample?.id
                    ? 'border-aurora-cyan/50 bg-aurora-cyan/12 text-aurora-cyan'
                    : 'border-hairline/60 text-ink-4 hover:border-hairline hover:text-ink-2'
                )}
              >
                <span className="block max-w-[110px] truncate">{file.name}</span>
              </button>
            ))}
            {candidates.length > 14 && (
              <span className="shrink-0 self-center text-[10.5px] text-ink-4">
                +{candidates.length - 14}
              </span>
            )}
          </div>

          <Button variant="primary" size="lg" disabled={submitting} onClick={() => void start()}>
            {submitting ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
            批量处理 {candidates.length} 张
          </Button>
        </div>
      </div>

      {/* ============ 右侧：参数 ============ */}
      <aside className="w-[336px] shrink-0 overflow-y-auto border-l border-hairline/50 bg-void/40 p-4">
        <div className="space-y-3.5">
          {/* 引擎状态 */}
          <Card title="识别引擎" icon={<Cpu size={13} strokeWidth={1.9} />}>
            <div className="space-y-2.5">
              <EngineTile
                icon={<Cpu size={12} />}
                title="本地离线"
                subtitle={localEngine?.label ?? 'RapidOCR'}
                available={localEngine?.available ?? false}
                reason={localEngine?.reason}
                tone="ok"
              />
              <EngineTile
                icon={<Cloud size={12} />}
                title="云端高精度"
                subtitle={cloudEngine?.label ?? 'DeepSeek Vision'}
                available={cloudEngine?.available ?? false}
                reason={cloudEngine?.reason}
                tone="aurora"
              />
              {engineQuery.data && !engineQuery.data.cloudEnabled && (
                <div className="rounded-lg border border-hairline/60 bg-panel-2/40 px-2.5 py-2 text-[10.5px] leading-relaxed text-ink-4">
                  云端识别默认关闭。到「设置」开启并填入 API Key 后，即可对复杂表格与手写内容使用
                  DeepSeek Vision。
                </div>
              )}
            </div>
          </Card>

          {/* 自动生成的参数表单 */}
          <Card title="识别参数" icon={<Type size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <PresetBar
                actionId={actionId}
                params={params}
                onApply={(presetParams) => setParams((current) => ({ ...current, ...presetParams }))}
              />
              <SchemaForm
                schema={action.paramsSchema}
                value={params}
                onChange={setParams}
                exclude={['append_filename_header']}
              />
            </div>
          </Card>

          {/* 输出与批处理 */}
          <Card title="输出与批处理" icon={<ShieldCheck size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <div className="space-y-1">
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

              <div className="space-y-1">
                <span className="text-[11.5px] font-medium text-ink-2">文件名后缀</span>
                <TextInput value={suffix} onChange={setSuffix} placeholder="留空则不加" />
              </div>

              <Switch
                checked={params.append_filename_header === true}
                onChange={(next) => setParams({ ...params, append_filename_header: next })}
                label="添加文件名标题"
                hint="在输出内容开头写入源文件名"
              />

              <div>
                <div className="mb-1.5 flex items-baseline justify-between">
                  <span className="text-[11.5px] font-medium text-ink-2">并发数</span>
                  <span className="font-mono text-[11px] text-ink-4">{concurrency}</span>
                </div>
                <ProgressBar value={(concurrency / 16) * 100} />
                <Select
                  value={String(concurrency)}
                  onChange={(v) => setConcurrency(Number(v))}
                  options={[1, 2, 4, 6, 8, 12, 16].map((n) => ({
                    value: String(n),
                    label: `${n} 个文件同时处理`
                  }))}
                />
              </div>
            </div>
          </Card>

          {/* 隐私说明 */}
          <div className="rounded-lg border border-ok/25 bg-ok/6 px-3 py-2.5 text-[10.5px] leading-relaxed text-ink-3">
            <div className="mb-1 flex items-center gap-1.5 font-medium text-ok">
              <ShieldCheck size={11} />
              隐私说明
            </div>
            选择「本地离线」时，图片**不会离开这台电脑**，也不产生任何费用。
            选择云端时图片会上传到 DeepSeek 用于识别；你可以在设置里配置敏感文件名规则，
            命中的文件会被强制留在本地。
          </div>
        </div>
      </aside>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 引擎状态卡片                                                        */
/* ------------------------------------------------------------------ */

function EngineTile({
  icon,
  title,
  subtitle,
  available,
  reason,
  tone
}: {
  icon: React.ReactNode
  title: string
  subtitle: string
  available: boolean
  reason?: string | null
  tone: 'ok' | 'aurora'
}): React.JSX.Element {
  return (
    <div
      className={cn(
        'rounded-xl border p-2.5',
        available
          ? tone === 'ok'
            ? 'border-ok/30 bg-ok/5'
            : 'border-aurora-cyan/30 bg-aurora-cyan/5'
          : 'border-hairline/60 bg-panel-2/40'
      )}
    >
      <div className="flex items-center gap-2">
        <span className={cn(available ? (tone === 'ok' ? 'text-ok' : 'text-aurora-cyan') : 'text-ink-4')}>
          {icon}
        </span>
        <span className="text-[12px] font-medium text-ink">{title}</span>
        <span className="ml-auto">
          {available ? (
            <CheckCircle2 size={13} className="text-ok" />
          ) : (
            <AlertCircle size={13} className="text-ink-4" />
          )}
        </span>
      </div>
      <div className="mt-1 text-[10.5px] text-ink-4">{subtitle}</div>
      {!available && reason && (
        <div className="mt-1 text-[10.5px] leading-relaxed text-warn/80">{reason}</div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 空状态                                                              */
/* ------------------------------------------------------------------ */

function EmptyState({
  icon,
  title,
  hint,
  onPick
}: {
  icon: React.ReactNode
  title: string
  hint: string
  onPick: () => void
}): React.JSX.Element {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <motion.div
        initial={{ opacity: 0, scale: 0.97 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ type: 'spring', stiffness: 300, damping: 28 }}
        className="glass max-w-md px-8 py-9 text-center"
      >
        <div className="relative mx-auto mb-4 w-fit">
          <span className="anim-pulse-ring absolute inset-0 rounded-2xl bg-aurora-violet/20" />
          <div className="glass-flat anim-float relative flex h-14 w-14 items-center justify-center rounded-2xl">
            <span className="text-aurora-violet">{icon}</span>
          </div>
        </div>
        <h2 className="text-[16px] font-semibold text-ink">{title}</h2>
        <p className="mt-1 text-[12px] text-ink-3">{hint}</p>
        <Button variant="primary" className="mt-4" onClick={onPick}>
          选择图片
        </Button>
      </motion.div>
    </div>
  )
}
