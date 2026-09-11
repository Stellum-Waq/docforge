/**
 * 设计系统原语。
 *
 * 这里刻意不用现成的组件库主题（shadcn 等），而是自己写：
 * 一来"科技风"需要高度定制的发光/玻璃/动效细节，改别人主题反而更费劲；
 * 二来桌面应用需要的组件数量有限，自研能保持包体小、样式一致。
 *
 * ## 面的纪律
 *
 * 所有组件只引用 `styles/index.css` 里的面（surface），**不允许自己拼半透明色**。
 * 页面底 → 工作区 → 卡片 → 卡内嵌套是四级固定的明度阶梯，组件只是站在其中一级上。
 * 一旦某个组件写了 `bg-panel-2` 这种半透明底，"模块和背景糊在一起"就会回来 ——
 * 这是上一版 UI 最主要的毛病，别再犯。
 */

import type { ReactNode } from 'react'
import { motion } from 'motion/react'
import { cn } from '@/lib/utils'

/* ------------------------------------------------------------------ */
/* Button                                                             */
/* ------------------------------------------------------------------ */

type ButtonVariant = 'primary' | 'ghost' | 'outline' | 'danger'
type ButtonSize = 'sm' | 'md' | 'lg'

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  // 主按钮：发光渐变底。整个界面里最"亮"的元素，只留给唯一的主动作
  primary: 'btn-glow font-semibold',
  // 次要按钮：实心嵌套面。悬停时抬一级，边线变亮
  outline: 'bg-panel-2 border border-hairline text-ink-2 hover:bg-panel-3 hover:border-hairline-2 hover:text-ink',
  ghost: 'text-ink-3 hover:bg-panel-2 hover:text-ink border border-transparent',
  danger: 'bg-err/12 border border-err/45 text-err hover:bg-err/20 hover:border-err/70'
}

const BUTTON_SIZE: Record<ButtonSize, string> = {
  sm: 'h-7 px-2.5 text-[11.5px] gap-1.5 rounded-lg',
  md: 'h-9 px-3.5 text-[12.5px] gap-2 rounded-[10px]',
  lg: 'h-11 px-5 text-[13.5px] gap-2 rounded-xl'
}

export interface ButtonProps {
  children: ReactNode
  onClick?: () => void
  variant?: ButtonVariant
  size?: ButtonSize
  disabled?: boolean
  title?: string
  className?: string
  type?: 'button' | 'submit'
}

export function Button({
  children,
  onClick,
  variant = 'outline',
  size = 'md',
  disabled,
  title,
  className,
  type = 'button'
}: ButtonProps): React.JSX.Element {
  return (
    <button
      type={type}
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'inline-flex items-center justify-center font-medium transition-all duration-200 select-none',
        BUTTON_VARIANT[variant],
        BUTTON_SIZE[size],
        disabled && 'pointer-events-none opacity-45 saturate-50',
        className
      )}
    >
      {children}
    </button>
  )
}

/* ------------------------------------------------------------------ */
/* Card                                                               */
/* ------------------------------------------------------------------ */

