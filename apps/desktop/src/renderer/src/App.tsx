import { useEffect, useState } from 'react'
import { motion } from 'motion/react'
import { Activity, ChevronUp, Layers } from 'lucide-react'
import { TitleBar } from '@/components/TitleBar'
import { Sidebar, NAV_GROUPS, type RouteId } from '@/components/Sidebar'
import { DropOverlay } from '@/components/DropOverlay'
import { ParticleField } from '@/components/ParticleField'
import { Dashboard } from '@/features/dashboard/Dashboard'
import { ImageWatermark } from '@/features/image-watermark/ImageWatermark'
import { OcrWorkbench } from '@/features/ocr/OcrWorkbench'
import { PdfWatermark } from '@/features/pdf/PdfWatermark'
import { PdfToolbox } from '@/features/pdf/PdfToolbox'
import { DocConvert } from '@/features/document/DocConvert'
import { SettingsPage } from '@/features/settings/SettingsPage'
import { BatchPage } from '@/features/batch/BatchPage'
import { PipelineBuilder } from '@/features/pipeline/PipelineBuilder'
import { TaskCenter } from '@/features/taskcenter/TaskCenter'
import { FirstRunWizard } from '@/features/diagnostics/SelfCheckPanel'
import { ComingSoon } from '@/features/placeholder/ComingSoon'
import { useCorePhase } from '@/hooks/useCore'
import { useWorkspace } from '@/store/workspace'
import { useActiveJob, useHasRunningJob, useJobs } from '@/store/jobs'
import { cn } from '@/lib/utils'

