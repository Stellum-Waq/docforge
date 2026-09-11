import { motion } from 'motion/react'
import { ArrowLeft, Construction } from 'lucide-react'

interface ComingSoonProps {
  label: string
  hint: string
  milestone: string | null
  onBack: () => void
}

/**
 * 未实现功能的占位页。
 *
 * 刻意不做成"死页面"：明确告知该功能属于哪个开发里程碑，
 * 让用户能判断进度，而不是以为程序坏了。
 */
export function ComingSoon({ label, hint, milestone, onBack }: ComingSoonProps): React.JSX.Element {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <motion.div
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ type: 'spring', stiffness: 300, damping: 28 }}
        className="glass max-w-md px-8 py-9 text-center"
      >
        <div className="relative mx-auto mb-4 w-fit">
          <span className="anim-pulse-ring absolute inset-0 rounded-2xl bg-aurora-violet/20" />
          <div className="glass-flat anim-float relative flex h-14 w-14 items-center justify-center rounded-2xl">
            <Construction size={24} className="text-aurora-violet" strokeWidth={1.6} />
          </div>
        </div>

        <h2 className="text-[17px] font-semibold text-ink">{label}</h2>
        {hint && <p className="mt-1 text-[12px] text-ink-3">{hint}</p>}

        <div className="mt-4 rounded-lg border border-hairline-2 bg-panel-2 px-3.5 py-2.5 text-[11.5px] leading-relaxed text-ink-3">
          该模块计划在{' '}
          <span className="rounded border border-aurora-cyan/40 bg-aurora-cyan/12 px-1.5 py-px font-mono text-[10.5px] font-semibold text-aurora-cyan">
            {milestone ?? '后续'}
          </span>{' '}
          里程碑交付。
          <br />
          当前已完成 M0：内核通信链路、引擎能力探测、拖拽导入通道。
        </div>

        <button
          type="button"
          onClick={onBack}
          className="btn-glow mt-5 inline-flex items-center gap-2 rounded-lg px-4 py-2 text-[12.5px] font-medium text-ink"
        >
          <ArrowLeft size={13} strokeWidth={2} />
          返回总览
        </button>
      </motion.div>
    </div>
  )
}
