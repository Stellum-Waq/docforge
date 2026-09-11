import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion } from 'motion/react'
import {
  AlertCircle,
  FileText,
  Info,
  Layers,
  Loader2,
  Play,
  Stamp,
  Wand2
} from 'lucide-react'
import type { PdfMeta } from '@shared/index'
import { cn } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  ColorInput,
  Field,
  ProgressBar,
  Segmented,
  Select,
  Slider,
  Switch,
  TextInput
} from '@/components/ui'
import { PositionPicker } from '@/components/PositionPicker'
import { useWorkspace } from '@/store/workspace'
import { useJobs } from '@/store/jobs'
import { useCapabilities } from '@/hooks/useCore'

/**
 * PDF 水印参数。snake_case 直接对应内核动作的 params_schema —— 多一层命名
 * 映射就多一处能对不上的地方，而对齐由测试保证。
 */
interface PdfWatermarkParams {
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
  pages: string
  overlay: boolean
  unique_per_file: boolean
  metadata_mark: boolean
  metadata_text: string
}

const DEFAULTS: PdfWatermarkParams = {
  mode: 'text',
  text: '机密',
  image_path: '',
  font_size_ratio: 0.08,
  scale_ratio: 0.28,
  color: '#C81E1E',
  opacity: 0.32,
  rotation: -35,
  position: 'center',
  x_ratio: 0.5,
  y_ratio: 0.5,
  margin_ratio: 0.05,
  tile_gap_ratio: 0.6,
  stroke: true,
  stroke_color: '#000000',
  stroke_width: 0.6,
  pages: 'all',
  overlay: true,
  unique_per_file: false,
  metadata_mark: false,
  metadata_text: '内部文件'
}

const PREVIEW_DEBOUNCE_MS = 260
const PREVIEW_MAX_WIDTH = 820

