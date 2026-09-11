import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { FileStack, FolderOpen, UploadCloud } from 'lucide-react'
import { useWorkspace } from '@/store/workspace'
import { formatBytes } from '@/lib/utils'

interface DragStat {
  count: number
  bytes: number
}

/**
 * 全局拖拽导入层（对应需求 7）。
 *
 * 三个必须处理好的细节：
 *  1. dragenter/dragleave 会在子元素之间反复冒泡，单纯用布尔量会导致遮罩闪烁，
 *     因此用进入/离开计数器的经典解法。
 *  2. 必须在 dragover 上 preventDefault，否则浏览器会拒收 drop。
 *  3. Electron 里如果放任默认行为，拖入文件会让整个窗口"导航"到该文件，
 *     把应用界面顶掉 —— 所以 dragover/drop 都要阻止默认行为。
 *
 * 路径获取走 webUtils.getPathForFile（Electron 32+ 已移除 File.path）。
 */
export function DropOverlay(): React.JSX.Element {
  const [active, setActive] = useState(false)
  const [stat, setStat] = useState<DragStat>({ count: 0, bytes: 0 })
  const depth = useRef(0)
  const addPaths = useWorkspace((s) => s.addPaths)

  const reset = useCallback(() => {
    depth.current = 0
    setActive(false)
    setStat({ count: 0, bytes: 0 })
  }, [])

  useEffect(() => {
    const onDragEnter = (e: DragEvent): void => {
      if (!e.dataTransfer?.types.includes('Files')) return
      e.preventDefault()
      depth.current += 1
      setActive(true)
    }

    const onDragOver = (e: DragEvent): void => {
      if (!e.dataTransfer?.types.includes('Files')) return
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'

      // 实时统计拖拽中的文件数量与体积，让用户拖之前就有预期
      const items = e.dataTransfer.items
      if (items?.length) {
        let count = 0
        let bytes = 0
        for (const item of items) {
          if (item.kind !== 'file') continue
          count += 1
          const file = item.getAsFile()
          if (file) bytes += file.size
        }
        setStat({ count, bytes })
      }
    }

    const onDragLeave = (e: DragEvent): void => {
      if (!e.dataTransfer?.types.includes('Files')) return
      depth.current = Math.max(0, depth.current - 1)
      if (depth.current === 0) reset()
    }

    const onDrop = (e: DragEvent): void => {
      const files = e.dataTransfer?.files
      if (!files?.length) {
        reset()
        return
      }
      e.preventDefault()
      reset()

      const paths: string[] = []
      for (const file of Array.from(files)) {
        const path = window.docforge.files.pathForFile(file)
        if (path) paths.push(path)
      }
      if (paths.length) void addPaths(paths)
    }

    window.addEventListener('dragenter', onDragEnter)
    window.addEventListener('dragover', onDragOver)
    window.addEventListener('dragleave', onDragLeave)
    window.addEventListener('drop', onDrop)
    return () => {
      window.removeEventListener('dragenter', onDragEnter)
      window.removeEventListener('dragover', onDragOver)
      window.removeEventListener('dragleave', onDragLeave)
      window.removeEventListener('drop', onDrop)
    }
  }, [addPaths, reset])

  return (
    <AnimatePresence>
      {active && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
          className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-abyss/78 backdrop-blur-md"
        >
          {/* 四周向内收束的极光边框 */}
          <motion.div
            initial={{ scale: 0.97, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.99, opacity: 0 }}
            transition={{ type: 'spring', stiffness: 300, damping: 28 }}
            className="absolute inset-5 rounded-3xl border-2 border-dashed border-aurora-cyan/45"
          >
            <div className="grid-floor absolute inset-0 rounded-3xl opacity-40" />
          </motion.div>

          <motion.div
            initial={{ y: 14, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 8, opacity: 0 }}
            transition={{ type: 'spring', stiffness: 320, damping: 30, delay: 0.03 }}
            className="relative flex flex-col items-center gap-4 px-10 text-center"
          >
            <div className="relative">
              <span className="anim-pulse-ring absolute inset-0 rounded-2xl bg-aurora-cyan/25" />
              <div className="glass anim-float relative flex h-16 w-16 items-center justify-center rounded-2xl">
                <UploadCloud size={30} className="text-aurora-cyan" strokeWidth={1.5} />
              </div>
            </div>

            <div>
              <div className="text-[19px] font-semibold text-aurora">松开即可导入</div>
              <div className="mt-1.5 text-[12.5px] text-ink-2">
                支持文件、文件夹、多选混合拖入 · 目录会自动递归展开
              </div>
            </div>

            {stat.count > 0 && (
              <div className="flex items-center gap-4 rounded-full border border-hairline-2 bg-panel px-4 py-1.5 text-[12px]">
                <span className="flex items-center gap-1.5 text-ink">
                  <FileStack size={13} className="text-aurora-cyan" strokeWidth={1.75} />
                  <span className="font-mono font-semibold">{stat.count}</span> 个文件
                </span>
                {stat.bytes > 0 && (
                  <>
                    <span className="h-3 w-px bg-hairline" />
                    <span className="font-mono text-ink-2">{formatBytes(stat.bytes)}</span>
                  </>
                )}
              </div>
            )}

            <div className="flex items-center gap-1.5 text-[11px] text-ink-4">
              <FolderOpen size={12} strokeWidth={1.75} />
              也可以按 Ctrl+O 打开文件，或 Ctrl+V 粘贴剪贴板内容
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