export function Card({
  children,
  className,
  title,
  icon,
  actions,
  padded = true
}: {
  children?: ReactNode
  className?: string
  title?: string
  icon?: ReactNode
  actions?: ReactNode
  padded?: boolean
}): React.JSX.Element {
  return (
    <section className={cn('surface-1 overflow-hidden', className)}>
      {title && (
        // 卡头用嵌套面（panel-2）：一眼就能看出"这是这张卡的标题区"，
        // 而不是靠一条几乎看不见的分隔线去猜
        <header className="flex items-center gap-2 border-b border-hairline bg-panel-2 px-4 py-2.5">
          {icon && <span className="text-aurora-cyan">{icon}</span>}
          <h3 className="text-[12.5px] font-semibold text-ink">{title}</h3>
          <div className="ml-auto flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className={cn(padded && 'p-4')}>{children}</div>
    </section>
  )
}

/* ------------------------------------------------------------------ */
/* 表单控件                                                            */
/* ------------------------------------------------------------------ */

export function Field({
  label,
  hint,
  children,
  className
}: {
  label: string
  hint?: string
  children: ReactNode
  className?: string
}): React.JSX.Element {
  return (
    <div className={cn('min-w-0', className)}>
      <div className="mb-1.5 flex items-baseline gap-2">
        <span className="text-[11.5px] font-medium text-ink-2">{label}</span>
        {hint && <span className="truncate text-[10.5px] text-ink-4">{hint}</span>}
      </div>
      {children}
    </div>
  )
}

export function Slider({
  value,
  min,
  max,
  step = 1,
  onChange,
  format
}: {
  value: number
  min: number
  max: number
  step?: number
  onChange: (value: number) => void
  format?: (value: number) => string
}): React.JSX.Element {
  const percent = ((value - min) / (max - min)) * 100
  return (
    <div className="flex items-center gap-3">
      <div className="relative h-5 flex-1">
        <div className="absolute top-1/2 h-1 w-full -translate-y-1/2 rounded-full bg-panel-3" />
        {/* 已填充部分用极光渐变，配合滑块的发光手柄 */}
        <div
          className="absolute top-1/2 h-1 -translate-y-1/2 rounded-full bg-linear-to-r from-aurora-cyan to-aurora-violet"
          style={{ width: `${percent}%` }}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="absolute inset-0 h-full w-full cursor-pointer appearance-none bg-transparent
            [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5
            [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full
            [&::-webkit-slider-thumb]:border [&::-webkit-slider-thumb]:border-aurora-cyan
            [&::-webkit-slider-thumb]:bg-panel-2 [&::-webkit-slider-thumb]:shadow-[0_0_10px_var(--color-aurora-cyan)]
            [&::-webkit-slider-thumb]:transition-transform hover:[&::-webkit-slider-thumb]:scale-115"
        />
      </div>
      <span className="w-14 shrink-0 text-right font-mono text-[11px] text-ink-2">
        {format ? format(value) : value}
      </span>
    </div>
  )
}

export function Switch({
  checked,
  onChange,
  label,
  hint
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  hint?: string
}): React.JSX.Element {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="flex w-full items-center gap-2.5 rounded-[10px] border border-transparent px-1 py-1.5 text-left transition-colors hover:border-hairline hover:bg-panel-2"
    >
      <span
        className={cn(
          'relative h-[18px] w-8 shrink-0 rounded-full border transition-colors duration-200',
          checked ? 'border-aurora-cyan/70 bg-aurora-cyan/30' : 'border-hairline-2 bg-panel-3'
        )}
      >
        <motion.span
          layout
          transition={{ type: 'spring', stiffness: 520, damping: 32 }}
          className={cn(
            'absolute top-[2px] h-3 w-3 rounded-full',
            checked
              ? 'left-[16px] bg-aurora-cyan shadow-[0_0_8px_var(--color-aurora-cyan)]'
              : 'left-[2px] bg-ink-4'
          )}
        />
      </span>
      <span className="min-w-0 flex-1">
        <span className={cn('block text-[12px]', checked ? 'text-ink' : 'text-ink-2')}>{label}</span>
        {hint && <span className="block truncate text-[10.5px] text-ink-4">{hint}</span>}
      </span>
    </button>
  )
}

export function Select<T extends string>({
  value,
  options,
  onChange
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
}): React.JSX.Element {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as T)}
      className="h-8 w-full appearance-none rounded-[10px] border border-hairline bg-panel-2 px-2.5
        text-[12px] text-ink transition-colors outline-none
        hover:border-hairline-2 focus:border-aurora-cyan"
    >
      {options.map((opt) => (
        <option key={opt.value} value={opt.value} className="bg-panel text-ink">
          {opt.label}
        </option>
      ))}
    </select>
  )
}

