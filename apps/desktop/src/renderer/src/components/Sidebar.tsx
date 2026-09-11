import {
  LayoutDashboard,
  Table2,
  ScanText,
  FileType2,
  Stamp,
  ImagePlus,
  Layers,
  Wrench,
  History,
  Settings,
  Workflow,
  type LucideIcon
} from 'lucide-react'
import { motion } from 'motion/react'
import { cn } from '@/lib/utils'

export type RouteId =
  | 'dashboard'
  | 'image-to-excel'
  | 'ocr'
  | 'pdf-word'
  | 'pdf-watermark'
  | 'image-watermark'
  | 'batch'
  | 'pipeline'
  | 'toolbox'
  | 'history'
  | 'settings'

interface NavItem {
  id: RouteId
  label: string
  hint: string
  icon: LucideIcon
  /** 该功能对应的设计里程碑；null 表示已实现 */
  milestone: string | null
}

interface NavGroup {
  title: string
  items: NavItem[]
}

/**
 * 导航结构直接映射需求的 10 项能力，让用户一眼看到软件的全貌。
 * milestone 字段标出尚未实现的功能属于哪个开发阶段，避免"点了没反应"的困惑。
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    title: '概览',
    items: [{ id: 'dashboard', label: '总览', hint: '系统状态与引擎能力', icon: LayoutDashboard, milestone: null }]
  },
  {
    title: '图像识别',
    items: [
      { id: 'image-to-excel', label: '图片 → Excel', hint: '表格结构识别', icon: Table2, milestone: null },
      { id: 'ocr', label: '文字提取', hint: '多模态 OCR', icon: ScanText, milestone: null }
    ]
  },
  {
    title: 'PDF 与文档',
    items: [
      { id: 'pdf-word', label: '文档转换', hint: 'PDF ↔ Word', icon: FileType2, milestone: null },
      { id: 'pdf-watermark', label: 'PDF 水印', hint: '实时预览', icon: Stamp, milestone: null },
      { id: 'toolbox', label: 'PDF 工具箱', hint: '合并 / 拆分 / 压缩', icon: Wrench, milestone: null }
    ]
  },
  {
    title: '图像处理',
    items: [{ id: 'image-watermark', label: '图片水印', hint: '拖拽定位 / 平铺', icon: ImagePlus, milestone: null }]
  },
  {
    title: '批处理',
    items: [
      { id: 'batch', label: '批量任务', hint: '队列与流水线', icon: Layers, milestone: null },
      { id: 'pipeline', label: '流水线', hint: '多步自动串联', icon: Workflow, milestone: null },
      { id: 'history', label: '任务历史', hint: '可回放记录', icon: History, milestone: null }
    ]
  },
  {
    title: '配置',
    items: [{ id: 'settings', label: '设置', hint: 'OCR 引擎与密钥', icon: Settings, milestone: null }]
  }
]

interface SidebarProps {
  active: RouteId
  onSelect: (id: RouteId) => void
  /** 工作区当前待处理文件数，显示在"批量任务"上的角标 */
  fileCount: number
}

export function Sidebar({ active, onSelect, fileCount }: SidebarProps): React.JSX.Element {
  return (
    <nav
      aria-label="主导航"
      className="relative z-20 flex w-[212px] shrink-0 flex-col border-r border-hairline/50 bg-void/60 backdrop-blur-xl"
    >
      <div className="flex-1 overflow-y-auto px-2.5 py-3">
        {NAV_GROUPS.map((group) => (
          <div key={group.title} className="mb-3.5">
            <div className="px-2.5 pb-1.5 text-[10px] font-semibold tracking-[0.16em] text-ink-4 uppercase">
              {group.title}
            </div>
            <ul className="space-y-0.5">
              {group.items.map((item) => {
                const isActive = item.id === active
                const Icon = item.icon
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(item.id)}
                      title={item.hint}
                      className={cn(
                        'group relative flex w-full items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-left transition-colors duration-150',
                        isActive ? 'text-ink' : 'text-ink-2 hover:bg-panel-2/60 hover:text-ink'
                      )}
                    >
                      {/* 选中态：极光渐变底 + 左侧发光条（layoutId 让高亮在切换时滑动） */}
                      {isActive && (
                        <motion.span
                          layoutId="nav-active"
                          transition={{ type: 'spring', stiffness: 420, damping: 34 }}
                          className="absolute inset-0 rounded-lg border border-aurora-cyan/25 bg-linear-to-r from-aurora-cyan/12 to-aurora-violet/10"
                        />
                      )}
                      {isActive && (
                        <span className="absolute top-1/2 -left-px h-4 w-[2px] -translate-y-1/2 rounded-full bg-aurora-cyan shadow-[0_0_8px_var(--color-aurora-cyan)]" />
                      )}

                      <Icon
                        size={15}
                        strokeWidth={1.75}
                        className={cn(
                          'relative shrink-0 transition-colors',
                          isActive ? 'text-aurora-cyan' : 'text-ink-3 group-hover:text-ink-2'
                        )}
                      />
                      <span className="relative flex-1 truncate text-[12.5px] font-medium">{item.label}</span>

                      {item.id === 'batch' && fileCount > 0 && (
                        <span className="relative rounded-full bg-aurora-cyan/20 px-1.5 py-px text-[10px] font-semibold text-aurora-cyan">
                          {fileCount > 999 ? '999+' : fileCount}
                        </span>
                      )}
                      {item.milestone && (
                        <span className="relative rounded border border-hairline/70 px-1 py-px font-mono text-[9px] text-ink-4">
                          {item.milestone}
                        </span>
                      )}
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </div>

      <div className="border-t border-hairline/50 px-3.5 py-2.5">
        <div className="text-[10px] leading-relaxed text-ink-4">
          已实现：文档转换 · PDF 水印 · PDF 工具箱
          <br />
          图片水印 · OCR 双引擎 · 图片转 Excel
        </div>
      </div>
    </nav>
  )
}
