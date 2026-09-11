import { create } from 'zustand'
import type { CreateJobRequest, Job, JobEvent, JobTask } from '@shared/index'

export interface JobLogEntry {
  id: string
  jobId: string
  fileName: string
  line: string
  at: number
}

interface JobsState {
  /** jobId → Job */
  jobs: Record<string, Job>
  /** 展示顺序（新的在下），与 jobs 解耦以便自由排序 */
  order: string[]
  /** jobId → 该任务的各文件状态 */
  tasks: Record<string, JobTask[]>
  logs: JobLogEntry[]
  activeJobId: string | null
  drawerOpen: boolean
  /** 事件流是否已连接（用于顶栏指示灯） */
  streaming: boolean

  init(): () => void
  /** 从内核重新拉取当前活跃任务（内核就绪后调用，补上启动期间的竞态） */
  refresh(): Promise<void>
  create(request: CreateJobRequest): Promise<string | null>
  cancel(jobId: string): Promise<void>
  pause(jobId: string): Promise<void>
  resume(jobId: string): Promise<void>
  setActive(jobId: string | null): void
  setDrawerOpen(open: boolean): void
  clearFinished(): void
}

const MAX_LOGS = 500

/**
 * 任务状态仓库。
 *
 * 状态**只来自内核事件流**，不做乐观更新：进度条如果先自己猜一个数、随后被真实值
 * 覆盖，会出现"来回跳"的观感。这里宁可让进度晚 80ms 出现，也要保证它单调、真实。
 *
 * 唯一例外是 create()：为了立刻把抽屉弹出来，会先插入一个占位 Job，
 * 等 job.created 事件到达后由真实数据覆盖（id 相同，不会重复）。
 */
export const useJobs = create<JobsState>((set, get) => ({
  jobs: {},
  order: [],
  tasks: {},
  logs: [],
  activeJobId: null,
  drawerOpen: false,
  streaming: false,

  init() {
    // 渲染进程挂载时内核可能还没起来，因此先尝试一次、并在内核就绪后由
    // App 调用 refresh() 补拉。这样刷新/崩溃恢复后仍能看到正在跑的任务。
    void get().refresh()

    set({ streaming: true })

    return window.docforge.jobs.onEvent((event: JobEvent) => {
      switch (event.type) {
        case 'ready':
          set({ streaming: true })
          break

        case 'job.created':
        case 'job.updated':
        case 'job.finished': {
          const job = event.job
          set((s) => ({
            jobs: { ...s.jobs, [job.id]: job },
            order: s.order.includes(job.id) ? s.order : [...s.order, job.id],
            // 新任务出现时自动选中并打开抽屉，用户不需要去找进度在哪
            activeJobId: s.activeJobId ?? job.id
          }))
          break
        }

        case 'task.updated': {
          const { jobId, task, job } = event
          set((s) => {
            const list = s.tasks[jobId] ? [...s.tasks[jobId]] : []
            const index = list.findIndex((t) => t.id === task.id)
            if (index >= 0) list[index] = task
            else list.push(task)
            return {
              tasks: { ...s.tasks, [jobId]: list },
              jobs: { ...s.jobs, [jobId]: job }
            }
          })
          break
        }

        case 'log': {
          const entry: JobLogEntry = {
            id: `${event.taskId}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            jobId: event.jobId,
            fileName: event.fileName,
            line: event.line,
            at: Date.now()
          }
          set((s) => ({ logs: [...s.logs, entry].slice(-MAX_LOGS) }))
          break
        }
      }
    })
  },

  async refresh() {
    try {
      const jobs = await window.docforge.jobs.listActive()
      for (const job of jobs) {
        set((s) => ({
          jobs: { ...s.jobs, [job.id]: job },
          order: s.order.includes(job.id) ? s.order : [...s.order, job.id]
        }))
        const detail = await window.docforge.jobs.get(job.id)
        if (detail) set((s) => ({ tasks: { ...s.tasks, [job.id]: detail.tasks } }))
      }
    } catch {
      // 内核尚未就绪时静默忽略，等 phase=ready 时会再调一次
    }
  },

  async create(request) {
    try {
      const { job, tasks } = await window.docforge.jobs.create(request)
      set((s) => ({
        jobs: { ...s.jobs, [job.id]: job },
        tasks: { ...s.tasks, [job.id]: tasks },
        order: s.order.includes(job.id) ? s.order : [...s.order, job.id],
        activeJobId: job.id,
        drawerOpen: true
      }))
      return job.id
    } catch (err) {
      // 创建失败（例如没有可处理的文件）必须让用户看见原因
      const entry: JobLogEntry = {
        id: `err-${Date.now()}`,
        jobId: 'local',
        fileName: '创建任务失败',
        line: (err as Error).message,
        at: Date.now()
      }
      set((s) => ({ logs: [...s.logs, entry].slice(-MAX_LOGS) }))
      return null
    }
  },

  async cancel(jobId) {
    const job = await window.docforge.jobs.cancel(jobId)
    if (job) set((s) => ({ jobs: { ...s.jobs, [jobId]: job } }))
  },

  async pause(jobId) {
    const job = await window.docforge.jobs.pause(jobId)
    if (job) set((s) => ({ jobs: { ...s.jobs, [jobId]: job } }))
  },

  async resume(jobId) {
    const job = await window.docforge.jobs.resume(jobId)
    if (job) set((s) => ({ jobs: { ...s.jobs, [jobId]: job } }))
  },

  setActive(jobId) {
    set({ activeJobId: jobId })
  },

  setDrawerOpen(open) {
    set({ drawerOpen: open })
  },

  clearFinished() {
    const { jobs, order } = get()
    const keep = order.filter((id) => {
      const status = jobs[id]?.status
      return status === 'running' || status === 'queued' || status === 'paused'
    })
    set({ order: keep })
  }
}))

/** 当前活跃任务（供底部状态条使用） */
export function useActiveJob(): Job | null {
  return useJobs((s) => (s.activeJobId ? (s.jobs[s.activeJobId] ?? null) : null))
}

/** 是否已有任务在跑 —— 用于限制重复提交 */
export function useHasRunningJob(): boolean {
  return useJobs((s) =>
    Object.values(s.jobs).some((j) => j.status === 'running' || j.status === 'queued')
  )
}
