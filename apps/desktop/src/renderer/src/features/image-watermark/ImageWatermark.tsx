import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'motion/react'
import {
  AlertCircle,
  Droplet,
  FolderOpen,
  Image as ImageIcon,
  Info,
  Loader2,
  Play,
  Settings2,
  Sparkles,
  Stamp,
  Wand2
} from 'lucide-react'
import type { ActionInfo } from '@shared/index'
import { cn } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  ColorInput,
  Field,
  Segmented,
  Select,
  Slider,
  Switch,
  TextInput
} from '@/components/ui'
import { useWorkspace } from '@/store/workspace'
import { useJobs } from '@/store/jobs'
import { PositionPicker } from '@/components/PositionPicker'
import { useCapabilities } from '@/hooks/useCore'

/**
 * 水印参数。
 *
 * 注意这里是 **snake_case** —— 这个对象会被原样发给 Python 内核的动作函数，
 * 直接对应 ActionSpec.params_schema 的字段名。刻意不做命名转换：
 * 多一层映射就多一处能对不上的地方，而参数 Schema 本身就是跨语言契约。
 */
export interface WatermarkParams {
  mode: 'text' | 'image'
  text: string
  image_path: string
  font_size_ratio: number
  scale_ratio: number
  color: string
  opacity: number
  rotation: number
  position: string
  x_ratio: number
  y_ratio: number
  margin_ratio: number
  tile_gap_ratio: number
  stroke: boolean
  stroke_color: string
  stroke_width: number
  shadow: boolean
  auto_orient: boolean
  keep_exif: boolean
  unique_per_file: boolean
  output_format: 'same' | 'jpg' | 'png' | 'webp'
  quality: number
}

const DEFAULT_PARAMS: WatermarkParams = {
  mode: 'text',
  text: '机密',
  image_path: '',
  font_size_ratio: 0.05,
  scale_ratio: 0.22,
  color: '#FFFFFF',
  opacity: 0.45,
  rotation: -30,
  position: 'bottom-right',
  x_ratio: 0.5,
  y_ratio: 0.5,
  margin_ratio: 0.03,
  tile_gap_ratio: 0.35,
  stroke: true,
  stroke_color: '#000000',
  stroke_width: 2,
  shadow: false,
  auto_orient: true,
  keep_exif: true,
  unique_per_file: false,
  output_format: 'same',
  quality: 92
}

const PREVIEW_MAX_WIDTH = 720
/** 预览防抖：调滑块时会连续触发，等用户停手 220ms 再渲染 */
const PREVIEW_DEBOUNCE_MS = 220

