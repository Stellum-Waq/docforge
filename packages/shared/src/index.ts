/**
 * @docforge/shared — 前端 / Electron 主进程 / Python 内核三方共享的类型契约。
 *
 * 约定的原则：
 *  1. 这里的类型是「唯一事实来源」，Python 侧用 pydantic 模型镜像同一份结构。
 *  2. 任何跨进程传输的数据必须能被 JSON 序列化。
 *  3. snake_case 只出现在 Python 内部；跨进程一律 camelCase。
 *  4. 渲染进程**永远拿不到内核 Token** —— 所有内核调用都经主进程 IPC 转发，
 *     这样即使渲染层被注入也无法直接访问内核接口。
 */

/* ------------------------------------------------------------------ */
/* 内核健康与能力探测                                                    */
/* ------------------------------------------------------------------ */

export type CorePhase = 'starting' | 'ready' | 'degraded' | 'stopped' | 'error'

export interface CoreHealth {
  phase: CorePhase
  version: string
  pid: number
  uptimeSec: number
  platform: string
  python: string
  jobsHandled: number
  message?: string
}

/** 单个转换引擎的可用性。 */
export interface EngineProbe {
  id: string
  label: string
  domain: 'office' | 'pdf' | 'image' | 'ocr' | 'archive'
  available: boolean
  /** 是否为该能力域的首选引擎（优先级最高的可用引擎） */
  preferred: boolean
  /** 保真度评分 0-100，用于路由排序 */
  fidelity: number
  detail?: string
  /** 不可用的原因，便于设置页展示"为什么不能用" */
  reason?: string
}

export interface SystemCapabilities {
  os: { platform: string; release: string; arch: string; hostname: string }
  cpu: { cores: number; physicalCores: number; model: string }
  memory: { totalBytes: number; availableBytes: number }
  disk: { outputFreeBytes: number }
  engines: EngineProbe[]
  /** 本地离线 OCR 是否就绪（对应需求 8 的"保留本地部署选项"） */
  localOcrReady: boolean
  /** 云端 DeepSeek Vision 是否已配置密钥 */
  cloudOcrConfigured: boolean
  probedAt: string
  /** 内核建议的默认输出目录（用户可在界面上覆盖） */
  defaultOutputDir: string
  /** 内核的数据目录（任务历史、缓存、预设都在这） */
  dataDir: string
}

/* ------------------------------------------------------------------ */
/* 文件与任务模型                                                       */
/* ------------------------------------------------------------------ */

export interface WorkspaceFile {
  id: string
  /** 磁盘绝对路径（Electron 侧用 webUtils.getPathForFile 取得） */
  path: string
  name: string
  /** 小写扩展名，不含点 */
  ext: string
  sizeBytes: number
  kind: FileKind
  addedAt: string
  /** 若来自文件夹递归展开，记录相对路径以支持保留目录结构 */
  relativePath?: string
}

export type FileKind = 'image' | 'pdf' | 'word' | 'excel' | 'ppt' | 'text' | 'archive' | 'unknown'

export type JobStatus =
  | 'queued'
  | 'running'
  | 'paused'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'skipped'

/** 一个批次任务（用户点一次"开始"产生一个 Job） */
export interface Job {
  id: string
  action: string
  actionLabel: string
  status: JobStatus
  createdAt: string
  startedAt?: string | null
  finishedAt?: string | null
  totalTasks: number
  completedTasks: number
  failedTasks: number
  /** 0-100，用于进度环 */
  progress: number
  outputDir: string
  error?: string | null
  concurrency: number
}

/** Job 下的单个文件处理任务 */
export interface JobTask {
  id: string
  jobId: string
  filePath: string
  fileName: string
  status: JobStatus
  progress: number
  durationMs?: number | null
  outputPath?: string | null
  error?: string | null
  message?: string
}

/* ------------------------------------------------------------------ */
/* 动作（能力的统一抽象）                                                */
/* ------------------------------------------------------------------ */

/**
 * 一个可执行的动作。
 *
 * 所有功能（图片水印、PDF 转 Word、图片转 Excel …）都表达为动作，
 * 因此 UI 可以读取 paramsSchema 自动生成参数表单，新增功能不用改前端。
 */
export interface ActionInfo {
  id: string
  label: string
  domain: string
  description: string
  /** 接受的扩展名（小写，不含点）；空数组表示接受任意文件 */
  accepts: string[]
  /** 固定输出扩展名；null 表示跟随源文件 */
  outputExt: string | null
  /**
   * 聚合动作：把整批文件当作**一个**输入，只产出一个结果（例如 PDF 合并）。
   * 界面据此改变交互，而不是硬编码动作 id 来判断。
   */
  aggregate: boolean
  /** JSON Schema，用于生成参数表单 */
  paramsSchema: JsonSchema
}

