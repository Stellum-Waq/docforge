import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { EventEmitter } from 'node:events'
import { join } from 'node:path'
import { streamSse } from './sse'
import type { CoreConnection, CoreHealth, CorePhase, SystemCapabilities } from '@shared/index'

/** Python 内核启动就绪后，会向 stdout 打印这一行握手信息 */
const READY_MARKER = '@@DOCFORGE_READY@@'
/** 内核启动超时（毫秒）。首次冷启动要加载 PyMuPDF 等重依赖，给足余量。 */
const STARTUP_TIMEOUT_MS = 45_000
/** 健康探测轮询间隔 */
const HEALTH_POLL_MS = 400

/**
 * 便携版：把用户数据放在 exe 旁边，而不是 `%APPDATA%`。
 *
 * electron-builder 的 portable target 启动时会设置 `PORTABLE_EXECUTABLE_DIR`
 * 指向 exe 所在目录。不处理这个变量的话，"便携版"仍然会把任务历史、参数预设、
 * 结果缓存写进**当前这台机器**的 `%APPDATA%\DocForge` —— 放在 U 盘里换台机器，
 * 自己的预设和历史就带不走了，与"便携"的预期不符。
 *
 * 之所以要真的写一个探针文件：放在只读介质（光盘、只读 U 盘）或
 * `Program Files` 下时目录是建不出来的。宁可"不够便携"退回默认位置，
 * 也不能让应用因为写不了数据目录而起不来。
 */
function resolvePortableDataDir(): string | null {
  const portableDir = process.env['PORTABLE_EXECUTABLE_DIR']
  if (!portableDir) return null

  const target = join(portableDir, 'DocForge数据')
  const probe = join(target, '.writable-probe')
  try {
    mkdirSync(target, { recursive: true })
    writeFileSync(probe, '')
    rmSync(probe, { force: true })
    return target
  } catch {
    return null
  }
}

/** 便携模式下解析出来的数据目录；非便携版为 null */
const portableDataDir = resolvePortableDataDir()

export interface CoreProcessOptions {
  /** 仓库根目录 */
  repoRoot: string
  /** 是否为开发模式（决定用 venv 里的 python 还是打包后的可执行文件） */
  isDev: boolean
  /** 打包后内核可执行文件所在目录 */
  resourcesPath?: string
}

/**
 * 管理 Python 内核子进程的完整生命周期。
 *
 * 设计要点：
 *  - 内核只监听 127.0.0.1 的随机端口，并通过每次启动随机生成的 Bearer Token 鉴权，
 *    因此本机其他程序无法直接访问（对应设计文档 §2.4 安全要求）。
 *  - 启动失败不阻塞 UI：进入 'error' 阶段，前端展示可读错误并允许重试。
 *  - stdout/stderr 全量转发为 'log' 事件，任务中心可以直接显示内核日志。
 */
export class CoreProcess extends EventEmitter {
  private proc: ChildProcessWithoutNullStreams | null = null
  private port: number | null = null
  private token: string | null = null
  private phase: CorePhase = 'stopped'
  private startError: string | null = null
  private starting: Promise<CoreConnection> | null = null
  private stopping = false
  private readonly logRing: string[] = []
  /** 事件流的中断句柄。内核重启时旧流必须显式关掉，否则会泄漏连接 */
  private sseAbort: AbortController | null = null
  private sseTask: Promise<void> | null = null

  constructor(private readonly opts: CoreProcessOptions) {
    super()
  }

  /* ---------------------------------------------------------------- */
  /* 状态查询                                                          */
  /* ---------------------------------------------------------------- */

  get currentPhase(): CorePhase {
    return this.phase
  }

  get connection(): CoreConnection {
    return {
      connected: this.phase === 'ready' || this.phase === 'degraded',
      port: this.port ?? undefined,
      // 刻意不把 token 暴露给渲染进程：所有内核调用都经主进程转发，
      // 渲染层即使被注入也拿不到访问内核的凭证（安全模型见设计文档 §2.4）
      error: this.startError ?? undefined
    }
  }