export function Segmented<T extends string>({
  value,
  options,
  onChange
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
}): React.JSX.Element {
  return (
    <div className="flex gap-1 rounded-[10px] border border-hairline bg-abyss p-1">
      {options.map((opt) => {
        const active = opt.value === value
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={cn(
              'relative flex-1 rounded-[7px] px-2 py-1 text-[11.5px] font-medium transition-colors',
              active ? 'text-ink' : 'text-ink-3 hover:text-ink-2'
            )}
          >
            {active && (
              <motion.span
                layoutId={`seg-${options.map((o) => o.value).join('-')}`}
                transition={{ type: 'spring', stiffness: 420, damping: 34 }}
                className="absolute inset-0 rounded-[7px] border border-hairline-2 bg-panel-3"
              />
            )}
            <span className="relative">{opt.label}</span>
          </button>
        )
      })}
    </div>
  )
}

export function ColorInput({
  value,
  onChange
}: {
  value: string
  onChange: (value: string) => void
}): React.JSX.Element {
  return (
    <div className="flex items-center gap-2">
      <label className="relative h-8 w-10 shrink-0 cursor-pointer overflow-hidden rounded-[10px] border border-hairline-2">
        <span className="absolute inset-0" style={{ background: value }} />
        <input
          type="color"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="absolute inset-0 cursor-pointer opacity-0"
        />
      </label>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        className="h-8 min-w-0 flex-1 rounded-[10px] border border-hairline bg-panel-2 px-2.5
          font-mono text-[11.5px] text-ink outline-none transition-colors
          hover:border-hairline-2 focus:border-aurora-cyan"
      />
    </div>
  )
}

export function TextInput({
  value,
  onChange,
  placeholder,
  mono
}: {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  mono?: boolean
}): React.JSX.Element {
  return (
    <input
      value={value}
      placeholder={placeholder}
      spellCheck={false}
      onChange={(e) => onChange(e.target.value)}
      className={cn(
        'h-8 w-full rounded-[10px] border border-hairline bg-panel-2 px-2.5 text-[12px] text-ink',
        'transition-colors outline-none placeholder:text-ink-4',
        'hover:border-hairline-2 focus:border-aurora-cyan',
        mono && 'font-mono text-[11.5px]'
      )}
    />
  )
}

/* ------------------------------------------------------------------ */
/* Badge / 进度环                                                      */
/* ------------------------------------------------------------------ */

const BADGE_TONE = {
  ok: 'border-ok/45 bg-ok/14 text-ok',
  warn: 'border-warn/45 bg-warn/14 text-warn',
  err: 'border-err/45 bg-err/14 text-err',
  run: 'border-run/50 bg-run/16 text-run',
  idle: 'border-hairline-2 bg-panel-3 text-ink-3',
  aurora: 'border-aurora-cyan/50 bg-aurora-cyan/14 text-aurora-cyan'
} as const

export function Badge({
  children,
  tone = 'idle',
  className
}: {
  children: ReactNode
  tone?: keyof typeof BADGE_TONE
  className?: string
}): React.JSX.Element {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md border px-1.5 py-px text-[10.5px] font-medium',
        BADGE_TONE[tone],
        className
      )}
    >
      {children}
    </span>
  )
}

export function ProgressRing({
  value,
  size = 56,
  stroke = 4,
  tone = 'var(--color-aurora-cyan)',
  children
}: {
  value: number
  size?: number
  stroke?: number
  tone?: string
  children?: ReactNode
}): React.JSX.Element {
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  const offset = circumference * (1 - Math.max(0, Math.min(100, value)) / 100)

  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--color-hairline)"
          strokeWidth={stroke}
        />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={tone}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          animate={{ strokeDashoffset: offset }}
          transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
          style={{ filter: `drop-shadow(0 0 5px ${tone})` }}
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center">{children}</div>
    </div>
  )
}

/** 细长的行内进度条，用于任务列表 */
export function ProgressBar({ value, tone }: { value: number; tone?: string }): React.JSX.Element {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-panel-3">
      <motion.div
        className="h-full rounded-full"
        style={{
          background: tone ?? 'linear-gradient(90deg, var(--color-aurora-cyan), var(--color-aurora-violet))'
        }}
        animate={{ width: `${Math.max(0, Math.min(100, value))}%` }}
        transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      />
    </div>
  )
}
