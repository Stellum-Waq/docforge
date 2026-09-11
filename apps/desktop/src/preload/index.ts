import { contextBridge, ipcRenderer, webUtils } from 'electron'
import type {
  ActionInfo,
  AppSettings,
  CacheStats,
  CoreConnection,
  CoreHealth,
  CorePhase,
  CreateJobRequest,
  CreateJobResponse,
  DocForgeBridge,
  FileFilter,
  Job,
  JobEvent,
  JobHistoryEntry,
  JobTask,
  OcrEngineReport,
  OcrTestRequest,
  OcrTestResult,
  OcrUsage,
  PdfMeta,
  Preset,
  SavePresetBody,
  SelfCheckReport,
  SystemCapabilities,
  UpdateSettingsBody,
  WorkspaceFile
} from '@shared/index'

/** 订阅辅助：返回取消订阅函数，避免渲染进程重渲染时事件监听器泄漏 */
function subscribe<T extends unknown[]>(channel: string, cb: (...args: T) => void): () => void {
  const listener = (_e: unknown, ...args: unknown[]): void => cb(...(args as T))
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

const bridge: DocForgeBridge = {
  app: {
    getVersion: () => ipcRenderer.invoke('app:version') as Promise<string>,
    getPlatform: () => ipcRenderer.invoke('app:platform') as Promise<string>
  },

  window: {
    minimize: () => ipcRenderer.send('window:minimize'),
    maximize: () => ipcRenderer.send('window:maximize'),
    close: () => ipcRenderer.send('window:close'),
    isMaximized: () => ipcRenderer.invoke('window:is-maximized') as Promise<boolean>,
    onMaximizeChange: (cb) => subscribe<[boolean]>('window:maximize-change', cb)
  },

  core: {
    connection: () => ipcRenderer.invoke('core:connection') as Promise<CoreConnection>,
    health: () => ipcRenderer.invoke('core:health') as Promise<CoreHealth>,
    capabilities: () => ipcRenderer.invoke('core:capabilities') as Promise<SystemCapabilities>,
    restart: () => ipcRenderer.invoke('core:restart') as Promise<CoreConnection>,
    onLog: (cb) => subscribe<[string]>('core:log', cb),
    onPhaseChange: (cb) => subscribe<[CorePhase, string?]>('core:phase', cb)
  },

  actions: {
    list: () => ipcRenderer.invoke('actions:list') as Promise<ActionInfo[]>
  },

  presets: {
    list: (action?: string) => ipcRenderer.invoke('presets:list', action) as Promise<Preset[]>,
    save: (body: SavePresetBody) => ipcRenderer.invoke('presets:save', body) as Promise<Preset>,
    remove: (presetId: string) => ipcRenderer.invoke('presets:remove', presetId) as Promise<void>
  },

  jobs: {
    create: (request: CreateJobRequest) =>
      ipcRenderer.invoke('jobs:create', request) as Promise<CreateJobResponse>,
    get: (jobId: string) =>
      ipcRenderer.invoke('jobs:get', jobId) as Promise<CreateJobResponse | null>,
    listActive: () => ipcRenderer.invoke('jobs:list-active') as Promise<Job[]>,
    cancel: (jobId: string) => ipcRenderer.invoke('jobs:cancel', jobId) as Promise<Job | null>,
    pause: (jobId: string) => ipcRenderer.invoke('jobs:pause', jobId) as Promise<Job | null>,
    resume: (jobId: string) => ipcRenderer.invoke('jobs:resume', jobId) as Promise<Job | null>,
    history: (limit = 50) =>
      ipcRenderer.invoke('jobs:history', limit) as Promise<{
        jobs: JobHistoryEntry[]
        cache: { entries: number; hits: number }
      }>,
    historyTasks: (jobId: string) => ipcRenderer.invoke('jobs:history-tasks', jobId) as Promise<JobTask[]>,
    clearHistory: () => ipcRenderer.invoke('jobs:clear-history') as Promise<void>,
    onEvent: (cb) => subscribe<[JobEvent]>('jobs:event', cb)
  },

  preview: {
    imageWatermark: (payload: { filePath: string; params: Record<string, unknown>; maxWidth?: number }) =>
      ipcRenderer.invoke('preview:image-watermark', payload) as Promise<string | null>,
    ocrTest: (payload: OcrTestRequest) =>
      ipcRenderer.invoke('preview:ocr-test', payload) as Promise<OcrTestResult>,
    thumbnail: (payload: { filePath: string; maxWidth?: number }) =>
      ipcRenderer.invoke('preview:thumbnail', payload) as Promise<string | null>,
    pdfMeta: (payload: { filePath: string }) =>
      ipcRenderer.invoke('preview:pdf-meta', payload) as Promise<PdfMeta>,
    pdfWatermark: (payload: {
      filePath: string
      page: number
      params: Record<string, unknown>
      maxWidth?: number
    }) => ipcRenderer.invoke('preview:pdf-watermark', payload) as Promise<string | null>
  },

  settings: {
    get: () => ipcRenderer.invoke('settings:get') as Promise<AppSettings>,
    update: (body: UpdateSettingsBody) =>
      ipcRenderer.invoke('settings:update', body) as Promise<AppSettings>,
    ocrEngines: () => ipcRenderer.invoke('settings:ocr-engines') as Promise<OcrEngineReport>,
    ocrUsage: () =>
      ipcRenderer.invoke('settings:ocr-usage') as Promise<{ usage: OcrUsage; cache: CacheStats }>,
    resetOcrUsage: () => ipcRenderer.invoke('settings:reset-ocr-usage') as Promise<void>,
    clearOcrCache: () => ipcRenderer.invoke('settings:clear-ocr-cache') as Promise<void>,
    testOcr: () =>
      ipcRenderer.invoke('settings:test-ocr') as Promise<{ ok: boolean; message: string }>
  },

  system: {
    selfCheck: () => ipcRenderer.invoke('system:selfcheck') as Promise<SelfCheckReport>
  },

  files: {
    pickFiles: (options?: { filters?: FileFilter[]; multi?: boolean }) =>
      ipcRenderer.invoke('files:pick', options) as Promise<string[]>,
    pickDirectory: () => ipcRenderer.invoke('files:pick-directory') as Promise<string | null>,
    expandPaths: (paths: string[]) => ipcRenderer.invoke('files:expand', paths) as Promise<WorkspaceFile[]>,
    revealInExplorer: (path: string) => ipcRenderer.invoke('files:reveal', path) as Promise<void>,
    /**
     * Electron 32+ 已移除 File.path。webUtils 只能在渲染进程（含 preload）使用，
     * 因此这里直接把 File 对象转成磁盘绝对路径 —— 这是拖拽导入功能的关键一环。
     */
    pathForFile: (file: File): string => {
      try {
        return webUtils.getPathForFile(file)
      } catch {
        return ''
      }
    },
    onOpened: (cb) => subscribe<[string[]]>('files:opened', cb)
  },

  shell: {
    openPath: (path: string) => ipcRenderer.invoke('shell:open-path', path) as Promise<void>
  }
}

contextBridge.exposeInMainWorld('docforge', bridge)
