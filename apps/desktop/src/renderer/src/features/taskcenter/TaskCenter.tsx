import { useMemo, useRef, useState } from 'react'
import { motion } from 'motion/react'
import { useVirtualizer } from '@tanstack/react-virtual'
import {
  CheckCircle2,
  CircleDashed,
  FolderOpen,
  Loader2,
  Pause,
  Play,
  SkipForward,
  Trash2,
  X,
  XCircle
} from 'lucide-react'
import type { Job, JobStatus, JobTask } from '@shared/index'
import { cn } from '@/lib/utils'
import { Badge, Button, ProgressBar, ProgressRing } from '@/components/ui'
import { useJobs } from '@/store/jobs'

/* ------------------------------------------------------------------ */
/* 状态展示映射                                                        */
/* ------------------------------------------------------------------ */

const STATUS_META: Record<JobStatus, { label: string; tone: 'ok' | 'warn' | 'err' | 'run' | 'idle'; icon: React.ReactNode }> = {
  queued: { label: '排队中', tone: 'idle', icon: <CircleDashed size={12} /> },
  running: { label: '处理中', tone: 'run', icon: <Loader2 size={12} className="animate-spin" /> },
  paused: { label: '已暂停', tone: 'warn', icon: <Pause size={12} /> },
  succeeded: { label: '已完成', tone: 'ok', icon: <CheckCircle2 size={12} /> },
  failed: { label: '失败', tone: 'err', icon: <XCircle size={12} /> },
  cancelled: { label: '已取消', tone: 'idle', icon: <XCircle size={12} /> },
  skipped: { label: '已跳过', tone: 'warn', icon: <SkipForward size={12} /> }
}

type Filter = 'all' | 'running' | 'failed' | 'done'

/**
 * 任务中心抽屉。
 *
 * **关闭不依赖退出动画**（所以没有用 AnimatePresence 的 exit）。
 * 原因与首次启动向导相同：退出动画依赖 requestAnimationFrame，
 * 窗口被遮挡时 Chromium 会暂停 rAF，动画永远播不完，抽屉就关不掉了。
 * 入场动画保留 —— 观感上的"高级感"主要来自出现的那一刻。
 */
export function TaskCenter(): React.JSX.Element {
  const {
    jobs,
    order,
    tasks,
    logs,
    activeJobId,
    drawerOpen,
    setActive,
    setDrawerOpen,
    cancel,
    pause,
    resume,
    clearFinished
  } = useJobs()

  const [filter, setFilter] = useState<Filter>('all')

  const job: Job | null = activeJobId ? (jobs[activeJobId] ?? null) : null
  const allTasks: JobTask[] = activeJobId ? (tasks[activeJobId] ?? []) : []

  const visible = useMemo(() => {
    switch (filter) {
      case 'running':
        return allTasks.filter((t) => t.status === 'running' || t.status === 'queued')
      case 'failed':
        return allTasks.filter((t) => t.status === 'failed')
      case 'done':
        return allTasks.filter((t) => t.status === 'succeeded' || t.status === 'skipped')
      default:
        return allTasks
    }
  }, [allTasks, filter])

  const jobLogs = useMemo(
    () => (activeJobId ? logs.filter((l) => l.jobId === activeJobId) : []),
    [logs, activeJobId]
  )

  if (!drawerOpen) return <></>

  return (
    <>
      {/* 遮罩：点击关闭。桌面应用里遮罩要轻，不能像 Web 那样压暗全屏 */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.18 }}
        onClick={() => setDrawerOpen(false)}
        className="absolute inset-0 z-30 bg-abyss/45 backdrop-blur-[2px]"
      />

      <motion.aside
        initial={{ x: 420, opacity: 0 }}
        animate={{ x: 0, opacity: 1 }}
        transition={{ type: 'spring', stiffness: 380, damping: 36 }}
        className="absolute inset-y-0 right-0 z-40 flex w-[440px] flex-col border-l border-hairline/60 bg-panel/92 backdrop-blur-2xl"
      >
        {job ? (
          <JobPanel
            job={job}
            allTasks={allTasks}
            visible={visible}
            filter={filter}
            onFilter={setFilter}
            logs={jobLogs}
            onClose={() => setDrawerOpen(false)}
            onCancel={() => void cancel(job.id)}
            onPause={() => void pause(job.id)}
            onResume={() => void resume(job.id)}
          />
        ) : (
          <JobList
            jobs={order.map((id) => jobs[id]).filter(Boolean)}
            activeId={activeJobId}
            onSelect={setActive}
            onClose={() => setDrawerOpen(false)}
            onClearFinished={clearFinished}
          />
        )}
      </motion.aside>
    </>
  )
}

