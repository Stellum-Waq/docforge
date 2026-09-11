import { Droplet, Wand2 } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * 水印位置选择器：九宫格 + 平铺 + 拖拽定位。
 *
 * 抽成共享组件是因为图片水印与 PDF 水印用的是**完全相同的语义**
 * （position 取值、平铺、自定义坐标比例），各写一份迟早会漂移。
 */

const GRID: { value: string; label: string }[] = [
  { value: 'top-left', label: '左上' },
  { value: 'top-center', label: '上中' },
  { value: 'top-right', label: '右上' },
  { value: 'middle-left', label: '左中' },
  { value: 'center', label: '居中' },
  { value: 'middle-right', label: '右中' },
  { value: 'bottom-left', label: '左下' },
  { value: 'bottom-center', label: '下中' },
  { value: 'bottom-right', label: '右下' }
]

interface PositionPickerProps {
  value: string
  onChange: (value: string) => void
  /** 平铺按钮的文案，PDF 与图片场景叫法略有不同 */
  tileLabel?: string
}

export function PositionPicker({
  value,
  onChange,
  tileLabel = '平铺'
}: PositionPickerProps): React.JSX.Element {
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-3 gap-1">
        {GRID.map((cell) => {
          const active = value === cell.value
          return (
            <button
              key={cell.value}
              type="button"
              title={cell.label}
              onClick={() => onChange(cell.value)}
              className={cn(
                'flex h-8 items-center justify-center rounded-md border text-[10px] transition-colors',
                active
                  ? 'border-aurora-cyan/55 bg-aurora-cyan/14 text-aurora-cyan'
                  : 'border-hairline-2 text-ink-4 hover:border-hairline hover:bg-panel-2 hover:text-ink-3'
              )}
            >
              {cell.label}
            </button>
          )
        })}
      </div>

      <div className="grid grid-cols-2 gap-1">
        <button
          type="button"
          onClick={() => onChange('tile')}
          className={cn(
            'flex h-7 items-center justify-center gap-1.5 rounded-md border text-[10.5px] transition-colors',
            value === 'tile'
              ? 'border-aurora-violet/55 bg-aurora-violet/14 text-aurora-violet'
              : 'border-hairline-2 text-ink-4 hover:border-hairline hover:text-ink-3'
          )}
        >
          <Droplet size={11} />
          {tileLabel}
        </button>
        <button
          type="button"
          onClick={() => onChange('custom')}
          className={cn(
            'flex h-7 items-center justify-center gap-1.5 rounded-md border text-[10.5px] transition-colors',
            value === 'custom'
              ? 'border-aurora-violet/55 bg-aurora-violet/14 text-aurora-violet'
              : 'border-hairline-2 text-ink-4 hover:border-hairline hover:text-ink-3'
          )}
        >
          <Wand2 size={11} />
          拖拽定位
        </button>
      </div>
    </div>
  )
}
