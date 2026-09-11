import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import { existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { CoreProcess } from './core'
import { expandPaths } from './files'
import type {
  ActionInfo,
  CoreConnection,
  CoreHealth,
  CreateJobRequest,
  CreateJobResponse,
  FileFilter,
  Job,
  JobHistoryEntry,
  JobTask,
  Preset,
  SavePresetBody,
  SystemCapabilities,
  WorkspaceFile
} from '@shared/index'

const isDev = !app.isPackaged
/**
 * 仓库根目录。
 *
 * 注意路径层级：主进程被 electron-vite 打包到 `<repo>/apps/desktop/out/main/index.js`，
 * 因此 __dirname 是 out/main，需要向上 4 层才到仓库根。
 * 少算一层会导到 apps/ 目录，内核启动时找不到 core 包。
 */
const repoRoot = join(__dirname, '..', '..', '..', '..')

/**
 * 可选：开发期开启 Chrome DevTools Protocol 调试端口。
 *
 * `tools/cdp.mjs` 靠它做界面验证 —— 直接操作 DOM 比"截屏 + 模拟按键"可靠得多，
 * 而且绝不会把按键打进用户当时正在使用的其他程序。
 *
 * 只在显式设置 `DOCFORGE_DEBUG_PORT` 时开启，**打包版永远不会监听调试端口**。
 */
if (process.env['DOCFORGE_DEBUG_PORT']) {
  app.commandLine.appendSwitch('remote-debugging-port', process.env['DOCFORGE_DEBUG_PORT'])
}

let mainWindow: BrowserWindow | null = null
let core: CoreProcess | null = null
/** 渲染进程尚在启动时到达的内核事件需要缓存，等 did-finish-load 后重放 */
let pendingCoreEvents: Array<{ channel: string; args: unknown[] }> = []

/* ------------------------------------------------------------------ */
/* 窗口                                                               */
/* ------------------------------------------------------------------ */

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1080,
    minHeight: 680,
    show: false,
    // 无边框 + 自绘标题栏，这是"科技风"观感的前提
    frame: false,
    backgroundColor: '#05070D',
    titleBarStyle: 'hidden',
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
      // 渲染进程需要 fetch 本地内核，保持 webSecurity 开启但允许 127.0.0.1
      webSecurity: true
    }
  })

  win.once('ready-to-show', () => {
    win.show()
    pendingCoreEvents = pendingCoreEvents.filter((e) => {
      win.webContents.send(e.channel, ...e.args)
      return false
    })
  })

  const emitMaximize = (): void => win.webContents.send('window:maximize-change', win.isMaximized())
  win.on('maximize', emitMaximize)
  win.on('unmaximize', emitMaximize)

  // 外部链接一律交给系统浏览器，绝不在应用内打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  if (isDev && process.env['ELECTRON_RENDERER_URL']) {
    void win.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    void win.loadFile(join(__dirname, '../renderer/index.html'))
  }

  return win
}

function sendToRenderer(channel: string, ...args: unknown[]): void {
  if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.webContents.isLoading()) {
    mainWindow.webContents.send(channel, ...args)
  } else {
    pendingCoreEvents.push({ channel, args })
    if (pendingCoreEvents.length > 200) pendingCoreEvents.shift()
  }
}

/**
 * 从命令行参数里提取真实存在的文件/目录路径。
 *
 * 支撑三种入口（设计文档 §1-F）：
 *   1. 资源管理器「用文枢打开」/ 文件关联双击
 *   2. 命令行 `DocForge.exe a.pdf b.png`
 *   3. 「发送到」菜单
 *
 * **刻意不做位置切片**（曾经用 argv.slice(2) 判断"跳过应用路径"，结果踩了坑）：
 * 开发模式下 `electron <开关> . <文件>` 里 Chromium 开关的位置不固定，
 * 一旦切片错位，`.` 就会被当成待处理目录，**递归扫描整个项目目录**，
 * 用户会莫名其妙看到几百个文件被导入。这是个后果严重且很难排查的问题。
 *
 * 因此改为按语义判断：
 *   * 跳过开关（以 - 开头）
 *   * 跳过 Electron 的应用路径本身（app.getAppPath()，正好能覆盖开发模式的 "."）
 *   * 只接受真实存在的路径
 */