export default function App(): React.JSX.Element {
  const [route, setRoute] = useState<RouteId>('dashboard')
  const { phase, message } = useCorePhase()

  const files = useWorkspace((s) => s.files)
  const addPaths = useWorkspace((s) => s.addPaths)
  const notice = useWorkspace((s) => s.notice)
  const setNotice = useWorkspace((s) => s.setNotice)

  const initJobs = useJobs((s) => s.init)
  const refreshJobs = useJobs((s) => s.refresh)
  const setDrawerOpen = useJobs((s) => s.setDrawerOpen)

  /* 订阅内核任务事件流。只在挂载时建立一次连接。 */
  useEffect(() => initJobs(), [initJobs])

  /* 内核就绪后补拉一次活跃任务 —— 渲染进程挂载时内核可能还在启动 */
  useEffect(() => {
    if (phase === 'ready' || phase === 'degraded') void refreshJobs()
  }, [phase, refreshJobs])

  /* 外部传入的文件：双击关联文件 / 命令行参数 /「发送到」菜单 */
  useEffect(
    () =>
      window.docforge.files.onOpened((paths) => {
        if (paths.length) void addPaths(paths)
      }),
    [addPaths]
  )

  /* 全局快捷键：Ctrl+O 打开文件 */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      const mod = e.ctrlKey || e.metaKey
      if (mod && e.key.toLowerCase() === 'o') {
        e.preventDefault()
        void window.docforge.files.pickFiles({ multi: true }).then((paths) => {
          if (paths.length) void addPaths(paths)
        })
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [addPaths])

  /* 导入提示 4 秒后自动消失 */
  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(null), 4000)
    return () => clearTimeout(timer)
  }, [notice, setNotice])

  const navItem = NAV_GROUPS.flatMap((g) => g.items).find((i) => i.id === route)

  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-abyss">
      {/* 背景层：网格地平线 + 极光光斑 + 粒子场，三层叠加出"深空科技"观感 */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="grid-floor absolute inset-0 opacity-[0.55]" />
        <div className="absolute -top-40 -left-32 h-[520px] w-[520px] rounded-full bg-aurora-cyan/8 blur-[130px]" />
        <div className="absolute -right-40 -bottom-52 h-[560px] w-[560px] rounded-full bg-aurora-violet/8 blur-[140px]" />
        <ParticleField className="absolute inset-0 h-full w-full opacity-70" />
        <div className="anim-scan absolute inset-x-0 top-0 h-px bg-linear-to-r from-transparent via-aurora-cyan/35 to-transparent" />
      </div>

      <TitleBar phase={phase} phaseMessage={message} />

      <div className="relative z-10 flex min-h-0 flex-1">
        <Sidebar active={route} onSelect={setRoute} fileCount={files.length} />

        <main className="flex min-w-0 flex-1 flex-col">
          {/*
            这里刻意**不用** AnimatePresence 做路由切换。
            `mode="wait"` 会让新页面必须等旧页面的退出动画播完才挂载，而退出动画依赖
            requestAnimationFrame —— 一旦窗口被遮挡/最小化，Chromium 会把页面标记为
            hidden 并暂停 rAF，动画永远播不完，界面就"点不动了"（实测确实发生过）。
            只做入场动画即可：视觉几乎一样，但切换不再依赖任何可被节流的东西。
          */}
          <motion.div
            key={route}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
            className="flex min-h-0 flex-1 flex-col"
          >
            {route === 'dashboard' ? (
              <Dashboard phase={phase} phaseMessage={message} />
            ) : route === 'image-watermark' ? (
              <ImageWatermark />
            ) : route === 'ocr' ? (
              <OcrWorkbench actionId="image.ocr" />
            ) : route === 'image-to-excel' ? (
              <OcrWorkbench actionId="image.to_excel" />
            ) : route === 'settings' ? (
              <SettingsPage />
            ) : route === 'pdf-watermark' ? (
              <PdfWatermark />
            ) : route === 'toolbox' ? (
              <PdfToolbox />
            ) : route === 'pdf-word' ? (
              <DocConvert />
            ) : route === 'batch' ? (
              <BatchPage />
            ) : route === 'pipeline' ? (
              <PipelineBuilder />
            ) : (
              <ComingSoon
                label={navItem?.label ?? route}
                hint={navItem?.hint ?? ''}
                milestone={navItem?.milestone ?? null}
                onBack={() => setRoute('dashboard')}
              />
            )}
          </motion.div>
        </main>
      </div>

      <StatusBar onOpenTaskCenter={() => setDrawerOpen(true)} />

      <TaskCenter />
      <DropOverlay />
      {/* 首次启动的环境自检向导 */}
      <FirstRunWizard />

      {/* 轻量提示条。同样不依赖退出动画 —— rAF 被暂停时它会一直挂着 */}
      {notice && (
        <motion.div
          initial={{ opacity: 0, y: 16, scale: 0.97 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          transition={{ type: 'spring', stiffness: 380, damping: 30 }}
          className="glass absolute bottom-11 left-1/2 z-40 -translate-x-1/2 px-4 py-2 text-[12px] text-ink-2"
        >
          {notice}
        </motion.div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 底部状态条                                                          */
/* ------------------------------------------------------------------ */

function StatusBar({ onOpenTaskCenter }: { onOpenTaskCenter: () => void }): React.JSX.Element {
  const job = useActiveJob()
  const running = useHasRunningJob()
  const files = useWorkspace((s) => s.files.length)
  const streaming = useJobs((s) => s.streaming)

  return (
    <footer className="relative z-30 flex h-8 shrink-0 items-center gap-3 border-t border-hairline/50 bg-void/70 px-3.5 backdrop-blur-xl">
      <span className="flex items-center gap-1.5 text-[10.5px] text-ink-4">
        <Activity size={11} className={cn(streaming ? 'text-ok' : 'text-idle')} />
        {streaming ? '事件流已连接' : '事件流断开'}
      </span>

      <span className="h-3 w-px bg-hairline/60" />

      <span className="text-[10.5px] text-ink-4">
        工作区 <span className="font-mono text-ink-2">{files}</span> 个文件
      </span>

      {job && (
        <>
          <span className="h-3 w-px bg-hairline/60" />
          <button
            type="button"
            onClick={onOpenTaskCenter}
            className="group flex min-w-0 items-center gap-2 rounded px-1.5 py-0.5 transition-colors hover:bg-panel-2/70"
          >
            <span className="relative flex h-1.5 w-1.5 shrink-0">
              {running && (
                <span className="anim-pulse-ring absolute inset-0 rounded-full bg-run" />
              )}
              <span
                className={cn(
                  'relative h-1.5 w-1.5 rounded-full',
                  running ? 'bg-run' : job.failedTasks > 0 ? 'bg-warn' : 'bg-ok'
                )}
              />
            </span>
            <span className="truncate text-[10.5px] text-ink-2">{job.actionLabel}</span>
            <span className="font-mono text-[10.5px] text-ink-4">
              {job.completedTasks}/{job.totalTasks}
            </span>

            {/* 行内细进度条 */}
            <span className="h-[3px] w-24 overflow-hidden rounded-full bg-hairline/60">
              <motion.span
                className="block h-full rounded-full bg-linear-to-r from-aurora-cyan to-aurora-violet"
                animate={{ width: `${job.progress}%` }}
                transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
              />
            </span>

            <ChevronUp
              size={12}
              className="shrink-0 text-ink-4 transition-colors group-hover:text-aurora-cyan"
            />
          </button>
        </>
      )}

      <button
        type="button"
        onClick={onOpenTaskCenter}
        className="ml-auto flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10.5px] text-ink-4 transition-colors hover:bg-panel-2/70 hover:text-ink-2"
      >
        <Layers size={11} />
        任务中心
      </button>
    </footer>
  )
}
