import { create } from 'zustand'
import type { WorkspaceFile } from '@shared/index'

interface WorkspaceState {
  files: WorkspaceFile[]
  /** 正在展开目录（递归扫描）时为 true，用于显示加载态 */
  expanding: boolean
  /** 最近一次导入的提示信息，例如"已跳过 3 个空文件" */
  notice: string | null

  addPaths(paths: string[]): Promise<void>
  addFiles(files: WorkspaceFile[]): void
  removeFile(id: string): void
  clear(): void
  setNotice(notice: string | null): void
}

/**
 * 工作区文件仓库。
 *
 * 所有导入方式（拖拽 / 文件选择 / 剪贴板 / 文件夹）最终都汇聚到 addPaths，
 * 由主进程统一递归展开 —— 这样"保留目录结构"等下游功能只需要一份数据来源。
 */
export const useWorkspace = create<WorkspaceState>((set, get) => ({
  files: [],
  expanding: false,
  notice: null,

  async addPaths(paths) {
    const clean = paths.filter(Boolean)
    if (clean.length === 0) return

    set({ expanding: true, notice: null })
    try {
      const expanded = await window.docforge.files.expandPaths(clean)

      // 按绝对路径去重，避免同一个文件被拖两次
      const existing = new Set(get().files.map((f) => f.path))
      const fresh = expanded.filter((f) => !existing.has(f.path))

      const skipped = clean.length > 0 && expanded.length === 0
      set({
        files: [...get().files, ...fresh],
        notice: skipped ? '没有找到可处理的文件（可能为空文件或无读取权限）' : null
      })
    } catch (err) {
      set({ notice: `导入失败：${(err as Error).message}` })
    } finally {
      set({ expanding: false })
    }
  },

  addFiles(files) {
    const existing = new Set(get().files.map((f) => f.path))
    set({ files: [...get().files, ...files.filter((f) => !existing.has(f.path))] })
  },

  removeFile(id) {
    set({ files: get().files.filter((f) => f.id !== id) })
  },

  clear() {
    set({ files: [], notice: null })
  },

  setNotice(notice) {
    set({ notice })
  }
}))