function extractPathsFromArgv(argv: string[]): string[] {
  const appPath = resolve(app.getAppPath())
  const seen = new Set<string>()
  const paths: string[] = []

  for (const arg of argv.slice(1)) {
    if (!arg || arg.startsWith('-')) continue

    let resolved: string
    try {
      resolved = resolve(arg)
    } catch {
      continue
    }

    if (resolved === appPath) continue
    if (seen.has(resolved)) continue
    if (!existsSync(resolved)) continue

    seen.add(resolved)
    paths.push(resolved)
  }

  return paths
}

/** 把外部传入的文件送进渲染进程的工作区 */
function openExternalPaths(argv: string[]): void {
  const paths = extractPathsFromArgv(argv)
  if (paths.length === 0) return
  sendToRenderer('files:opened', paths)
}

/* ------------------------------------------------------------------ */
/* IPC                                                               */
/* ------------------------------------------------------------------ */

function registerIpc(): void {
  /* --- 应用信息 --- */
  ipcMain.handle('app:version', () => app.getVersion())
  ipcMain.handle('app:platform', () => process.platform)

  /* --- 窗口控制 --- */
  ipcMain.on('window:minimize', () => mainWindow?.minimize())
  ipcMain.on('window:maximize', () => {
    if (!mainWindow) return
    mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize()
  })
  ipcMain.on('window:close', () => mainWindow?.close())
  ipcMain.handle('window:is-maximized', () => mainWindow?.isMaximized() ?? false)

  /* --- 内核 --- */
  ipcMain.handle('core:connection', (): CoreConnection => core?.connection ?? { connected: false })
  ipcMain.handle('core:health', async (): Promise<CoreHealth> => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson<CoreHealth>('/api/health')
  })
  ipcMain.handle('core:capabilities', async (): Promise<SystemCapabilities> => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson<SystemCapabilities>('/api/system/capabilities')
  })
  ipcMain.handle('core:restart', async (): Promise<CoreConnection> => {
    if (!core) throw new Error('内核未初始化')
    return core.restart()
  })

  /* --- 动作发现 --- */
  ipcMain.handle('actions:list', async (): Promise<ActionInfo[]> => {
    if (!core) return []
    try {
      const data = await core.fetchJson<{ actions: ActionInfo[] }>('/api/actions')
      return data.actions
    } catch {
      return []
    }
  })

  /* --- 参数预设 --- */
  // 与只读查询不同，保存/删除失败必须让用户知道（"以为存上了其实没存"
  // 会让人白白重填一遍参数），所以这几个不吞异常。
  ipcMain.handle('presets:list', async (_e, action?: string): Promise<Preset[]> => {
    if (!core) return []
    try {
      const query = action ? `?action=${encodeURIComponent(action)}` : ''
      const data = await core.fetchJson<{ presets: Preset[] }>(`/api/presets${query}`)
      return data.presets
    } catch {
      return []
    }
  })

  ipcMain.handle('presets:save', async (_e, body: SavePresetBody): Promise<Preset> => {
    if (!core) throw new Error('内核未初始化，无法保存预设')
    const data = await core.fetchJson<{ preset: Preset }>('/api/presets', {
      method: 'POST',
      body
    })
    return data.preset
  })

  ipcMain.handle('presets:remove', async (_e, presetId: string): Promise<void> => {
    if (!core) throw new Error('内核未初始化，无法删除预设')
    await core.fetchJson(`/api/presets/${presetId}`, { method: 'DELETE' })
  })

  /* --- 任务 --- */
  ipcMain.handle('jobs:create', async (_e, request: CreateJobRequest): Promise<CreateJobResponse> => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson<CreateJobResponse>('/api/jobs', { method: 'POST', body: request })
  })

  ipcMain.handle('jobs:get', async (_e, jobId: string): Promise<CreateJobResponse | null> => {
    if (!core) return null
    try {
      return await core.fetchJson<CreateJobResponse>(`/api/jobs/${jobId}`)
    } catch {
      // 内存中已被淘汰，交给历史接口兜底
      return null
    }
  })

  ipcMain.handle('jobs:list-active', async (): Promise<Job[]> => {
    if (!core) return []
    // 渲染进程挂载时内核可能还在启动。只读查询不该抛错 ——
    // 抛出去会变成渲染进程的未处理 rejection，用户看到控制台报警却不知所以。
    // 返回空列表，等内核就绪后由前端主动刷新即可。
    try {
      const data = await core.fetchJson<{ jobs: Job[] }>('/api/jobs')
      return data.jobs
    } catch {
      return []
    }
  })

  ipcMain.handle('jobs:cancel', async (_e, jobId: string): Promise<Job | null> => {
    if (!core) return null
    const data = await core.fetchJson<{ job: Job }>(`/api/jobs/${jobId}/cancel`, { method: 'POST' })
    return data.job
  })

  ipcMain.handle('jobs:pause', async (_e, jobId: string): Promise<Job | null> => {
    if (!core) return null
    const data = await core.fetchJson<{ job: Job }>(`/api/jobs/${jobId}/pause`, { method: 'POST' })
    return data.job
  })

  ipcMain.handle('jobs:resume', async (_e, jobId: string): Promise<Job | null> => {
    if (!core) return null
    const data = await core.fetchJson<{ job: Job }>(`/api/jobs/${jobId}/resume`, { method: 'POST' })
    return data.job
  })

  ipcMain.handle(
    'jobs:history',
    async (_e, limit = 50): Promise<{ jobs: JobHistoryEntry[]; cache: { entries: number; hits: number } }> => {
      if (!core) return { jobs: [], cache: { entries: 0, hits: 0 } }
      return core.fetchJson(`/api/jobs/history/list?limit=${encodeURIComponent(String(limit))}`)
    }
  )

  ipcMain.handle('jobs:history-tasks', async (_e, jobId: string): Promise<JobTask[]> => {
    if (!core) return []
    const data = await core.fetchJson<{ tasks: JobTask[] }>(`/api/jobs/history/${jobId}/tasks`)
    return data.tasks
  })

  ipcMain.handle('jobs:clear-history', async (): Promise<void> => {
    if (!core) return
    await core.fetchJson('/api/jobs/history', { method: 'DELETE' })
  })

  /* --- 参数预览（所见即所得：预览与输出走同一套内核代码） --- */
  ipcMain.handle(
    'preview:image-watermark',
    async (_e, payload: { filePath: string; params: Record<string, unknown>; maxWidth?: number }): Promise<string | null> => {
      if (!core) return null
      return core.fetchDataUrl('/api/preview/image-watermark', {
        filePath: payload.filePath,
        params: payload.params,
        maxWidth: payload.maxWidth ?? 720
      })
    }
  )

  /* --- 设置与 OCR 引擎 --- */
  ipcMain.handle('preview:thumbnail', async (_e, payload: { filePath: string; maxWidth?: number }) => {
    if (!core) return null
    return core.fetchDataUrl('/api/preview/thumbnail', {
      filePath: payload.filePath,
      maxWidth: payload.maxWidth ?? 480
    })
  })

  ipcMain.handle('preview:ocr-test', async (_e, payload: unknown) => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson('/api/preview/ocr-test', { method: 'POST', body: payload })
  })

  ipcMain.handle('preview:pdf-meta', async (_e, payload: unknown) => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson('/api/preview/pdf-meta', { method: 'POST', body: payload })
  })

  ipcMain.handle(
    'preview:pdf-watermark',
    async (_e, payload: { filePath: string; page: number; params: Record<string, unknown>; maxWidth?: number }) => {
      if (!core) return null
      return core.fetchDataUrl('/api/preview/pdf-watermark', {
        filePath: payload.filePath,
        page: payload.page,
        params: payload.params,
        maxWidth: payload.maxWidth ?? 900
      })
    }
  )

  ipcMain.handle('settings:get', async () => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson('/api/settings')
  })

  ipcMain.handle('settings:update', async (_e, body: unknown) => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson('/api/settings', { method: 'PUT', body })
  })

  ipcMain.handle('settings:ocr-engines', async () => {
    if (!core) {
      return { engines: [], cloudEnabled: false, hasApiKey: false }
    }
    try {
      return await core.fetchJson('/api/ocr/engines')
    } catch {
      // 内核未就绪时返回空列表，渲染层稍后会随 phase 变化重新拉取
      return { engines: [], cloudEnabled: false, hasApiKey: false }
    }
  })

  ipcMain.handle('settings:ocr-usage', async () => {
    if (!core) return { usage: { calls: 0, promptTokens: 0, completionTokens: 0, estimatedCostUsd: 0 }, cache: { entries: 0, hits: 0 } }
    return core.fetchJson('/api/ocr/usage')
  })

  ipcMain.handle('settings:reset-ocr-usage', async (): Promise<void> => {
    if (!core) return
    await core.fetchJson('/api/ocr/usage/reset', { method: 'POST' })
  })

  ipcMain.handle('settings:clear-ocr-cache', async (): Promise<void> => {
    if (!core) return
    await core.fetchJson('/api/ocr/cache', { method: 'DELETE' })
  })

  ipcMain.handle('settings:test-ocr', async () => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson('/api/ocr/test', { method: 'POST' })
  })

  /* --- 系统自检 --- */
  ipcMain.handle('system:selfcheck', async () => {
    if (!core) throw new Error('内核未初始化')
    return core.fetchJson('/api/system/selfcheck')
  })

  /* --- 文件 --- */
  ipcMain.handle('files:pick', async (_e, options?: { filters?: FileFilter[]; multi?: boolean }): Promise<string[]> => {
    const result = await dialog.showOpenDialog(mainWindow ?? undefined!, {
      properties: options?.multi === false ? ['openFile'] : ['openFile', 'multiSelections'],
      filters: options?.filters?.length ? options.filters : [{ name: '所有文件', extensions: ['*'] }]
    })
    return result.canceled ? [] : result.filePaths
  })

  ipcMain.handle('files:pick-directory', async (): Promise<string | null> => {
    const result = await dialog.showOpenDialog(mainWindow ?? undefined!, {
      properties: ['openDirectory', 'createDirectory']
    })
    return result.canceled || !result.filePaths[0] ? null : result.filePaths[0]
  })

  ipcMain.handle('files:expand', async (_e, paths: string[]): Promise<WorkspaceFile[]> => expandPaths(paths))

  ipcMain.handle('files:reveal', async (_e, path: string): Promise<void> => {
    shell.showItemInFolder(path)
  })

  /* --- 系统 --- */
  ipcMain.handle('shell:open-path', async (_e, path: string): Promise<void> => {
    await shell.openPath(path)
  })
}