  get recentLogs(): readonly string[] {
    return this.logRing
  }

  /* ---------------------------------------------------------------- */
  /* 启动 / 停止                                                       */
  /* ---------------------------------------------------------------- */

  async start(): Promise<CoreConnection> {
    if (this.starting) return this.starting
    this.starting = this.doStart().finally(() => {
      this.starting = null
    })
    return this.starting
  }

  private resolveLaunch(): { command: string; args: string[]; cwd: string } {
    const coreDir = join(this.opts.repoRoot, 'core')

    if (!this.opts.isDev && this.opts.resourcesPath) {
      // 生产环境：PyInstaller 打出的 onedir 可执行文件
      const packaged = join(this.opts.resourcesPath, 'core', 'docforge-core.exe')
      if (existsSync(packaged)) {
        return { command: packaged, args: [], cwd: join(this.opts.resourcesPath, 'core') }
      }
    }

    // 开发环境：优先使用项目内 venv，保证依赖隔离
    const venvPython = join(coreDir, '.venv', 'Scripts', 'python.exe')
    const command = existsSync(venvPython) ? venvPython : 'python'
    return { command, args: ['-m', 'docforge'], cwd: coreDir }
  }

  private async doStart(): Promise<CoreConnection> {
    await this.stop()
    this.stopping = false
    this.setPhase('starting', '正在启动文档处理内核…')

    const { command, args, cwd } = this.resolveLaunch()

    let proc: ChildProcessWithoutNullStreams
    try {
      proc = spawn(command, args, {
        cwd,
        windowsHide: true,
        env: {
          ...process.env,
          PYTHONIOENCODING: 'utf-8',
          PYTHONUNBUFFERED: '1',
          PYTHONUTF8: '1',
          DOCFORGE_PARENT_PID: String(process.pid),
          // 开发期把数据目录留在仓库内，避免开发调试污染用户真实的
          // %APPDATA%\DocForge（任务历史、缓存、预设）。生产环境不设置，
          // 内核会走标准的用户数据目录。
          ...(this.opts.isDev ? { DOCFORGE_DATA_DIR: join(this.opts.repoRoot, '.docforge-data') } : {}),
          // 便携版：数据跟着 exe 走（见 resolvePortableDataDir 的说明）
          ...(!this.opts.isDev && portableDataDir ? { DOCFORGE_DATA_DIR: portableDataDir } : {})
        }
      }) as ChildProcessWithoutNullStreams
    } catch (err) {
      const message = `无法启动内核进程（${command}）：${(err as Error).message}`
      this.setPhase('error', message)
      this.startError = message
      return this.connection
    }

    this.proc = proc

    const ready = new Promise<{ port: number; token: string }>((resolve, reject) => {
      let stdoutBuf = ''
      const timer = setTimeout(() => {
        reject(new Error(`内核 ${STARTUP_TIMEOUT_MS / 1000}s 内未就绪。最近输出：\n${this.logRing.slice(-15).join('\n')}`))
      }, STARTUP_TIMEOUT_MS)

      const finish = (value: { port: number; token: string }): void => {
        clearTimeout(timer)
        resolve(value)
      }
      const fail = (err: Error): void => {
        clearTimeout(timer)
        reject(err)
      }

      proc.stdout.setEncoding('utf-8')
      proc.stdout.on('data', (chunk: string) => {
        stdoutBuf += chunk
        this.pushLog(chunk)
        let idx: number
        while ((idx = stdoutBuf.indexOf('\n')) >= 0) {
          const line = stdoutBuf.slice(0, idx).trim()
          stdoutBuf = stdoutBuf.slice(idx + 1)
          if (line.startsWith(READY_MARKER)) {
            try {
              finish(JSON.parse(line.slice(READY_MARKER.length).trim()))
            } catch (err) {
              fail(new Error(`握手信息解析失败：${(err as Error).message}`))
            }
          }
        }
      })

      proc.stderr.setEncoding('utf-8')
      proc.stderr.on('data', (chunk: string) => this.pushLog(chunk))

      proc.on('error', (err) => fail(new Error(`内核进程错误：${err.message}`)))
      proc.on('exit', (code, signal) => {
        if (!this.stopping) {
          fail(new Error(`内核进程意外退出（code=${code}, signal=${signal}）`))
        }
      })
    })

    try {
      const { port, token } = await ready
      this.port = port
      this.token = token
      this.startError = null

      // 进程退出时同步更新阶段，前端可据此提示"内核已断开"
      proc.on('exit', (code, signal) => {
        if (this.stopping) return
        this.port = null
        this.token = null
        this.setPhase('error', `内核进程已退出（code=${code}${signal ? `, signal=${signal}` : ''}）`)
      })

      // 真实探活一次，确认 HTTP 层也通了，而不是只拿到了端口号
      const health = await this.fetchJson<CoreHealth>('/api/health', { retries: 20, retryDelayMs: HEALTH_POLL_MS })
      this.setPhase('ready', `内核就绪 · ${health.python}`)
      this.startEventStream()
      return this.connection
    } catch (err) {
      const message = (err as Error).message
      this.startError = message
      this.setPhase('error', message)
      return this.connection
    }
  }

