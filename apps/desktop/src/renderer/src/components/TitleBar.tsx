import { useEffect, useState } from 'react'
import { Minus, Square, Copy, X, Hexagon } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { CorePhase } from '@shared/index'

const PHASE_STYLE: Record<CorePhase, { color: string; label: string }> = {
  starting: { color: 'var(--color-warn)', label: '内核启动中' },
  ready: { color: 'var(--color-ok)', label: '内核就绪' },
  degraded: { color: 'var(--color-warn)', label: '降级运行' },
  stopped: { color: 'var(--color-idle)', label: '内核已停止' },
  error: { color: 'var(--color-err)', label: '内核异常' }
}

interface TitleBarProps {
  phase: CorePhase
  phaseMessage?: string
}

/**
 * 自绘标题栏。
 *
 * 窗口是无边框的（frame: false），所以拖拽、双击最大化、窗口按钮都要自己做。
 * 拖拽靠 CSS 的 -webkit-app-region，按钮区域必须显式标 no-drag，否则点不动。
 */
export function TitleBar({ phase, phaseMessage }: TitleBarProps): React.JSX.Element {
  const [maximized, setMaximized] = useState(false)

  useEffect(() => {
    void window.docforge.window.isMaximized().then(setMaximized)
    return window.docforge.window.onMaximizeChange(setMaximized)
  }, [])

  const style = PHASE_STYLE[phase]

  return (
    // 标题栏用最底的外壳色（abyss），并在底部压一条极光分隔线 ——
    // 它和工作区之间有一道明确的"边界"，而不是靠颜色深浅去猜
    <header className="drag-region relative z-30 flex h-11 shrink-0 items-center justify-between border-b border-hairline bg-abyss pl-4">
      {/* 左：品牌标识 + 状态 */}
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex items-center gap-2">
          <Hexagon
            size={17}
            className="text-aurora-cyan anim-spin-slow"
            strokeWidth={1.75}
            aria-hidden="true"
          />
          <span className="text-[13px] font-semibold tracking-wide text-ink">文枢</span>
          <span className="text-[11px] font-medium tracking-[0.18em] text-ink-4">DOCFORGE</span>
        </div>

        <div className="h-4 w-px bg-panel-3" />

        {/* 内核状态灯：呼吸圆点 + 文案。做成带底色的小胶囊，
            否则状态文字会"飘"在标题栏上，和品牌名混成一串 */}
        <div
          className="flex min-w-0 items-center gap-2 rounded-full border border-hairline bg-panel px-2.5 py-1"
          title={phaseMessage}
        >
          <span className="relative flex h-2 w-2 shrink-0">
            {phase === 'ready' && (
              <span
                className="anim-pulse-ring absolute inset-0 rounded-full"
                style={{ background: style.color }}
              />
            )}
            <span
              className="relative h-2 w-2 rounded-full"
              style={{ background: style.color, boxShadow: `0 0 8px ${style.color}` }}
            />
          </span>
          <span className="truncate text-[11.5px] text-ink-2">{style.label}</span>
          {phaseMessage && phase !== 'ready' && (
            <span className="truncate text-[11px] text-ink-4">· {phaseMessage}</span>
          )}
        </div>
      </div>

      {/* 右：窗口控制按钮 */}
      <div className="no-drag flex h-full items-stretch">
        <WindowButton label="最小化" onClick={() => window.docforge.window.minimize()}>
          <Minus size={14} strokeWidth={1.75} />
        </WindowButton>
        <WindowButton
          label={maximized ? '还原' : '最大化'}
          onClick={() => window.docforge.window.maximize()}
        >
          {maximized ? <Copy size={12} strokeWidth={1.75} /> : <Square size={12} strokeWidth={1.75} />}
        </WindowButton>
        <WindowButton label="关闭" danger onClick={() => window.docforge.window.close()}>
          <X size={15} strokeWidth={1.75} />
        </WindowButton>
      </div>
    </header>
  )
}

function WindowButton({
  children,
  onClick,
  label,
  danger
}: {
  children: React.ReactNode
  onClick: () => void
  label: string
  danger?: boolean
}): React.JSX.Element {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className={cn(
        'flex w-12 items-center justify-center text-ink-2 transition-colors duration-150',
        danger ? 'hover:bg-err hover:text-white' : 'hover:bg-panel-3 hover:text-ink'
      )}
    >
      {children}
    </button>
  )
}
