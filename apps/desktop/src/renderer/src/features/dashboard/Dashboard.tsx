import { motion } from 'motion/react'
import {
  Activity,
  CheckCircle2,
  Cpu,
  FileText,
  HardDrive,
  MemoryStick,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Terminal,
  XCircle
} from 'lucide-react'
import type { CorePhase, EngineProbe, SystemCapabilities } from '@shared/index'
import { cn, formatBytes } from '@/lib/utils'
import { useCapabilities, useCoreHealth, useCoreLogs } from '@/hooks/useCore'
import { SelfCheckPanel } from '@/features/diagnostics/SelfCheckPanel'
import { useWorkspace } from '@/store/workspace'

const DOMAIN_LABEL: Record<EngineProbe['domain'], string> = {
  office: 'Office 转换',
  pdf: 'PDF 处理',
  image: '图像处理',
  ocr: '文字识别',
  archive: '压缩归档'
}

const DOMAIN_ORDER: EngineProbe['domain'][] = ['office', 'pdf', 'ocr', 'image', 'archive']

interface DashboardProps {
  phase: CorePhase
  phaseMessage?: string
}

export function Dashboard({ phase, phaseMessage }: DashboardProps): React.JSX.Element {
  const ready = phase === 'ready' || phase === 'degraded'
  const health = useCoreHealth(ready)
  const caps = useCapabilities(ready)
  const logs = useCoreLogs(120)
  const files = useWorkspace((s) => s.files)

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-[1180px] px-7 py-6">
        {/* ---------------- 头部 ---------------- */}
        <div className="mb-6 flex items-end justify-between gap-6">
          <div>
            <h1 className="text-[26px] leading-tight font-semibold tracking-tight">
              <span className="text-aurora">文枢 DocForge</span>
            </h1>
            <p className="mt-1.5 max-w-2xl text-[12.5px] text-ink-2">
              办公文件转换与处理中枢 · 本地优先 · 拖入即用
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              void caps.refetch()
              void health.refetch()
            }}
            className="btn-glow flex items-center gap-2 rounded-lg px-3.5 py-2 text-[12.5px] font-medium text-ink"
          >
            <RefreshCw size={13} strokeWidth={2} className={cn(caps.isFetching && 'animate-spin')} />
            重新探测
          </button>
        </div>

        {/* ---------------- 内核状态 + 资源 ---------------- */}
        <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-[1.15fr_1fr]">
          <CoreStatusCard phase={phase} phaseMessage={phaseMessage} health={health.data} />
          <ResourceCard caps={caps.data} />
        </div>

        {/* ---------------- 引擎能力矩阵 ---------------- */}
        <SectionTitle
          icon={<Sparkles size={14} strokeWidth={1.9} />}
          title="引擎能力矩阵"
          desc="启动时自动探测，转换时按保真度择优路由；不可用的引擎会自动降级到下一档"
        />
        <div className="mb-6">
          {caps.isLoading && !caps.data ? (
            <SkeletonGrid />
          ) : caps.data ? (
            <EngineMatrix engines={caps.data.engines} />
          ) : (
            <EmptyHint text="内核尚未就绪，无法读取引擎能力" />
          )}
        </div>

        {/* ---------------- OCR 双引擎状态 ---------------- */}
        {caps.data && <OcrSummary caps={caps.data} />}

        {/* ---------------- 环境自检 ---------------- */}
        <div className="mt-6">
          <SectionTitle
            icon={<ShieldCheck size={14} strokeWidth={1.9} />}
            title="环境自检"
            desc="检查本机具备哪些能力；缺失可选项只会降级，不影响核心功能"
          />
          <SelfCheckPanel variant="compact" />
        </div>

        {/* ---------------- 日志 + 文件 ---------------- */}
        <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <LogPanel logs={logs} />
          <FilePanel count={files.length} />
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 内核状态卡                                                          */
/* ------------------------------------------------------------------ */