  async stop(): Promise<void> {
    this.stopEventStream()
    const proc = this.proc
    if (!proc || proc.exitCode !== null) {
      this.proc = null
      return
    }

    // 先请内核优雅退出。
    //
    // 这一步不是可有可无的客套：Windows 上 child.kill() 等价于 TerminateProcess，
    // Python 的清理代码**一行都不会执行**。用 Office COM 转换过文档的内核如果被
    // 直接杀掉，就会在用户机器上留下无主的 WINWORD.EXE —— 看不见窗口、占着内存，
    // 还可能锁住文档导致下次转换失败。实测遇到过。
    await this.requestGracefulShutdown()

    this.stopping = true
    this.proc = null
    this.port = null
    this.token = null

    await new Promise<void>((resolve) => {
      let settled = false
      const finish = (): void => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve()
      }

      // 优雅退出通常 1–2 秒内完成（要回收 Office 进程会久一点），给 8 秒
      const timer = setTimeout(() => {
        try {
          proc.kill()
        } catch {
          /* 已退出 */
        }
        finish()
      }, 8000)

      proc.once('exit', finish)
    })

    this.setPhase('stopped')
  }

  /** 请求内核优雅退出（回收 Office 进程等）。失败不影响后续强杀。 */
  private async requestGracefulShutdown(): Promise<void> {
    if (this.port === null || this.token === null) return
    try {
      await fetch(`http://127.0.0.1:${this.port}/api/shutdown`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.token}` },
        signal: AbortSignal.timeout(2500)
      })
    } catch {
      // 内核可能已经退出或尚未就绪，忽略即可
    }
  }

  async restart(): Promise<CoreConnection> {
    await this.stop()
    return this.start()
  }

  /* ---------------------------------------------------------------- */
  /* 与内核通信                                                        */
  /* ---------------------------------------------------------------- */

  /** 请求内核 HTTP 接口。token 自动注入，业务侧无需关心鉴权。 */
  async fetchJson<T>(path: string, opts?: { method?: string; body?: unknown; retries?: number; retryDelayMs?: number }): Promise<T> {
    const retries = opts?.retries ?? 0
    const delay = opts?.retryDelayMs ?? 300
    let lastErr: Error | null = null

    for (let attempt = 0; attempt <= retries; attempt++) {
      if (this.port === null || this.token === null) {
        lastErr = new Error('内核尚未就绪')
      } else {
        try {
          const res = await fetch(`http://127.0.0.1:${this.port}${path}`, {
            method: opts?.method ?? 'GET',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${this.token}`
            },
            body: opts?.body === undefined ? undefined : JSON.stringify(opts.body)
          })
          if (!res.ok) {
            const text = await res.text().catch(() => '')
            throw new Error(`内核返回 ${res.status}：${text.slice(0, 400)}`)
          }
          return (await res.json()) as T
        } catch (err) {
          lastErr = err as Error
        }
      }
      if (attempt < retries) await sleep(delay)
    }
    throw lastErr ?? new Error('未知错误')
  }

  /**
   * POST 一个 JSON 请求，把二进制响应转成 data URL 返回。
   *
   * 用于参数预览：内核渲染出 PNG，主进程编码成 data URL 交给渲染进程直接塞进
   * <img src>。这样渲染进程既不需要 Token，也不用处理 ArrayBuffer 跨 IPC 的
   * 序列化问题。
   */
  async fetchDataUrl(path: string, body: unknown): Promise<string | null> {
    if (this.port === null || this.token === null) return null

    const res = await fetch(`http://127.0.0.1:${this.port}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.token}`
      },
      body: JSON.stringify(body)
    })

    if (!res.ok) {
      // 预览失败通常是参数不合法，把内核给的可读原因原样抛出去让 UI 显示
      const text = await res.text().catch(() => '')
      let detail = text.slice(0, 300)
      try {
        detail = (JSON.parse(text) as { detail?: string }).detail ?? detail
      } catch {
        /* 非 JSON 响应，保留原文 */
      }
      throw new Error(detail || `预览失败：HTTP ${res.status}`)
    }

    const buffer = Buffer.from(await res.arrayBuffer())
    const mime = res.headers.get('Content-Type') ?? 'image/png'
    return `data:${mime};base64,${buffer.toString('base64')}`
  }

  /* ---------------------------------------------------------------- */
  /* 任务事件流（SSE → IPC）                                            */
  /* ---------------------------------------------------------------- */

  /**
   * 订阅内核的任务事件流并转发给渲染进程。
   *
   * 渲染进程拿不到 Token，因此事件必须经由主进程中转。断线后自动重连：
   * 内核重启、网络栈抖动都不该让进度条永久卡住。
   */
  private startEventStream(): void {
    if (this.sseTask) return
    this.sseAbort = new AbortController()
    this.sseTask = this.runEventLoop(this.sseAbort.signal).finally(() => {
      this.sseTask = null
    })
  }

  private stopEventStream(): void {
    this.sseAbort?.abort()
    this.sseAbort = null
    this.sseTask = null
  }

  private async runEventLoop(signal: AbortSignal): Promise<void> {
    let attempt = 0

    while (!signal.aborted) {
      const port = this.port
      const token = this.token
      if (port === null || token === null) return

      try {
        await streamSse(`http://127.0.0.1:${port}/api/jobs/events/stream`, token, signal, {
          onEvent: (payload) => this.emit('job-event', payload),
          onError: (err) => this.pushLog(`[docforge] 事件解析告警：${err.message}`)
        })
        attempt = 0 // 正常结束（例如内核重启）后重置退避
      } catch (err) {
        if (signal.aborted) return
        this.pushLog(`[docforge] 事件流断开：${(err as Error).message}`)
      }

      if (signal.aborted) return

      // 指数退避，上限 5s，避免内核没起来时疯狂重连
      attempt += 1
      const delay = Math.min(5000, 300 * 2 ** Math.min(attempt, 4))
      await sleep(delay)
    }
  }

  /* ---------------------------------------------------------------- */
  /* 内部工具                                                          */
  /* ---------------------------------------------------------------- */

  private setPhase(phase: CorePhase, message?: string): void {
    this.phase = phase
    this.emit('phase', phase, message)
  }

  private pushLog(chunk: string): void {
    for (const raw of chunk.split(/\r?\n/)) {
      const line = raw.trimEnd()
      if (!line) continue
      this.logRing.push(line)
      if (this.logRing.length > 500) this.logRing.shift()
      this.emit('log', line)
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