/* ------------------------------------------------------------------ */
/* 单任务面板                                                          */
/* ------------------------------------------------------------------ */

function JobPanel({
  job,
  allTasks,
  visible,
  filter,
  onFilter,
  logs,
  onClose,
  onCancel,
  onPause,
  onResume
}: {
  job: Job
  allTasks: JobTask[]
  visible: JobTask[]
  filter: Filter
  onFilter: (f: Filter) => void
  logs: { id: string; fileName: string; line: string }[]
  onClose: () => void
  onCancel: () => void
  onPause: () => void
  onResume: () => void
}): React.JSX.Element {
  const meta = STATUS_META[job.status]
  const running = job.status === 'running' || job.status === 'queued'
  const paused = job.status === 'paused'
  const failedCount = allTasks.filter((t) => t.status === 'failed').length

  return (
    <>
      {/* 头部 */}
      <header className="flex items-center gap-3 border-b border-hairline/50 px-4 py-3">
        <ProgressRing value={job.progress} size={44} stroke={3.5}>
          <span className="font-mono text-[10.5px] font-semibold text-ink">
            {Math.round(job.progress)}%
          </span>
        </ProgressRing>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-semibold text-ink">{job.actionLabel}</span>
            <Badge tone={meta.tone}>
              {meta.icon}
              {meta.label}
            </Badge>
          </div>
          <div className="mt-0.5 truncate text-[11px] text-ink-4">
            {job.completedTasks}/{job.totalTasks} 完成
            {job.failedTasks > 0 && <span className="text-err"> · {job.failedTasks} 失败</span>}
            {job.totalTasks > 0 && <span className="text-ink-4"> · {job.totalTasks - job.completedTasks - job.failedTasks} 剩余</span>}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          {running && (
            <Button size="sm" variant="ghost" onClick={onPause} title="暂停派发新文件">
              <Pause size={13} />
            </Button>
          )}
          {paused && (
            <Button size="sm" variant="ghost" onClick={onResume} title="继续">
              <Play size={13} />
            </Button>
          )}
          {(running || paused) && (
            <Button size="sm" variant="danger" onClick={onCancel} title="取消任务">
              <Trash2 size={13} />
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={onClose} title="关闭">
            <X size={14} />
          </Button>
        </div>
      </header>

      {/* 结果目录 */}
      <div className="flex items-center gap-2 border-b border-hairline/40 px-4 py-2">
        <FolderOpen size={12} className="shrink-0 text-ink-4" />
        <span className="selectable truncate font-mono text-[10.5px] text-ink-4" title={job.outputDir}>
          {job.outputDir}
        </span>
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto shrink-0"
          onClick={() => void window.docforge.shell.openPath(job.outputDir)}
        >
          打开
        </Button>
      </div>

      {/* 失败汇总 */}
      {job.error && (
        <div className="border-b border-hairline/40 bg-warn/6 px-4 py-2 text-[11px] text-warn">
          {job.error}
        </div>
      )}

      {/* 过滤器 */}
      <div className="flex gap-1 border-b border-hairline/40 px-3 py-2">
        {(
          [
            ['all', `全部 ${allTasks.length}`],
            ['running', `进行中 ${allTasks.filter((t) => t.status === 'running' || t.status === 'queued').length}`],
            ['failed', `失败 ${failedCount}`],
            ['done', `完成 ${allTasks.filter((t) => t.status === 'succeeded' || t.status === 'skipped').length}`]
          ] as [Filter, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => onFilter(key)}
            className={cn(
              'rounded-md px-2 py-1 text-[11px] transition-colors',
              filter === key
                ? 'bg-aurora-cyan/14 text-aurora-cyan'
                : 'text-ink-3 hover:bg-panel-2/60 hover:text-ink-2'
            )}
          >
            {label}
          </button>
        ))}
      </div>

      <TaskList tasks={visible} />

      {logs.length > 0 && <LogStrip logs={logs} />}
    </>
  )
}

/* ------------------------------------------------------------------ */
/* 任务列表（虚拟滚动）                                                 */
/* ------------------------------------------------------------------ */

function TaskList({ tasks }: { tasks: JobTask[] }): React.JSX.Element {
  const scrollRef = useRef<HTMLDivElement>(null)

  // 上千个文件时 DOM 会爆掉，因此用虚拟滚动。行高固定，估算准确。
  const virtualizer = useVirtualizer({
    count: tasks.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 46,
    overscan: 10
  })

  if (tasks.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-[12px] text-ink-4">
        该筛选条件下没有文件
      </div>
    )
  }

  return (
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-2 py-1.5">
      <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
        {virtualizer.getVirtualItems().map((item) => {
          const task = tasks[item.index]
          const meta = STATUS_META[task.status]
          return (
            <div
              key={item.key}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: '100%',
                transform: `translateY(${item.start}px)`
              }}
              className="px-1.5 py-[3px]"
            >
              <div className="rounded-lg border border-transparent px-2 py-1.5 transition-colors hover:border-hairline/60 hover:bg-panel-2/45">
                <div className="flex items-center gap-2">
                  <span className={cn('shrink-0', meta.tone === 'err' ? 'text-err' : meta.tone === 'ok' ? 'text-ok' : 'text-ink-4')}>
                    {meta.icon}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[11.5px] text-ink-2" title={task.filePath}>
                    {task.fileName}
                  </span>
                  {task.durationMs != null && task.status === 'succeeded' && (
                    <span className="shrink-0 font-mono text-[10px] text-ink-4">{task.durationMs}ms</span>
                  )}
                  {task.outputPath && task.status === 'succeeded' && (
                    <button
                      type="button"
                      title="在资源管理器中显示"
                      onClick={(e) => {
                        e.stopPropagation()
                        void window.docforge.files.revealInExplorer(task.outputPath!)
                      }}
                      className="shrink-0 rounded p-0.5 text-ink-4 transition-colors hover:bg-panel-3 hover:text-aurora-cyan"
                    >
                      <FolderOpen size={12} />
                    </button>
                  )}
                </div>

                {task.status === 'running' && (
                  <div className="mt-1 pl-[22px]">
                    <ProgressBar value={task.progress} />
                  </div>
                )}

                {task.error && (
                  <div className="mt-0.5 truncate pl-[22px] text-[10.5px] text-err/85" title={task.error}>
                    {task.error}
                  </div>
                )}
                {!task.error && task.message && task.status === 'succeeded' && (
                  <div className="mt-0.5 truncate pl-[22px] text-[10.5px] text-ink-4">{task.message}</div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 日志条                                                              */
/* ------------------------------------------------------------------ */

function LogStrip({ logs }: { logs: { id: string; fileName: string; line: string }[] }): React.JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  const [collapsed, setCollapsed] = useState(false)

  return (
    <div className="shrink-0 border-t border-hairline/50">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-1.5 text-[10.5px] text-ink-4 transition-colors hover:text-ink-2"
      >
        <span>事件日志</span>
        <span className="font-mono">{logs.length}</span>
        <span className="ml-auto">{collapsed ? '展开' : '收起'}</span>
      </button>
      {!collapsed && (
        <div
          ref={ref}
          className="selectable max-h-28 overflow-y-auto px-4 pb-2.5 font-mono text-[10px] leading-relaxed text-ink-4"
        >
          {logs.slice(-60).map((log) => (
            <div key={log.id} className="truncate">
              <span className="text-ink-3">{log.fileName}</span> · {log.line}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 任务列表视图（未选中具体任务时）                                      */
/* ------------------------------------------------------------------ */

function JobList({
  jobs,
  activeId,
  onSelect,
  onClose,
  onClearFinished
}: {
  jobs: Job[]
  activeId: string | null
  onSelect: (id: string) => void
  onClose: () => void
  onClearFinished: () => void
}): React.JSX.Element {
  return (
    <>
      <header className="flex items-center gap-2 border-b border-hairline/50 px-4 py-3">
        <span className="text-[13px] font-semibold text-ink">任务中心</span>
        <Badge tone="idle">{jobs.length}</Badge>
        <div className="ml-auto flex gap-1.5">
          <Button size="sm" variant="ghost" onClick={onClearFinished}>
            清理已完成
          </Button>
          <Button size="sm" variant="ghost" onClick={onClose}>
            <X size={14} />
          </Button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {jobs.length === 0 ? (
          <div className="flex h-full items-center justify-center text-[12px] text-ink-4">
            还没有任务
          </div>
        ) : (
          jobs.map((job) => {
            const meta = STATUS_META[job.status]
            return (
              <button
                key={job.id}
                type="button"
                onClick={() => onSelect(job.id)}
                className={cn(
                  'mb-1.5 w-full rounded-lg border px-3 py-2 text-left transition-colors',
                  job.id === activeId
                    ? 'border-aurora-cyan/30 bg-aurora-cyan/8'
                    : 'border-hairline/50 hover:border-hairline hover:bg-panel-2/50'
                )}
              >
                <div className="flex items-center gap-2">
                  <span className="truncate text-[12px] font-medium text-ink">{job.actionLabel}</span>
                  <Badge tone={meta.tone}>{meta.label}</Badge>
                  <span className="ml-auto font-mono text-[10px] text-ink-4">
                    {Math.round(job.progress)}%
                  </span>
                </div>
                <div className="mt-1.5">
                  <ProgressBar value={job.progress} />
                </div>
              </button>
            )
          })
        )}
      </div>
    </>
  )
}