function CoreStatusCard({
  phase,
  phaseMessage,
  health
}: {
  phase: CorePhase
  phaseMessage?: string
  health?: { version: string; python: string; pid: number; uptimeSec: number; platform: string }
}): React.JSX.Element {
  const tone =
    phase === 'ready'
      ? 'var(--color-ok)'
      : phase === 'error'
        ? 'var(--color-err)'
        : phase === 'starting'
          ? 'var(--color-warn)'
          : 'var(--color-idle)'

  const label =
    phase === 'ready'
      ? '运行正常'
      : phase === 'degraded'
        ? '降级运行'
        : phase === 'starting'
          ? '正在启动'
          : phase === 'error'
            ? '启动失败'
            : '已停止'

  return (
    <div className="glass relative overflow-hidden p-5">
      {/* 顶部扫描线，运行时持续扫过，暗示"系统活着" */}
      {phase === 'starting' && (
        <div className="anim-scan pointer-events-none absolute inset-x-0 top-0 h-px bg-linear-to-r from-transparent via-aurora-cyan to-transparent" />
      )}

      <div className="flex items-start gap-5">
        {/* 状态环 */}
        <div className="relative shrink-0">
          <svg width="76" height="76" viewBox="0 0 76 76" className="-rotate-90">
            <circle cx="38" cy="38" r="32" fill="none" stroke="var(--color-hairline)" strokeWidth="4" />
            <motion.circle
              cx="38"
              cy="38"
              r="32"
              fill="none"
              stroke={tone}
              strokeWidth="4"
              strokeLinecap="round"
              strokeDasharray={2 * Math.PI * 32}
              initial={{ strokeDashoffset: 2 * Math.PI * 32 }}
              animate={{ strokeDashoffset: phase === 'ready' ? 2 * Math.PI * 32 * 0.12 : 2 * Math.PI * 32 * 0.55 }}
              transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1] }}
              style={{ filter: `drop-shadow(0 0 6px ${tone})` }}
            />
          </svg>
          <div className="absolute inset-0 flex items-center justify-center">
            {phase === 'ready' ? (
              <CheckCircle2 size={24} style={{ color: tone }} strokeWidth={1.75} />
            ) : phase === 'error' ? (
              <XCircle size={24} style={{ color: tone }} strokeWidth={1.75} />
            ) : (
              <Activity size={24} style={{ color: tone }} strokeWidth={1.75} />
            )}
          </div>
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2.5">
            <span className="text-[17px] font-semibold" style={{ color: tone }}>
              {label}
            </span>
            <span className="font-mono text-[11px] text-ink-4">Python 内核</span>
          </div>

          {health ? (
            <dl className="mt-3 grid grid-cols-2 gap-x-5 gap-y-1.5 text-[11.5px]">
              <Metric label="内核版本" value={`v${health.version}`} />
              <Metric label="运行时" value={health.python} />
              <Metric label="进程 PID" value={String(health.pid)} mono />
              <Metric label="已运行" value={`${Math.round(health.uptimeSec)} 秒`} mono />
            </dl>
          ) : (
            <div className="mt-3 space-y-2">
              <div className="anim-shimmer relative h-3 w-40 overflow-hidden rounded bg-panel-3" />
              <div className="anim-shimmer relative h-3 w-28 overflow-hidden rounded bg-panel-3" />
            </div>
          )}

          {phaseMessage && phase !== 'ready' && (
            <p className="mt-3 line-clamp-3 rounded-lg border border-warn/25 bg-warn/8 px-2.5 py-1.5 text-[11.5px] leading-relaxed text-warn">
              {phaseMessage}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}

function Metric({ label, value, mono }: { label: string; value: string; mono?: boolean }): React.JSX.Element {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-hairline pb-1">
      <dt className="shrink-0 text-ink-4">{label}</dt>
      <dd className={cn('truncate font-medium text-ink-2', mono && 'font-mono text-[11px]')}>{value}</dd>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 系统资源卡                                                          */
/* ------------------------------------------------------------------ */

function ResourceCard({ caps }: { caps?: SystemCapabilities }): React.JSX.Element {
  const usable = caps ? caps.disk.outputFreeBytes / 1024 ** 3 : 0
  const diskTone = usable < 1 ? 'err' : usable < 5 ? 'warn' : 'ok'

  return (
    <div className="glass p-5">
      <div className="mb-3.5 flex items-center gap-2">
        <Cpu size={14} strokeWidth={1.9} className="text-aurora-violet" />
        <span className="text-[13px] font-semibold text-ink">系统资源</span>
      </div>

      {caps ? (
        <div className="space-y-2.5">
          <ResourceRow
            icon={<Cpu size={13} strokeWidth={1.75} />}
            label="CPU"
            value={`${caps.cpu.cores} 逻辑核心`}
            sub={caps.cpu.model}
          />
          <ResourceRow
            icon={<MemoryStick size={13} strokeWidth={1.75} />}
            label="内存"
            value={`${formatBytes(caps.memory.availableBytes)} 可用`}
            sub={`共 ${formatBytes(caps.memory.totalBytes)}`}
          />
          <ResourceRow
            icon={<HardDrive size={13} strokeWidth={1.75} />}
            label="输出磁盘"
            value={`${formatBytes(caps.disk.outputFreeBytes)} 可用`}
            sub={`${caps.os.platform} ${caps.os.release} · ${caps.os.arch}`}
            tone={diskTone}
          />
        </div>
      ) : (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="anim-shimmer relative h-9 overflow-hidden rounded-lg bg-panel-3" />
          ))}
        </div>
      )}
    </div>
  )
}