export interface JsonSchema {
  type?: string
  title?: string
  description?: string
  default?: unknown
  enum?: unknown[]
  minimum?: number
  maximum?: number
  properties?: Record<string, JsonSchema>
  required?: string[]
  [key: string]: unknown
}

/* ------------------------------------------------------------------ */
/* 任务请求与事件                                                       */
/* ------------------------------------------------------------------ */

export interface CreateJobRequest {
  action: string
  files: string[]
  outputDir?: string
  params?: Record<string, unknown>
  concurrency?: number
  suffix?: string
  conflictPolicy?: 'rename' | 'overwrite' | 'skip'
  preserveTree?: boolean
  rootDir?: string | null
}

export interface CreateJobResponse {
  job: Job
  tasks: JobTask[]
}

/** 内核通过 SSE 推来的事件，经主进程转发给渲染进程 */
export type JobEvent =
  | { type: 'ready'; ok: boolean }
  | { type: 'job.created'; job: Job }
  | { type: 'job.updated'; job: Job }
  | { type: 'job.finished'; job: Job }
  | { type: 'task.updated'; jobId: string; task: JobTask; job: Job }
  | { type: 'log'; jobId: string; taskId: string; fileName: string; line: string }

export interface JobHistoryEntry extends Job {
  startedAt?: string | null
  finishedAt?: string | null
}

export interface CacheStats {
  entries: number
  hits: number
}

/* ------------------------------------------------------------------ */
/* 设置与 OCR 引擎                                                      */
/* ------------------------------------------------------------------ */

export interface OcrEngineStatus {
  id: string
  label: string
  /** 是否完全离线（离线引擎可在断网与隐私敏感场景使用） */
  offline: boolean
  /** 保真度 0-100 */
  fidelity: number
  available: boolean
  reason?: string | null
}

export interface AppSettings {
  /** 已掩码的密钥，**永远不会是明文** */
  deepseekApiKeyMasked: string
  hasApiKey: boolean
  cloudOcrEnabled: boolean
  /** 命中这些通配符的文件强制走本地引擎，不上传 */
  sensitivePatterns: string[]
  /** 当前平台是否支持加密存储（Windows DPAPI） */
  encryptionAvailable: boolean
  cache: CacheStats
}

/** 云端识别的累计用量与估算费用 */
export interface OcrUsage {
  calls: number
  promptTokens: number
  completionTokens: number
  estimatedCostUsd: number
}

export interface OcrEngineReport {
  engines: OcrEngineStatus[]
  cloudEnabled: boolean
  hasApiKey: boolean
}

/** 单张试识别的结果 */
export interface OcrTestResult {
  ok: boolean
  engine: string
  text: string
  truncated: boolean
  lineCount: number
  width: number
  height: number
  tiles: number
  elapsedMs: number
  averageConfidence: number
  warnings: string[]
  usage: Record<string, unknown>
  tables: unknown[]
}

export interface OcrTestRequest {
  filePath: string
  engine?: 'local' | 'cloud' | 'auto'
  mode?: 'text' | 'layout' | 'table' | 'formula' | 'card'
  tiling?: 'auto' | 'off' | 'always'
  detail?: string
  language?: string
  maxChars?: number
}

/* ------------------------------------------------------------------ */
/* PDF                                                                */
/* ------------------------------------------------------------------ */

export interface PdfPageInfo {
  page: number
  width: number
  height: number
  rotation: number
  /** 文字层几乎为空且含图片，通常说明是扫描件（需要先做 OCR） */
  likelyScanned: boolean
}

export interface PdfMeta {
  pageCount: number
  pages: PdfPageInfo[]
}

/* ------------------------------------------------------------------ */
/* 系统自检                                                            */
/* ------------------------------------------------------------------ */

export type CheckStatus = 'ok' | 'warn' | 'fail'

export interface SelfCheckItem {
  id: string
  label: string
  status: CheckStatus
  detail: string
  /** 该检查项不通过时对用户意味着什么 */
  impact?: string | null
}

export interface SelfCheckReport {
  items: SelfCheckItem[]
  okCount: number
  warnCount: number
  failCount: number
  /** 关键项无失败即视为具备完成全部核心功能的条件 */
  ready: boolean
}

export interface UpdateSettingsBody {
  /** 留空表示不修改（避免误清空） */
  deepseekApiKey?: string
  cloudOcrEnabled?: boolean
  sensitivePatterns?: string[]
  clearApiKey?: boolean
}

/**
 * 参数预设：把一套调好的参数存下来复用。
 *
 * 预设绑定到具体的动作（`action`）—— 跨动作套用参数没有意义，
 * 图片水印的 `opacity` 放到 PDF 水印里什么都不是。
 */
export interface Preset {
  id: string
  /** 动作 id，例如 image.watermark */
  action: string
  name: string
  params: Record<string, unknown>
  createdAt: string
  updatedAt: string
}

export interface SavePresetBody {
  action: string
  name: string
  params: Record<string, unknown>
}

