import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  XCircle
} from 'lucide-react'
import type { CheckStatus, SelfCheckReport } from '@shared/index'
import { cn } from '@/lib/utils'
import { Badge, Button, Card } from '@/components/ui'

/** 首次启动向导是否已展示过的标记（纯界面状态，放本地即可） */
const SEEN_KEY = 'docforge.selfcheck.seen'

/**
 * 内核冷启动要加载 PyMuPDF / ONNX 等重依赖，打包后实测需要 8 秒左右。
 * 自检面板通常在这之前就挂载了，因此需要等一会儿再判定为失败。
 * 约 30 秒的总等待窗口足够覆盖慢机器。
 */
const MAX_READY_RETRIES = 15
const READY_RETRY_DELAY_MS = 2000

const STATUS_META: Record<
  CheckStatus,
  { icon: React.ReactNode; tone: 'ok' | 'warn' | 'err'; label: string }
> = {
  ok: { icon: <CheckCircle2 size={14} />, tone: 'ok', label: '可用' },
  warn: { icon: <AlertTriangle size={14} />, tone: 'warn', label: '降级' },
  fail: { icon: <XCircle size={14} />, tone: 'err', label: '不可用' }
}

/**
 * 环境自检面板。
 *
 * 存在的意义是回答用户最关心的那个问题：**"我这台机器上它能干什么？"**
 * 与其让用户逐个功能去试、撞到报错才知道缺东西，不如一次性把环境状况、
 * 缺失组件、以及缺失会造成什么影响都摆清楚。
 *
 * 请注意文案的取向：**缺失可选组件时说的是"功能降级"，不是"错误"**。
 * 没有 Office 不代表软件坏了，只是保真度低一些；把这种区别讲清楚，
 * 用户才不会以为装了个残缺的东西。
 */
