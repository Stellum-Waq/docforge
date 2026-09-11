import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { useJobs } from './store/jobs'
import { useWorkspace } from './store/workspace'
import './styles/index.css'

/**
 * 开发期调试钩子。
 *
 * 界面验证要往工作区里塞文件，而"选择文件"会弹原生对话框 —— 那是脚本
 * 碰不到的地方。把两个 store 挂到 window 上，`tools/cdp.mjs` 就能直接
 * 调用 `__docforge.workspace.addPaths([...])`，走的是和拖拽**完全相同**的
 * 导入路径，验证结果才有意义。
 *
 * 只在开发构建里挂载（`import.meta.env.DEV` 在生产构建中会被静态替换为
 * false，整个代码块会被摇掉）。
 */
if (import.meta.env.DEV) {
  ;(window as unknown as Record<string, unknown>).__docforge = {
    workspace: useWorkspace,
    jobs: useJobs
  }
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 引擎探测结果在运行期基本不变，避免频繁打扰内核
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false
    }
  }
})

const container = document.getElementById('root')
if (!container) throw new Error('找不到 #root 挂载点')

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>
)