/* ------------------------------------------------------------------ */
/* IPC 契约                                                           */
/* ------------------------------------------------------------------ */

export interface CoreConnection {
  connected: boolean
  /** Python 内核监听端口，仅绑定 127.0.0.1。仅用于诊断，业务代码不应直接使用 */
  port?: number
  error?: string
}

export interface FileFilter {
  name: string
  extensions: string[]
}

/** preload 通过 contextBridge 暴露给渲染进程的完整 API 面 */
export interface DocForgeBridge {
  app: {
    getVersion(): Promise<string>
    getPlatform(): Promise<string>
  }
  window: {
    minimize(): void
    maximize(): void
    close(): void
    isMaximized(): Promise<boolean>
    onMaximizeChange(cb: (maximized: boolean) => void): () => void
  }
  core: {
    connection(): Promise<CoreConnection>
    health(): Promise<CoreHealth>
    capabilities(): Promise<SystemCapabilities>
    restart(): Promise<CoreConnection>
    onLog(cb: (line: string) => void): () => void
    onPhaseChange(cb: (phase: CorePhase, message?: string) => void): () => void
  }
  actions: {
    /** 列出内核注册的全部动作（用于自动生成功能入口与参数表单） */
    list(): Promise<ActionInfo[]>
  }
  presets: {
    /** 不带 action 时返回全部预设 */
    list(action?: string): Promise<Preset[]>
    /** 同名（同一动作下）即覆盖，返回落库后的记录 */
    save(body: SavePresetBody): Promise<Preset>
    remove(presetId: string): Promise<void>
  }
  jobs: {
    create(request: CreateJobRequest): Promise<CreateJobResponse>
    get(jobId: string): Promise<CreateJobResponse | null>
    listActive(): Promise<Job[]>
    cancel(jobId: string): Promise<Job | null>
    pause(jobId: string): Promise<Job | null>
    resume(jobId: string): Promise<Job | null>
    history(limit?: number): Promise<{ jobs: JobHistoryEntry[]; cache: CacheStats }>
    historyTasks(jobId: string): Promise<JobTask[]>
    clearHistory(): Promise<void>
    /** 订阅任务事件流（自动重连）；返回取消订阅函数 */
    onEvent(cb: (event: JobEvent) => void): () => void
  }
  preview: {
    /**
     * 渲染参数预览，返回可直接用于 <img src> 的 data URL。
     * 预览由内核用**与真实输出相同的代码**渲染，确保所见即所得。
     */
    imageWatermark(payload: {
      filePath: string
      params: Record<string, unknown>
      maxWidth?: number
    }): Promise<string | null>
    /** 对单张图片试识别，返回文本与元信息（不产出文件） */
    ocrTest(payload: OcrTestRequest): Promise<OcrTestResult>
    /** 获取原图缩略图（data URL）。绕开渲染进程无法直接加载 file:// 的限制 */
    thumbnail(payload: { filePath: string; maxWidth?: number }): Promise<string | null>
    /** PDF 页数与各页尺寸，供界面画页选择器 */
    pdfMeta(payload: { filePath: string }): Promise<PdfMeta>
    /** 渲染"已加水印的某一页"（与正式输出同一套代码，确保所见即所得） */
    pdfWatermark(payload: {
      filePath: string
      page: number
      params: Record<string, unknown>
      maxWidth?: number
    }): Promise<string | null>
  }
  settings: {
    get(): Promise<AppSettings>
    update(body: UpdateSettingsBody): Promise<AppSettings>
    ocrEngines(): Promise<OcrEngineReport>
    ocrUsage(): Promise<{ usage: OcrUsage; cache: CacheStats }>
    resetOcrUsage(): Promise<void>
    clearOcrCache(): Promise<void>
    /** 用极小请求验证 API Key 是否真的可用 */
    testOcr(): Promise<{ ok: boolean; message: string }>
  }
  system: {
    /**
     * 环境自检：这台机器上哪些能力可用、缺什么、缺了会有什么影响。
     * 首次启动会展示一次，之后可在设置页随时重跑。
     */
    selfCheck(): Promise<SelfCheckReport>
  }
  files: {
    pickFiles(options?: { filters?: FileFilter[]; multi?: boolean }): Promise<string[]>
    pickDirectory(): Promise<string | null>
    expandPaths(paths: string[]): Promise<WorkspaceFile[]>
    revealInExplorer(path: string): Promise<void>
    /** 拖拽得到的 File 对象 → 绝对路径（Electron 32+ 必须走 webUtils） */
    pathForFile(file: File): string
    /**
     * 订阅外部传入的文件：双击关联文件、命令行参数、「发送到」菜单。
     * 返回取消订阅函数。
     */
    onOpened(cb: (paths: string[]) => void): () => void
  }
  shell: {
    openPath(path: string): Promise<void>
  }
}