/* ------------------------------------------------------------------ */
/* 启动                                                               */
/* ------------------------------------------------------------------ */

// 单实例锁：文件关联双击时把参数转交给已有窗口，而不是开第二个应用
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', (_event, argv) => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
    // 第二次双击关联文件时，把文件交给已有窗口处理，而不是开第二个应用
    openExternalPaths(argv)
  })

  void app.whenReady().then(async () => {
    registerIpc()
    mainWindow = createWindow()

    core = new CoreProcess({
      repoRoot,
      isDev,
      resourcesPath: process.resourcesPath
    })
    core.on('log', (line: string) => sendToRenderer('core:log', line))
    core.on('phase', (phase: string, message?: string) => sendToRenderer('core:phase', phase, message))
    // 任务进度事件：内核 SSE → 主进程 → 渲染进程
    core.on('job-event', (payload: unknown) => sendToRenderer('jobs:event', payload))

    // 不 await：UI 先显示，内核在后台启动，就绪后通过事件通知前端。
    // 这样即便内核启动失败，用户也能看到界面和可读的错误原因（设计文档 §4 风险对策）。
    void core.start()

    // 启动时若带文件参数（双击关联文件 / 命令行），在窗口加载完成后送入工作区
    if (mainWindow) {
      mainWindow.webContents.once('did-finish-load', () => openExternalPaths(process.argv))
    }

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) mainWindow = createWindow()
    })
  })

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit()
  })

  // 退出前务必回收 Python 子进程，避免残留孤儿进程
  let cleanupDone = false
  app.on('before-quit', (event) => {
    if (cleanupDone || !core) return
    event.preventDefault()
    void core.stop().finally(() => {
      cleanupDone = true
      app.quit()
    })
  })
}