export function PdfWatermark(): React.JSX.Element {
  const files = useWorkspace((s) => s.files)
  const addPaths = useWorkspace((s) => s.addPaths)
  const createJob = useJobs((s) => s.create)
  const caps = useCapabilities(true)

  const [params, setParams] = useState<PdfWatermarkParams>(DEFAULTS)
  const [suffix, setSuffix] = useState('_水印')
  const [concurrency, setConcurrency] = useState(4)
  const [outputDir, setOutputDir] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const pdfs = useMemo(() => files.filter((f) => f.ext === 'pdf'), [files])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const sample = useMemo(
    () => pdfs.find((f) => f.id === selectedId) ?? pdfs[0] ?? null,
    [pdfs, selectedId]
  )

  const set = useCallback(<K extends keyof PdfWatermarkParams>(key: K, value: PdfWatermarkParams[K]) => {
    setParams((prev) => ({ ...prev, [key]: value }))
  }, [])

  /* ---------------- PDF 元信息 ---------------- */

  const [meta, setMeta] = useState<PdfMeta | null>(null)
  const [page, setPage] = useState(1)
  const [metaError, setMetaError] = useState<string | null>(null)

  useEffect(() => {
    if (!sample) {
      setMeta(null)
      return
    }
    let alive = true
    setMetaError(null)
    void window.docforge.preview
      .pdfMeta({ filePath: sample.path })
      .then((data) => {
        if (!alive) return
        setMeta(data)
        setPage((current) => Math.min(Math.max(1, current), Math.max(1, data.pageCount)))
      })
      .catch((err: Error) => {
        if (alive) {
          setMetaError(err.message)
          setMeta(null)
        }
      })
    return () => {
      alive = false
    }
  }, [sample])

  /* ---------------- 实时预览 ---------------- */

  const [preview, setPreview] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const requestSeq = useRef(0)

  useEffect(() => {
    if (!sample || !meta) return
    const seq = ++requestSeq.current
    const timer = setTimeout(() => {
      setPreviewing(true)
      window.docforge.preview
        .pdfWatermark({ filePath: sample.path, page, params: params as unknown as Record<string, unknown>, maxWidth: PREVIEW_MAX_WIDTH })
        .then((url) => {
          // 丢弃过期响应，否则快速拖动滑块时预览会倒退
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
  }, [sample, page, meta, params])

  /* ---------------- 拖拽定位 ---------------- */

  const imageRef = useRef<HTMLImageElement>(null)
  const [dragging, setDragging] = useState(false)

  const updateFromPointer = useCallback((clientX: number, clientY: number) => {
    const img = imageRef.current
    if (!img) return
    const rect = img.getBoundingClientRect()
    if (rect.width === 0 || rect.height === 0) return
    const x = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    const y = Math.min(1, Math.max(0, (clientY - rect.top) / rect.height))
    setParams((prev) => ({ ...prev, position: 'custom', x_ratio: Number(x.toFixed(4)), y_ratio: Number(y.toFixed(4)) }))
  }, [])

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

  /* ---------------- 提交 ---------------- */

  const start = async (): Promise<void> => {
    if (pdfs.length === 0) return
    setSubmitting(true)
    try {
      await createJob({
        action: 'pdf.watermark',
        files: pdfs.map((f) => f.path),
        outputDir: outputDir.trim() || caps.data?.defaultOutputDir || undefined,
        params: params as unknown as Record<string, unknown>,
        suffix,
        concurrency
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

  if (pdfs.length === 0) {
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
              <FileText size={24} className="text-aurora-cyan" strokeWidth={1.6} />
            </div>
          </div>
          <h2 className="text-[16px] font-semibold text-ink">还没有 PDF 文件</h2>
          <p className="mt-1 text-[12px] text-ink-3">把 PDF 拖到窗口任意位置，或点击下面的按钮选择</p>
          <Button
            variant="primary"
            className="mt-4"
            onClick={() => {
              void window.docforge.files.pickFiles({ multi: true }).then((paths) => {
                if (paths.length) void addPaths(paths)
              })
            }}
          >
            选择 PDF
          </Button>
        </motion.div>
      </div>
    )
  }

  const scanned = meta?.pages.filter((p) => p.likelyScanned).length ?? 0

  return (
    <div className="flex min-h-0 flex-1">
      {/* ============ 左侧：预览 ============ */}
      <div className="flex min-w-0 flex-1 flex-col p-5">
        <div className="mb-3 flex items-center gap-2.5">
          <Stamp size={15} className="text-aurora-cyan" strokeWidth={1.9} />
          <h2 className="text-[15px] font-semibold text-ink">PDF 加水印</h2>
          <Badge tone="aurora">{pdfs.length} 个文件</Badge>
          {meta && <Badge tone="idle">{meta.pageCount} 页</Badge>}
          {previewing && (
            <span className="flex items-center gap-1.5 text-[11px] text-ink-4">
              <Loader2 size={11} className="animate-spin" />
              渲染预览
            </span>
          )}
        </div>

        <div className="glass relative flex min-h-0 flex-1 items-center justify-center overflow-hidden p-4">
          <div className="grid-floor pointer-events-none absolute inset-0 opacity-25" />

          {preview ? (
            <img
              ref={imageRef}
              src={preview}
              alt="PDF 水印预览"
              draggable={false}
              onPointerDown={(e) => {
                if (params.position !== 'custom') return
                e.preventDefault()
                setDragging(true)
                updateFromPointer(e.clientX, e.clientY)
              }}
              className={cn(
                'relative max-h-full max-w-full rounded object-contain shadow-2xl ring-1 ring-hairline/60 select-none',
                params.position === 'custom' && 'cursor-crosshair'
              )}
              style={{ touchAction: 'none' }}
            />
          ) : (
            <div className="relative flex flex-col items-center gap-3 text-center">
              {metaError || previewError ? (
                <>
                  <AlertCircle size={26} className="text-warn" strokeWidth={1.6} />
                  <div className="max-w-sm text-[12px] text-warn">{metaError ?? previewError}</div>
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

        {/* 页码选择 */}
        {meta && (
          <div className="mt-3 flex items-center gap-3">
            <span className="shrink-0 text-[11.5px] text-ink-3">预览页</span>
            <input
              type="range"
              min={1}
              max={Math.max(1, meta.pageCount)}
              value={page}
              onChange={(e) => setPage(Number(e.target.value))}
              className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-panel-3
                [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5
                [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full
                [&::-webkit-slider-thumb]:border [&::-webkit-slider-thumb]:border-aurora-cyan
                [&::-webkit-slider-thumb]:bg-panel-2 [&::-webkit-slider-thumb]:shadow-[0_0_10px_var(--color-aurora-cyan)]"
            />
            <span className="w-16 shrink-0 text-right font-mono text-[11px] text-ink-2">
              {page} / {meta.pageCount}
            </span>
          </div>
        )}

        <div className="mt-3 flex items-center gap-2.5">
          <span className="shrink-0 text-[11.5px] text-ink-3">样本</span>
          <div className="flex min-w-0 flex-1 gap-1.5 overflow-x-auto pb-1">
            {pdfs.slice(0, 12).map((file) => (
              <button
                key={file.id}
                type="button"
                onClick={() => {
                  setSelectedId(file.id)
                  setPage(1)
                }}
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
          </div>

          <Button variant="primary" size="lg" disabled={submitting} onClick={() => void start()}>
            {submitting ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
            批量处理 {pdfs.length} 个
          </Button>
        </div>

        {scanned > 0 && (
          <div className="mt-2 flex items-start gap-1.5 text-[10.5px] text-warn/85">
            <Info size={11} className="mt-0.5 shrink-0" />
            检测到 {scanned} 页疑似扫描件（几乎没有文字层）。如果需要在扫描件上叠加水印，效果是正常的；
            但如果要提取文字，请先用「文字提取」做 OCR。
          </div>
        )}
      </div>

      {/* ============ 右侧：参数 ============ */}
      <aside className="w-[336px] shrink-0 overflow-y-auto border-l border-hairline bg-abyss p-4">
        <div className="space-y-3.5">
          <Card title="水印内容" icon={<Stamp size={13} strokeWidth={1.9} />}>
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
                  <Field label="水印文字" hint="支持 {page} {pages} {filename} {date}">
                    <TextInput value={params.text} onChange={(v) => set('text', v)} placeholder="机密" />
                  </Field>
                  <Field label="字号" hint="占页面短边比例">
                    <Slider
                      value={params.font_size_ratio}
                      min={0.02}
                      max={0.3}
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
                      <TextInput value={params.image_path} onChange={(v) => set('image_path', v)} placeholder="选择图片" mono />
                      <Button size="sm" className="shrink-0" onClick={() => void pickLogo()}>
                        选择
                      </Button>
                    </div>
                  </Field>
                  <Field label="Logo 宽度" hint="占页面短边比例">
                    <Slider
                      value={params.scale_ratio}
                      min={0.05}
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

          <Card title="位置与范围" icon={<Wand2 size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <PositionPicker value={params.position} onChange={(v) => set('position', v)} tileLabel="平铺整页" />

              {params.position === 'tile' && (
                <Field label="平铺间距">
                  <Slider
                    value={params.tile_gap_ratio}
                    min={0.05}
                    max={2}
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
                    <Slider value={params.x_ratio} min={0} max={1} step={0.01} onChange={(v) => set('x_ratio', v)} format={(v) => v.toFixed(2)} />
                  </Field>
                  <Field label="Y 位置">
                    <Slider value={params.y_ratio} min={0} max={1} step={0.01} onChange={(v) => set('y_ratio', v)} format={(v) => v.toFixed(2)} />
                  </Field>
                </div>
              )}

              <Field label="页码范围" hint="all / 1-3 / 1,5,7 / 2-">
                <TextInput value={params.pages} onChange={(v) => set('pages', v)} mono />
              </Field>
            </div>
          </Card>

          <Card title="外观" icon={<Layers size={13} strokeWidth={1.9} />}>
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
              {params.mode === 'text' && (
                <>
                  <Field label="颜色">
                    <ColorInput value={params.color} onChange={(v) => set('color', v)} />
                  </Field>
                  <Switch
                    checked={params.stroke}
                    onChange={(v) => set('stroke', v)}
                    label="文字描边"
                    hint="在深浅背景上都更清晰"
                  />
                  {params.stroke && (
                    <Field label="描边">
                      <ColorInput value={params.stroke_color} onChange={(v) => set('stroke_color', v)} />
                    </Field>
                  )}
                </>
              )}
            </div>
          </Card>

          <Card title="输出与批处理" icon={<FileText size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <Field label="输出目录" hint="留空使用默认目录">
                <div className="flex gap-2">
                  <TextInput
                    value={outputDir}
                    onChange={setOutputDir}
                    placeholder={caps.data?.defaultOutputDir ?? '默认输出目录'}
                    mono
                  />
                  <Button
                    size="sm"
                    className="shrink-0"
                    onClick={() => {
                      void window.docforge.files.pickDirectory().then((dir) => {
                        if (dir) setOutputDir(dir)
                      })
                    }}
                  >
                    选择
                  </Button>
                </div>
              </Field>

              <Field label="文件名后缀">
                <TextInput value={suffix} onChange={setSuffix} />
              </Field>

              <Switch
                checked={params.overlay}
                onChange={(v) => set('overlay', v)}
                label="覆盖在正文之上"
                hint="关闭则作为底纹放在正文下方，不遮挡内容"
              />
              <Switch
                checked={params.unique_per_file}
                onChange={(v) => set('unique_per_file', v)}
                label="启用模板变量"
                hint="每页可生成不同文字，如「第{page}页」，便于逐页追溯"
              />
              <Switch
                checked={params.metadata_mark}
                onChange={(v) => set('metadata_mark', v)}
                label="写入元数据水印"
                hint="在 PDF 元数据中留下标记，不可见但可溯源"
              />
              {params.metadata_mark && (
                <Field label="元数据内容">
                  <TextInput value={params.metadata_text} onChange={(v) => set('metadata_text', v)} />
                </Field>
              )}

              <div>
                <div className="mb-1.5 flex items-baseline justify-between">
                  <span className="text-[11.5px] font-medium text-ink-2">并发数</span>
                  <span className="font-mono text-[11px] text-ink-4">{concurrency}</span>
                </div>
                <ProgressBar value={(concurrency / 16) * 100} />
                <Select
                  value={String(concurrency)}
                  onChange={(v) => setConcurrency(Number(v))}
                  options={[1, 2, 4, 6, 8, 12, 16].map((n) => ({
                    value: String(n),
                    label: `${n} 个文件同时处理`
                  }))}
                />
              </div>
            </div>
          </Card>

          <div className="rounded-lg border border-hairline-2 bg-panel-2 px-3 py-2.5 text-[10.5px] leading-relaxed text-ink-4">
            <div className="mb-1 font-medium text-ink-3">关于水印文字</div>
            水印是以**矢量文字**写入的，因此任意放大都清晰，而且仍然可以被复制与检索 ——
            如果渲染成图片贴上去，用户就没法选中它了。
          </div>
        </div>
      </aside>
    </div>
  )
}