export function SelfCheckPanel({
  /** 紧凑模式用于设置页；完整模式用于首次启动向导 */
  variant = 'full'
}: {
  variant?: 'full' | 'compact'
}): React.JSX.Element {
  const [report, setReport] = useState<SelfCheckReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  /** 内核还在启动时的重试计数 */
  const [waiting, setWaiting] = useState(false)
  const attemptRef = useRef(0)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await window.docforge.system.selfCheck()
      setReport(next)
      setWaiting(false)
      attemptRef.current = 0
    } catch (err) {
      const message = (err as Error).message
      // 打包后内核冷启动要好几秒（要加载 PyMuPDF / ONNX 等重依赖），
      // 而自检面板往往在这之前就挂载了。此时"内核尚未就绪"是**预期状态**，
      // 不是错误 —— 直接甩一个红框给用户，会让人以为程序坏了。
      // 因此这里自动重试，界面上显示"正在等待内核启动"。
      const notReady = /尚未就绪|未初始化|not ready/i.test(message)
      if (notReady && attemptRef.current < MAX_READY_RETRIES) {
        attemptRef.current += 1
        setWaiting(true)
        window.setTimeout(() => void load(), READY_RETRY_DELAY_MS)
        return
      }
      setWaiting(false)
      setError(message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    // 组件卸载后不再重试，避免对已销毁组件 setState
    return () => {
      attemptRef.current = MAX_READY_RETRIES
    }
  }, [load])

  if (waiting && !report) {
    return (
      <div className="flex items-center justify-center gap-2 py-8 text-[12px] text-ink-3">
        <Loader2 size={14} className="animate-spin" />
        正在等待处理内核启动…
      </div>
    )
  }

  if (loading && !report) {
    return (
      <div className="flex items-center justify-center gap-2 py-8 text-[12px] text-ink-3">
        <Loader2 size={14} className="animate-spin" />
        正在检查运行环境…
      </div>
    )
  }

  if (error) {
    return (
      <div className="space-y-2 rounded-lg border border-err/30 bg-err/8 px-3 py-2.5">
        <div className="text-[11.5px] text-err">自检失败：{error}</div>
        <button
          type="button"
          onClick={() => {
            attemptRef.current = 0
            void load()
          }}
          className="text-[11px] text-ink-3 underline transition-colors hover:text-ink"
        >
          重试
        </button>
      </div>
    )
  }

  if (!report) return <></>

  const attention = report.items.filter((item) => item.status !== 'ok')
  const visible =
    variant === 'compact' && !expanded
      ? report.items.filter((item) => item.status !== 'ok')
      : report.items

  return (
    <div className="space-y-3">
      {/* 总览 */}
      <div
        className={cn(
          'flex items-center gap-3 rounded-xl border px-3.5 py-3',
          report.ready ? 'border-ok/30 bg-ok/6' : 'border-err/30 bg-err/6'
        )}
      >
        <span className={report.ready ? 'text-ok' : 'text-err'}>
          {report.ready ? <ShieldCheck size={20} /> : <XCircle size={20} />}
        </span>
        <div className="min-w-0 flex-1">
          <div className={cn('text-[13px] font-semibold', report.ready ? 'text-ok' : 'text-err')}>
            {report.ready
              ? attention.length > 0
                ? '核心功能就绪，部分能力将降级'
                : '全部检查通过，所有功能可用'
              : '存在影响核心功能的缺失项'}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[10.5px] text-ink-4">
            <span>
              通过 <span className="font-mono text-ok">{report.okCount}</span>
            </span>
            {report.warnCount > 0 && (
              <span>
                降级 <span className="font-mono text-warn">{report.warnCount}</span>
              </span>
            )}
            {report.failCount > 0 && (
              <span>
                不可用 <span className="font-mono text-err">{report.failCount}</span>
              </span>
            )}
          </div>
        </div>
        <Button size="sm" variant="ghost" onClick={() => void load()} title="重新检查">
          <RefreshCw size={13} className={cn(loading && 'animate-spin')} />
        </Button>
      </div>

      {/* 明细 */}
      <div className="space-y-1">
        <AnimatePresence initial={false}>
          {visible.map((item, index) => {
            const meta = STATUS_META[item.status]
            return (
              <motion.div
                key={item.id}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.2, delay: Math.min(index * 0.02, 0.2) }}
                className={cn(
                  'flex items-start gap-2.5 rounded-lg border px-3 py-2',
                  item.status === 'ok'
                    ? 'border-hairline bg-panel-2'
                    : item.status === 'warn'
                      ? 'border-warn/25 bg-warn/6'
                      : 'border-err/30 bg-err/8'
                )}
              >
                <span
                  className={cn(
                    'mt-0.5 shrink-0',
                    meta.tone === 'ok' ? 'text-ok' : meta.tone === 'warn' ? 'text-warn' : 'text-err'
                  )}
                >
                  {meta.icon}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[12px] font-medium text-ink">{item.label}</span>
                    {item.status !== 'ok' && <Badge tone={meta.tone}>{meta.label}</Badge>}
                  </div>
                  <div className="mt-0.5 truncate text-[10.5px] text-ink-4" title={item.detail}>
                    {item.detail}
                  </div>
                  {item.impact && (
                    <div className="mt-1 text-[10.5px] leading-relaxed text-warn/90">
                      {item.impact}
                    </div>
                  )}
                </div>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </div>

      {variant === 'compact' && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg py-1.5 text-[11px] text-ink-4 transition-colors hover:bg-panel-2 hover:text-ink-2"
        >
          <ChevronDown size={12} className={cn('transition-transform', expanded && 'rotate-180')} />
          {expanded ? '只看需要关注的项' : `查看全部 ${report.items.length} 项检查`}
        </button>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 首次启动向导                                                        */
/* ------------------------------------------------------------------ */

/**
 * 首次启动时弹出的自检向导。
 *
 * 只在第一次运行时出现，之后可以用「重新检查」按钮随时再跑。
 * 刻意不做成强制阻塞的向导 —— 用户看完直接就能开始用，
 * 不必先走完一串"下一步"。
 *
 * **刻意不用 AnimatePresence 做关闭动画**：退出动画依赖 requestAnimationFrame，
 * 而窗口被遮挡时 Chromium 会把页面标记为 hidden 并完全暂停 rAF ——
 * 动画永远播不完，模态框就一直留在 DOM 里挡住整个界面。
 * 实测踩过：点击"开始使用"后 localStorage 标记已写入（逻辑是对的），
 * 但弹窗纹丝不动。只保留入场动画，视觉几乎无差别，但关闭一定生效。
 */
export function FirstRunWizard(): React.JSX.Element {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    try {
      if (window.localStorage.getItem(SEEN_KEY) !== '1') setVisible(true)
    } catch {
      // 隐私模式下 localStorage 可能不可用，那就干脆不弹
    }
  }, [])

  const dismiss = (): void => {
    try {
      window.localStorage.setItem(SEEN_KEY, '1')
    } catch {
      /* 忽略 */
    }
    setVisible(false)
  }

  if (!visible) return <></>

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.18 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-abyss/75 backdrop-blur-md"
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ type: 'spring', stiffness: 320, damping: 30 }}
        className="glass flex max-h-[82vh] w-[600px] flex-col overflow-hidden"
      >
        <div className="border-b border-hairline px-5 py-4">
          <div className="flex items-center gap-2.5">
            <Sparkles size={17} className="text-aurora-cyan" />
            <h2 className="text-[16px] font-semibold text-ink">欢迎使用文枢 DocForge</h2>
          </div>
          <p className="mt-1 text-[11.5px] text-ink-3">
            第一次运行，先花一秒检查一下这台机器的环境 —— 看看哪些能力已经可用。
          </p>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <SelfCheckPanel variant="full" />
        </div>

        <div className="flex items-center gap-2 border-t border-hairline px-5 py-3">
          <span className="text-[10.5px] text-ink-4">随时可在「总览」或「设置」里重新检查</span>
          <Button variant="primary" className="ml-auto" onClick={dismiss}>
            开始使用
          </Button>
        </div>
      </motion.div>
    </motion.div>
  )
}
