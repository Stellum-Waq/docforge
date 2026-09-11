import { useEffect, useState } from 'react'
import { motion } from 'motion/react'
import {
  CheckCircle2,
  Clock,
  Database,
  FolderOpen,
  Layers,
  Loader2,
  Trash2,
  XCircle
} from 'lucide-react'
import type { JobHistoryEntry, JobStatus } from '@shared/index'
import { cn } from '@/lib/utils'
import { Badge, Button, Card, ProgressBar } from '@/components/ui'
import { useJobs } from '@/store/jobs'

const TONE: Record<JobStatus, 'ok' | 'warn' | 'err' | 'run' | 'idle'> = {
  queued: 'idle',
  running: 'run',
  paused: 'warn',
  succeeded: 'ok',
  failed: 'err',
  cancelled: 'idle',
  skipped: 'warn'
}

const LABEL: Record<JobStatus, string> = {
  queued: '排队中',
  running: '处理中',
  paused: '已暂停',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  skipped: '已跳过'
}

function formatTime(iso?: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function duration(job: JobHistoryEntry): string {
  if (!job.startedAt || !job.finishedAt) return '—'
  const ms = new Date(job.finishedAt).getTime() - new Date(job.startedAt).getTime()
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}分${Math.round((ms % 60_000) / 1000)}秒`
}

/**
 * 批量任务页。
 *
 * 展示"正在运行的任务"与"历史记录"两部分。历史来自内核的 SQLite，
 * 因此关掉应用重开后依然可查 —— 这也是任务可复现（保存配方）的基础。
 */
export function BatchPage(): React.JSX.Element {
  const jobs = useJobs((s) => s.jobs)
  const order = useJobs((s) => s.order)
  const tasks = useJobs((s) => s.tasks)
  const setActive = useJobs((s) => s.setActive)
  const setDrawerOpen = useJobs((s) => s.setDrawerOpen)

  const [history, setHistory] = useState<JobHistoryEntry[]>([])
  const [cache, setCache] = useState({ entries: 0, hits: 0 })
  const [loading, setLoading] = useState(true)

  const load = (): void => {
    setLoading(true)
    void window.docforge.jobs
      .history(100)
      .then((data) => {
        setHistory(data.jobs)
        setCache(data.cache)
      })
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const live = order.map((id) => jobs[id]).filter(Boolean)
  const liveIds = new Set(live.map((j) => j.id))

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-[1100px] px-7 py-6">
        <div className="mb-5 flex items-end justify-between gap-4">
          <div>
            <h1 className="flex items-center gap-2.5 text-[20px] font-semibold tracking-tight text-ink">
              <Layers size={19} className="text-aurora-cyan" strokeWidth={1.8} />
              批量任务
            </h1>
            <p className="mt-1 text-[12px] text-ink-3">
              所有功能共用同一套任务队列 · 单个文件失败不会影响整批
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Badge tone="aurora">
              <Database size={10} />
              缓存 {cache.entries} 条 / 命中 {cache.hits} 次
            </Badge>
            <Button size="sm" onClick={load}>
              刷新
            </Button>
          </div>
        </div>

        {/* ---------------- 进行中 ---------------- */}
        <div className="mb-2 flex items-center gap-2">
          <span className="text-[12.5px] font-semibold text-ink">正在运行</span>
          <span className="h-px flex-1 bg-hairline/50" />
          <span className="font-mono text-[10.5px] text-ink-4">{live.length}</span>
        </div>

        {live.length === 0 ? (
          <Card className="mb-6">
            <div className="py-6 text-center text-[12px] text-ink-4">
              当前没有进行中的任务
            </div>
          </Card>
        ) : (
          <div className="mb-6 space-y-2.5">
            {live.map((job) => {
              const jobTasks = tasks[job.id] ?? []
              const failedTasks = jobTasks.filter((t) => t.status === 'failed')
              return (
                <motion.div
                  key={job.id}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
                >
                  <Card>
                    <div className="flex items-center gap-3">
                      {job.status === 'running' ? (
                        <Loader2 size={15} className="shrink-0 animate-spin text-run" />
                      ) : job.failedTasks > 0 ? (
                        <XCircle size={15} className="shrink-0 text-warn" />
                      ) : (
                        <CheckCircle2 size={15} className="shrink-0 text-ok" />
                      )}

                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-[12.5px] font-medium text-ink">
                            {job.actionLabel}
                          </span>
                          <Badge tone={TONE[job.status]}>{LABEL[job.status]}</Badge>
                        </div>
                        <div className="mt-1.5">
                          <ProgressBar value={job.progress} />
                        </div>
                        <div className="mt-1 flex items-center gap-3 text-[10.5px] text-ink-4">
                          <span>
                            {job.completedTasks}/{job.totalTasks} 完成
                          </span>
                          {job.failedTasks > 0 && <span className="text-err">{job.failedTasks} 失败</span>}
                          <span className="selectable truncate font-mono" title={job.outputDir}>
                            {job.outputDir}
                          </span>
                        </div>

                        {failedTasks.length > 0 && (
                          <div className="mt-1.5 space-y-0.5">
                            {failedTasks.slice(0, 3).map((t) => (
                              <div key={t.id} className="truncate text-[10.5px] text-err/85" title={t.error ?? ''}>
                                {t.fileName}：{t.error}
                              </div>
                            ))}
                            {failedTasks.length > 3 && (
                              <div className="text-[10.5px] text-ink-4">
                                还有 {failedTasks.length - 3} 个失败文件…
                              </div>
                            )}
                          </div>
                        )}
                      </div>

                      <div className="flex shrink-0 gap-1.5">
                        <Button
                          size="sm"
                          onClick={() => {
                            setActive(job.id)
                            setDrawerOpen(true)
                          }}
                        >
                          详情
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          title="打开输出目录"
                          onClick={() => void window.docforge.shell.openPath(job.outputDir)}
                        >
                          <FolderOpen size={13} />
                        </Button>
                      </div>
                    </div>
                  </Card>
                </motion.div>
              )
            })}
          </div>
        )}

        {/* ---------------- 历史 ---------------- */}
        <div className="mb-2 flex items-center gap-2">
          <span className="text-[12.5px] font-semibold text-ink">历史记录</span>
          <span className="h-px flex-1 bg-hairline/50" />
          {history.length > 0 && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                void window.docforge.jobs.clearHistory().then(load)
              }}
            >
              <Trash2 size={12} />
              清空
            </Button>
          )}
        </div>

        {loading ? (
          <div className="glass-flat py-8 text-center text-[12px] text-ink-4">加载中…</div>
        ) : history.length === 0 ? (
          <div className="glass-flat py-8 text-center text-[12px] text-ink-4">
            还没有历史记录。完成任务后会自动落盘，重开应用也能查到。
          </div>
        ) : (
          <div className="glass overflow-hidden">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-hairline/50 text-[10.5px] tracking-wide text-ink-4">
                  <th className="px-4 py-2 font-medium">任务</th>
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-3 py-2 text-right font-medium">文件</th>
                  <th className="px-3 py-2 text-right font-medium">耗时</th>
                  <th className="px-3 py-2 font-medium">创建时间</th>
                </tr>
              </thead>
              <tbody>
                {history.map((job) => (
                  <tr
                    key={job.id}
                    className={cn(
                      'border-b border-hairline/25 text-[11.5px] transition-colors last:border-0 hover:bg-panel-2/40',
                      liveIds.has(job.id) && 'bg-aurora-cyan/4'
                    )}
                  >
                    <td className="px-4 py-2">
                      <span className="text-ink-2">{job.actionLabel}</span>
                    </td>
                    <td className="px-3 py-2">
                      <Badge tone={TONE[job.status]}>{LABEL[job.status]}</Badge>
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-ink-3">
                      {job.completedTasks}/{job.totalTasks}
                      {job.failedTasks > 0 && <span className="text-err"> ·{job.failedTasks}✗</span>}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-ink-4">{duration(job)}</td>
                    <td className="px-3 py-2">
                      <span className="flex items-center gap-1.5 text-ink-4">
                        <Clock size={10} />
                        {formatTime(job.createdAt)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
