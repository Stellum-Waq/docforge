import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { CorePhase, CoreHealth, SystemCapabilities } from '@shared/index'

/** 内核实时状态：阶段 + 可读说明。由主进程的 phase 事件驱动。 */
export function useCorePhase(): { phase: CorePhase; message?: string } {
  const [state, setState] = useState<{ phase: CorePhase; message?: string }>({ phase: 'starting' })

  useEffect(() => {
    // 首帧可能错过早期事件，先主动拉一次当前连接状态兜底
    void window.docforge.core.connection().then((conn) => {
      if (conn.connected) setState({ phase: 'ready' })
      else if (conn.error) setState({ phase: 'error', message: conn.error })
    })

    return window.docforge.core.onPhaseChange((phase, message) => setState({ phase, message }))
  }, [])

  return state
}

/** 内核健康信息（含 Python 版本、运行时长） */
export function useCoreHealth(enabled: boolean) {
  return useQuery<CoreHealth>({
    queryKey: ['core', 'health'],
    queryFn: () => window.docforge.core.health(),
    enabled,
    refetchInterval: enabled ? 10_000 : false
  })
}

/** 系统能力与引擎可用性探测结果 */
export function useCapabilities(enabled: boolean) {
  return useQuery<SystemCapabilities>({
    queryKey: ['core', 'capabilities'],
    queryFn: () => window.docforge.core.capabilities(),
    enabled,
    staleTime: 60_000
  })
}

/** 内核日志流（环形缓冲，由主进程转发） */
export function useCoreLogs(limit = 200): string[] {
  const [logs, setLogs] = useState<string[]>([])

  useEffect(() => {
    return window.docforge.core.onLog((line) => {
      setLogs((prev) => {
        const next = [...prev, line]
        return next.length > limit ? next.slice(next.length - limit) : next
      })
    })
  }, [limit])

  return logs
}