function ResourceRow({
  icon,
  label,
  value,
  sub,
  tone = 'neutral'
}: {
  icon: React.ReactNode
  label: string
  value: string
  sub?: string
  tone?: 'neutral' | 'ok' | 'warn' | 'err'
}): React.JSX.Element {
  const toneColor =
    tone === 'err'
      ? 'text-err'
      : tone === 'warn'
        ? 'text-warn'
        : tone === 'ok'
          ? 'text-ok'
          : 'text-ink'

  return (
    // 每项资源放进自己的嵌套面：三项之间靠"面"分开，而不是靠几像素的行距。
    // 之前三项是一摞纯文字，CPU 的型号串又长，挤在一起确实分不清哪一行属于谁。
    <div className="surface-2 flex items-start gap-3 p-2.5">
      <span className="mt-0.5 text-ink-3">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-[11.5px] text-ink-3">{label}</span>
          <span className={cn('font-mono text-[12px] font-semibold', toneColor)}>{value}</span>
        </div>
        {sub && <div className="mt-0.5 truncate text-[10.5px] text-ink-4">{sub}</div>}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 引擎能力矩阵                                                        */
/* ------------------------------------------------------------------ */

function EngineMatrix({ engines }: { engines: EngineProbe[] }): React.JSX.Element {
  const grouped = DOMAIN_ORDER.map((domain) => ({
    domain,
    items: engines.filter((e) => e.domain === domain)
  })).filter((g) => g.items.length > 0)

  return (
    <div className="space-y-3.5">
      {grouped.map((group) => (
        <div key={group.domain}>
          <div className="mb-1.5 flex items-center gap-2 px-0.5">
            <span className="text-[11px] font-semibold tracking-wide text-ink-3">
              {DOMAIN_LABEL[group.domain]}
            </span>
            <span className="h-px flex-1 bg-panel-3" />
            <span className="font-mono text-[10px] text-ink-4">
              {group.items.filter((i) => i.available).length}/{group.items.length} 可用
            </span>
          </div>

          <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-3">
            {group.items.map((engine, idx) => (
              <motion.div
                key={engine.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.3, delay: idx * 0.035, ease: [0.16, 1, 0.3, 1] }}
                className={cn(
                  'glass-flat group relative overflow-hidden p-3 transition-colors duration-200',
                  engine.available ? 'hover:border-aurora-cyan/35' : 'opacity-65'
                )}
              >
                <div className="flex items-start justify-between gap-2.5">
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span
                        className={cn(
                          'h-1.5 w-1.5 shrink-0 rounded-full',
                          engine.available ? 'bg-ok' : 'bg-idle'
                        )}
                        style={engine.available ? { boxShadow: '0 0 6px var(--color-ok)' } : undefined}
                      />
                      <span className="truncate text-[12.5px] font-medium text-ink">{engine.label}</span>
                    </div>
                    {engine.detail && (
                      <div className="mt-1 line-clamp-2 text-[10.5px] leading-relaxed text-ink-4 selectable">
                        {engine.detail}
                      </div>
                    )}
                    {!engine.available && engine.reason && (
                      <div className="mt-1 line-clamp-2 text-[10.5px] leading-relaxed text-warn/85">
                        {engine.reason}
                      </div>
                    )}
                  </div>

                  <div className="flex shrink-0 flex-col items-end gap-1">
                    {engine.preferred && (
                      <span className="rounded border border-aurora-cyan/40 bg-aurora-cyan/12 px-1.5 py-px text-[9px] font-semibold tracking-wide text-aurora-cyan">
                        首选
                      </span>
                    )}
                    <span className="font-mono text-[10px] text-ink-4">保真 {engine.fidelity}</span>
                  </div>
                </div>

                {/* 保真度迷你条 */}
                <div className="mt-2.5 h-[3px] overflow-hidden rounded-full bg-panel-3">
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: engine.available ? `${engine.fidelity}%` : '0%' }}
                    transition={{ duration: 0.7, delay: 0.1 + idx * 0.03, ease: [0.16, 1, 0.3, 1] }}
                    className="h-full rounded-full"
                    style={{
                      background: engine.preferred
                        ? 'linear-gradient(90deg, var(--color-aurora-cyan), var(--color-aurora-violet))'
                        : 'color-mix(in oklab, var(--color-ink-3) 70%, transparent)'
                    }}
                  />
                </div>
              </motion.div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function SkeletonGrid(): React.JSX.Element {
  return (
    <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="anim-shimmer relative h-[86px] overflow-hidden rounded-xl bg-panel-2" />
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* OCR 双引擎摘要（对应需求 8）                                         */
/* ------------------------------------------------------------------ */

function OcrSummary({ caps }: { caps: SystemCapabilities }): React.JSX.Element {
  return (
    <div className="glass p-4">
      <div className="mb-3 flex items-center gap-2">
        <ShieldAlert size={14} strokeWidth={1.9} className="text-aurora-cyan" />
        <span className="text-[13px] font-semibold text-ink">OCR 双引擎</span>
        <span className="text-[11px] text-ink-4">默认本地优先 · 云端按需启用</span>
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <OcrEngineTile
          title="本地离线引擎"
          subtitle="隐私最佳 · 零费用 · 断网可用"
          ready={caps.localOcrReady}
          readyText="已就绪"
          missingText="未安装组件"
          missingHint="安装 rapidocr-onnxruntime + onnxruntime 后即可完全离线识别"
        />
        <OcrEngineTile
          title="DeepSeek Vision"
          subtitle="deepseek-flash · 自适应切片 · 复杂表格与手写更强"
          ready={caps.cloudOcrConfigured}
          readyText="已配置密钥"
          missingText="未配置 API Key"
          missingHint="在「设置 → OCR 引擎」中填入密钥，将使用 Windows DPAPI 加密存储"
        />
      </div>
    </div>
  )
}

function OcrEngineTile({
  title,
  subtitle,
  ready,
  readyText,
  missingText,
  missingHint
}: {
  title: string
  subtitle: string
  ready: boolean
  readyText: string
  missingText: string
  missingHint: string
}): React.JSX.Element {
  return (
    <div
      className={cn(
        'rounded-xl border p-3.5 transition-colors',
        ready ? 'border-ok/30 bg-ok/5' : 'border-hairline-2 bg-panel-2'
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="text-[12.5px] font-semibold text-ink">{title}</span>
        <span
          className={cn(
            'flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10.5px] font-medium',
            ready ? 'bg-ok/15 text-ok' : 'bg-panel-3 text-ink-3'
          )}
        >
          {ready ? <CheckCircle2 size={11} strokeWidth={2.2} /> : <XCircle size={11} strokeWidth={2.2} />}
          {ready ? readyText : missingText}
        </span>
      </div>
      <div className="mt-1 text-[10.5px] text-ink-4">{subtitle}</div>
      {!ready && <div className="mt-1.5 text-[10.5px] leading-relaxed text-warn/80">{missingHint}</div>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 日志面板 / 文件面板                                                  */
/* ------------------------------------------------------------------ */

function LogPanel({ logs }: { logs: string[] }): React.JSX.Element {
  return (
    <div className="glass flex min-h-[230px] flex-col overflow-hidden">
      <div className="flex items-center gap-2 border-b border-hairline px-4 py-2.5">
        <Terminal size={13} strokeWidth={1.9} className="text-ok" />
        <span className="text-[12.5px] font-semibold text-ink">内核日志</span>
        <span className="ml-auto font-mono text-[10px] text-ink-4">{logs.length} 行</span>
      </div>
      <div className="selectable max-h-[260px] flex-1 overflow-y-auto px-3.5 py-2.5 font-mono text-[10.5px] leading-[1.7] text-ink-3">
        {logs.length === 0 ? (
          <div className="pt-2 text-center text-ink-4">等待内核输出…</div>
        ) : (
          logs.map((line, i) => (
            <div key={i} className="flex gap-2">
              <span className="shrink-0 text-ink-4/60">{String(i + 1).padStart(3, '0')}</span>
              <span className="break-all whitespace-pre-wrap">{line}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

function FilePanel({ count }: { count: number }): React.JSX.Element {
  return (
    <div className="glass flex min-h-[230px] flex-col overflow-hidden">
      <div className="flex items-center gap-2 border-b border-hairline px-4 py-2.5">
        <FileText size={13} strokeWidth={1.9} className="text-aurora-violet" />
        <span className="text-[12.5px] font-semibold text-ink">工作区文件</span>
        <span className="ml-auto font-mono text-[10px] text-ink-4">{count} 个</span>
      </div>
      <div className="flex flex-1 items-center justify-center p-6">
        {count === 0 ? (
          <div className="text-center">
            <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl border border-dashed border-hairline bg-panel-2">
              <FileText size={18} className="text-ink-4" strokeWidth={1.5} />
            </div>
            <div className="text-[12px] text-ink-3">还没有文件</div>
            <div className="mt-1 text-[11px] text-ink-4">把文件拖到窗口任意位置即可导入</div>
          </div>
        ) : (
          <div className="text-center text-[12px] text-ink-2">
            已导入 <span className="font-mono font-semibold text-aurora-cyan">{count}</span> 个文件
            <div className="mt-1.5 text-[11px] text-ink-4">
              到「图片水印」可直接处理照片，或在「批量任务」查看队列
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 小组件                                                              */
/* ------------------------------------------------------------------ */

function SectionTitle({
  icon,
  title,
  desc
}: {
  icon: React.ReactNode
  title: string
  desc: string
}): React.JSX.Element {
  return (
    <div className="mb-2.5 flex items-baseline gap-2.5">
      <span className="text-aurora-cyan">{icon}</span>
      <span className="text-[13.5px] font-semibold text-ink">{title}</span>
      <span className="text-[11px] text-ink-4">{desc}</span>
    </div>
  )
}

function EmptyHint({ text }: { text: string }): React.JSX.Element {
  return (
    <div className="glass-flat px-4 py-8 text-center text-[12px] text-ink-3">{text}</div>
  )
}