export function ImageWatermark(): React.JSX.Element {
  const files = useWorkspace((s) => s.files)
  const addPaths = useWorkspace((s) => s.addPaths)
  const createJob = useJobs((s) => s.create)

  const [params, setParams] = useState<WatermarkParams>(DEFAULT_PARAMS)
  const [preview, setPreview] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const [suffix, setSuffix] = useState('_水印')
  const [concurrency, setConcurrency] = useState(4)
  const [conflictPolicy, setConflictPolicy] = useState<'rename' | 'overwrite' | 'skip'>('rename')
  /** 留空表示使用内核建议的默认输出目录 */
  const [outputDir, setOutputDir] = useState('')

  const caps = useCapabilities(true)
  const effectiveOutputDir = outputDir.trim() || caps.data?.defaultOutputDir || ''

  // 从内核读取动作元数据，用它作为"支持哪些格式"的唯一事实来源
  const actionsQuery = useQuery<ActionInfo[]>({
    queryKey: ['actions'],
    queryFn: () => window.docforge.actions.list(),
    staleTime: 5 * 60_000
  })
  const actionSpec = useMemo(
    () => actionsQuery.data?.find((item) => item.id === 'image.watermark'),
    [actionsQuery.data]
  )

  /**
   * 已导入文件里能加水印的图片。
   *
   * **按动作声明的 accepts 过滤，不自己维护白名单。**
   * 之前这里硬编码了一份扩展名列表，漏掉了 heic/heif ——
   * 后端明明支持 iPhone 照片（还专门注册了 pillow-heif），
   * 界面却把它们当成"不是图片"，用户根本选不到。
   * 能力清单只应该有一个事实来源，那就是内核的动作元数据。
   */
  const images = useMemo(() => {
    const accepted = new Set(
      actionSpec?.accepts ?? ['jpg', 'jpeg', 'png', 'bmp', 'webp', 'tif', 'tiff', 'heic', 'heif']
    )
    return files.filter((f) => accepted.has(f.ext))
  }, [files, actionSpec])

  /** 预览用的样本图：优先用户选中的，否则用第一个 */
  const sample = useMemo(
    () => images.find((f) => f.id === selectedId) ?? images[0] ?? null,
    [images, selectedId]
  )

  const set = useCallback(<K extends keyof WatermarkParams>(key: K, value: WatermarkParams[K]) => {
    setParams((prev) => ({ ...prev, [key]: value }))
  }, [])

  /* ---------------- 实时预览（防抖） ---------------- */

  const requestSeq = useRef(0)

  useEffect(() => {
    if (!sample) {
      setPreview(null)
      return
    }

    // 预览用到的参数与真实任务完全一致，只是强制输出 PNG（在服务端处理）
    const payload = { ...params, __preview: true }
    const seq = ++requestSeq.current

    const timer = setTimeout(() => {
      setPreviewing(true)
      window.docforge.preview
        .imageWatermark({ filePath: sample.path, params: payload, maxWidth: PREVIEW_MAX_WIDTH })
        .then((url) => {
          // 丢弃过期响应，否则快速拖动滑块时会出现"预览倒退"
          if (seq !== requestSeq.current) return
          setPreview(url)
          setPreviewError(null)
        })
        .catch((err: Error) => {
          if (seq !== requestSeq.current) return
          setPreviewError(err.message)
          setPreview(null)
        })
        .finally(() => {
          if (seq === requestSeq.current) setPreviewing(false)
        })
    }, PREVIEW_DEBOUNCE_MS)

    return () => clearTimeout(timer)
  }, [params, sample])

  /* ---------------- 画布拖拽定位 ---------------- */

  const imageRef = useRef<HTMLImageElement>(null)
  const [dragging, setDragging] = useState(false)

  const updateFromPointer = useCallback(
    (clientX: number, clientY: number) => {
      const img = imageRef.current
      if (!img) return
      const rect = img.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) return
      const x = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
      const y = Math.min(1, Math.max(0, (clientY - rect.top) / rect.height))
      setParams((prev) => ({ ...prev, position: 'custom', x_ratio: Number(x.toFixed(4)), y_ratio: Number(y.toFixed(4)) }))
    },
    []
  )

  useEffect(() => {
    if (!dragging) return
    const onMove = (e: PointerEvent): void => updateFromPointer(e.clientX, e.clientY)
    const onUp = (): void => setDragging(false)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [dragging, updateFromPointer])

  /* ---------------- 提交任务 ---------------- */

  const [submitting, setSubmitting] = useState(false)

  const start = async (): Promise<void> => {
    if (images.length === 0) return
    setSubmitting(true)
    try {
      await createJob({
        action: 'image.watermark',
        files: images.map((f) => f.path),
        outputDir: effectiveOutputDir || undefined,
        params: params as unknown as Record<string, unknown>,
        suffix,
        concurrency,
        conflictPolicy
      })
    } finally {
      setSubmitting(false)
    }
  }

  const pickLogo = async (): Promise<void> => {
    const paths = await window.docforge.files.pickFiles({
      multi: false,
      filters: [{ name: '图片', extensions: ['png', 'jpg', 'jpeg', 'webp', 'bmp'] }]
    })
    if (paths[0]) set('image_path', paths[0])
  }

  /* ---------------- 渲染 ---------------- */

  if (images.length === 0) {
    return (
      <EmptyState
        onPick={() => {
          void window.docforge.files.pickFiles({ multi: true }).then((paths) => {
            if (paths.length) void addPaths(paths)
          })
        }}
      />
    )
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* ============ 左侧：预览 ============ */}
      <div className="flex min-w-0 flex-1 flex-col p-5">
        <div className="mb-3 flex items-center gap-2.5">
          <Stamp size={15} className="text-aurora-cyan" strokeWidth={1.9} />
          <h2 className="text-[15px] font-semibold text-ink">图片加水印</h2>
          <Badge tone="aurora">
            {images.length} 张待处理
          </Badge>
          {previewing && (
            <span className="flex items-center gap-1.5 text-[11px] text-ink-4">
              <Loader2 size={11} className="animate-spin" />
              渲染预览
            </span>
          )}
        </div>

        {/* 预览画布 */}
        <div className="glass relative flex min-h-0 flex-1 items-center justify-center overflow-hidden p-4">
          <div className="grid-floor pointer-events-none absolute inset-0 opacity-30" />

          {preview ? (
            <img
              ref={imageRef}
              src={preview}
              alt="水印预览"
              draggable={false}
              onPointerDown={(e) => {
                // 只在自定义定位模式下允许拖拽，避免误触打乱九宫格设置
                if (params.position !== 'custom' && params.position !== 'tile') return
                e.preventDefault()
                setDragging(true)
                updateFromPointer(e.clientX, e.clientY)
              }}
              className={cn(
                'relative max-h-full max-w-full rounded-lg object-contain shadow-2xl select-none',
                (params.position === 'custom' || params.position === 'tile') && 'cursor-crosshair'
              )}
              style={{ touchAction: 'none' }}
            />
          ) : (
            <div className="relative flex flex-col items-center gap-3 text-center">
              {previewError ? (
                <>
                  <AlertCircle size={26} className="text-warn" strokeWidth={1.6} />
                  <div className="max-w-sm text-[12px] text-warn">{previewError}</div>
                  <div className="text-[11px] text-ink-4">调整参数后会自动重试</div>
                </>
              ) : (
                <>
                  <Loader2 size={22} className="animate-spin text-aurora-cyan" />
                  <div className="text-[12px] text-ink-3">正在渲染预览…</div>
                </>
              )}
            </div>
          )}

          {dragging && (
            <div className="pointer-events-none absolute inset-x-0 bottom-3 text-center text-[11px] text-aurora-cyan">
              拖动以精确放置水印
            </div>
          )}
        </div>

        {/* 样本图切换 + 提交 */}
        <div className="mt-3 flex items-center gap-2.5">
          <span className="shrink-0 text-[11.5px] text-ink-3">预览样本</span>
          <div className="flex min-w-0 flex-1 gap-1.5 overflow-x-auto pb-1">
            {images.slice(0, 14).map((file) => (
              <button
                key={file.id}
                type="button"
                onClick={() => setSelectedId(file.id)}
                title={file.name}
                className={cn(
                  'shrink-0 rounded-md border px-2 py-1 text-[10.5px] transition-colors',
                  file.id === sample?.id
                    ? 'border-aurora-cyan/50 bg-aurora-cyan/12 text-aurora-cyan'
                    : 'border-hairline-2 text-ink-4 hover:border-hairline hover:text-ink-2'
                )}
              >
                <span className="block max-w-[110px] truncate">{file.name}</span>
              </button>
            ))}
            {images.length > 14 && (
              <span className="shrink-0 self-center text-[10.5px] text-ink-4">
                +{images.length - 14}
              </span>
            )}
          </div>

          <Button variant="primary" size="lg" disabled={submitting} onClick={() => void start()}>
            {submitting ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
            开始处理 {images.length} 张
          </Button>
        </div>

        <div className="mt-2 flex items-start gap-1.5 text-[10.5px] text-ink-4">
          <Info size={11} className="mt-0.5 shrink-0" />
          预览由内核用**与正式输出完全相同**的代码渲染，因此所见即所得。
          {params.position === 'custom' && ' 当前为自定义定位：在预览图上拖动即可精确放置。'}
        </div>
      </div>

      {/* ============ 右侧：参数 ============ */}
      <aside className="w-[336px] shrink-0 overflow-y-auto border-l border-hairline bg-abyss p-4">
        <div className="space-y-3.5">
          <Card title="水印内容" icon={<Droplet size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <Segmented
                value={params.mode}
                onChange={(v) => set('mode', v)}
                options={[
                  { value: 'text', label: '文字水印' },
                  { value: 'image', label: 'Logo 水印' }
                ]}
              />

              {params.mode === 'text' ? (
                <>
                  <Field label="水印文字" hint="支持 {filename} {date} {index}">
                    <TextInput value={params.text} onChange={(v) => set('text', v)} placeholder="机密" />
                  </Field>
                  <Field label="字号" hint="占图片短边比例">
                    <Slider
                      value={params.font_size_ratio}
                      min={0.01}
                      max={0.25}
                      step={0.005}
                      onChange={(v) => set('font_size_ratio', v)}
                      format={(v) => `${(v * 100).toFixed(1)}%`}
                    />
                  </Field>
                </>
              ) : (
                <>
                  <Field label="Logo 图片">
                    <div className="flex gap-2">
                      <TextInput
                        value={params.image_path}
                        onChange={(v) => set('image_path', v)}
                        placeholder="选择一张图片"
                        mono
                      />
                      <Button size="sm" onClick={() => void pickLogo()} className="shrink-0">
                        <ImageIcon size={13} />
                      </Button>
                    </div>
                  </Field>
                  <Field label="Logo 宽度" hint="占图片短边比例">
                    <Slider
                      value={params.scale_ratio}
                      min={0.03}
                      max={1}
                      step={0.01}
                      onChange={(v) => set('scale_ratio', v)}
                      format={(v) => `${(v * 100).toFixed(0)}%`}
                    />
                  </Field>
                </>
              )}
            </div>
          </Card>

          <Card title="位置" icon={<Wand2 size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <PositionPicker value={params.position} onChange={(v) => set('position', v)} />

              {params.position === 'tile' && (
                <Field label="平铺间距">
                  <Slider
                    value={params.tile_gap_ratio}
                    min={0.05}
                    max={1.5}
                    step={0.05}
                    onChange={(v) => set('tile_gap_ratio', v)}
                    format={(v) => `${(v * 100).toFixed(0)}%`}
                  />
                </Field>
              )}

              {params.position !== 'tile' && params.position !== 'custom' && (
                <Field label="边距">
                  <Slider
                    value={params.margin_ratio}
                    min={0}
                    max={0.2}
                    step={0.005}
                    onChange={(v) => set('margin_ratio', v)}
                    format={(v) => `${(v * 100).toFixed(1)}%`}
                  />
                </Field>
              )}

              {params.position === 'custom' && (
                <div className="grid grid-cols-2 gap-2.5">
                  <Field label="X 位置">
                    <Slider
                      value={params.x_ratio}
                      min={0}
                      max={1}
                      step={0.01}
                      onChange={(v) => set('x_ratio', v)}
                      format={(v) => v.toFixed(2)}
                    />
                  </Field>
                  <Field label="Y 位置">
                    <Slider
                      value={params.y_ratio}
                      min={0}
                      max={1}
                      step={0.01}
                      onChange={(v) => set('y_ratio', v)}
                      format={(v) => v.toFixed(2)}
                    />
                  </Field>
                </div>
              )}
            </div>
          </Card>

          <Card title="外观" icon={<Sparkles size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <Field label="不透明度">
                <Slider
                  value={params.opacity}
                  min={0.02}
                  max={1}
                  step={0.01}
                  onChange={(v) => set('opacity', v)}
                  format={(v) => `${(v * 100).toFixed(0)}%`}
                />
              </Field>
              <Field label="旋转角度">
                <Slider
                  value={params.rotation}
                  min={-180}
                  max={180}
                  step={1}
                  onChange={(v) => set('rotation', v)}
                  format={(v) => `${v}°`}
                />
              </Field>
              <Field label="颜色">
                <ColorInput value={params.color} onChange={(v) => set('color', v)} />
              </Field>

              <div className="space-y-0.5 border-t border-hairline pt-2">
                <Switch
                  checked={params.stroke}
                  onChange={(v) => set('stroke', v)}
                  label="文字描边"
                  hint="深浅背景上都更清晰"
                />
                {params.stroke && (
                  <div className="pl-1">
                    <ColorInput
                      value={params.stroke_color}
                      onChange={(v) => set('stroke_color', v)}
                    />
                  </div>
                )}
                <Switch checked={params.shadow} onChange={(v) => set('shadow', v)} label="投影阴影" />
              </div>
            </div>
          </Card>

          <Card title="输出与批处理" icon={<Settings2 size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <Field label="输出目录" hint="留空使用默认目录">
                <div className="flex gap-2">
                  <TextInput
                    value={effectiveOutputDir}
                    onChange={setOutputDir}
                    placeholder="默认输出目录"
                    mono
                  />
                  <Button
                    size="sm"
                    className="shrink-0"
                    title="选择输出目录"
                    onClick={() => {
                      void window.docforge.files.pickDirectory().then((dir) => {
                        if (dir) setOutputDir(dir)
                      })
                    }}
                  >
                    <FolderOpen size={13} />
                  </Button>
                </div>
              </Field>

              <div className="grid grid-cols-2 gap-2.5">
                <Field label="输出格式">
                  <Select
                    value={params.output_format}
                    onChange={(v) => set('output_format', v)}
                    options={[
                      { value: 'same', label: '保持原格式' },
                      { value: 'jpg', label: 'JPEG' },
                      { value: 'png', label: 'PNG' },
                      { value: 'webp', label: 'WebP' }
                    ]}
                  />
                </Field>
                <Field label="文件名后缀">
                  <TextInput value={suffix} onChange={setSuffix} />
                </Field>
              </div>

              {(params.output_format === 'jpg' || params.output_format === 'webp') && (
                <Field label="输出质量">
                  <Slider
                    value={params.quality}
                    min={40}
                    max={100}
                    step={1}
                    onChange={(v) => set('quality', v)}
                    format={(v) => String(v)}
                  />
                </Field>
              )}

              <div className="grid grid-cols-2 gap-2.5">
                <Field label="重名处理">
                  <Select
                    value={conflictPolicy}
                    onChange={setConflictPolicy}
                    options={[
                      { value: 'rename', label: '自动重命名' },
                      { value: 'overwrite', label: '覆盖原文件' },
                      { value: 'skip', label: '跳过已存在' }
                    ]}
                  />
                </Field>
                <Field label="并发数" hint="按 CPU 核心数调整">
                  <Slider
                    value={concurrency}
                    min={1}
                    max={16}
                    step={1}
                    onChange={setConcurrency}
                    format={(v) => String(v)}
                  />
                </Field>
              </div>

              <div className="space-y-0.5 border-t border-hairline pt-2">
                <Switch
                  checked={params.auto_orient}
                  onChange={(v) => set('auto_orient', v)}
                  label="按 EXIF 自动摆正"
                  hint="手机照片必备，否则水印会歪"
                />
                <Switch
                  checked={params.keep_exif}
                  onChange={(v) => set('keep_exif', v)}
                  label="保留 EXIF"
                  hint="关闭可去除拍摄时间与 GPS 等隐私信息"
                />
                <Switch
                  checked={params.unique_per_file}
                  onChange={(v) => set('unique_per_file', v)}
                  label="每张唯一水印"
                  hint="用模板变量生成不同文字，便于溯源"
                />
              </div>
            </div>
          </Card>
        </div>
      </aside>
    </div>
  )
}


/* ------------------------------------------------------------------ */
/* 空状态                                                              */
/* ------------------------------------------------------------------ */

function EmptyState({ onPick }: { onPick: () => void }): React.JSX.Element {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <motion.div
        initial={{ opacity: 0, scale: 0.97 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ type: 'spring', stiffness: 300, damping: 28 }}
        className="glass max-w-md px-8 py-9 text-center"
      >
        <div className="relative mx-auto mb-4 w-fit">
          <span className="anim-pulse-ring absolute inset-0 rounded-2xl bg-aurora-cyan/20" />
          <div className="glass-flat anim-float relative flex h-14 w-14 items-center justify-center rounded-2xl">
            <ImageIcon size={24} className="text-aurora-cyan" strokeWidth={1.6} />
          </div>
        </div>
        <h2 className="text-[16px] font-semibold text-ink">还没有图片</h2>
        <p className="mt-1 text-[12px] text-ink-3">
          把照片拖到窗口任意位置，或点击下面的按钮选择
        </p>
        <Button variant="primary" className="mt-4" onClick={onPick}>
          <ImageIcon size={14} />
          选择图片
        </Button>
      </motion.div>
    </div>
  )
}