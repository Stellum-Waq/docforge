import { readdir, stat } from 'node:fs/promises'
import { basename, extname, join, relative } from 'node:path'
import { randomUUID } from 'node:crypto'
import type { FileKind, WorkspaceFile } from '@shared/index'

/** 扩展名 → 粗分类。用于"拖入即智能推荐任务"（对应设计文档 §1-F）。 */
const EXT_KIND: Record<string, FileKind> = {
  // 图像
  jpg: 'image', jpeg: 'image', png: 'image', gif: 'image', webp: 'image',
  bmp: 'image', tif: 'image', tiff: 'image', heic: 'image', heif: 'image',
  avif: 'image', ico: 'image', svg: 'image', jfif: 'image',
  // PDF
  pdf: 'pdf',
  // Word
  doc: 'word', docx: 'word', docm: 'word', rtf: 'word', odt: 'word', wps: 'word',
  // Excel
  xls: 'excel', xlsx: 'excel', xlsm: 'excel', xlsb: 'excel', csv: 'excel',
  tsv: 'excel', ods: 'excel', et: 'excel',
  // PPT
  ppt: 'ppt', pptx: 'ppt', pptm: 'ppt', odp: 'ppt', dps: 'ppt',
  // 文本
  txt: 'text', md: 'text', markdown: 'text', json: 'text', xml: 'text',
  html: 'text', htm: 'text', log: 'text', yaml: 'text', yml: 'text',
  // 压缩包
  zip: 'archive', rar: 'archive', '7z': 'archive', tar: 'archive', gz: 'archive'
}

/** 递归展开时会跳过的目录，避免误扫系统目录导致卡死 */
const SKIP_DIRS = new Set([
  'node_modules', '.git', '.venv', '__pycache__', '$RECYCLE.BIN',
  'System Volume Information', 'Windows', 'Program Files', 'Program Files (x86)',
  'AppData', '.cache', 'dist', 'out'
])

/** 单次导入的文件数上限，防止用户误拖整个盘符把 UI 拖死 */
const MAX_FILES = 20_000
/** 递归最大深度 */
const MAX_DEPTH = 12

export function classify(name: string): FileKind {
  const ext = extname(name).slice(1).toLowerCase()
  return EXT_KIND[ext] ?? 'unknown'
}

export function extOf(name: string): string {
  return extname(name).slice(1).toLowerCase()
}

function toWorkspaceFile(absPath: string, root?: string): WorkspaceFile {
  const name = basename(absPath)
  return {
    id: randomUUID(),
    path: absPath,
    name,
    ext: extOf(name),
    sizeBytes: 0,
    kind: classify(name),
    addedAt: new Date().toISOString(),
    relativePath: root ? relative(root, absPath) : undefined
  }
}

/**
 * 把用户拖入 / 选择的路径展开为扁平的 WorkspaceFile 列表。
 *
 * - 目录会递归展开（对应需求 7 "打开本地文件等便利方式"）
 * - 保留 relativePath，从而支持"保留原目录结构"的输出选项
 * - 目录按名称排序，保证同一批次的处理顺序稳定可复现
 */
export async function expandPaths(paths: string[]): Promise<WorkspaceFile[]> {
  const out: WorkspaceFile[] = []
  const seen = new Set<string>()

  const visit = async (absPath: string, root: string | undefined, depth: number): Promise<void> => {
    if (out.length >= MAX_FILES) return

    let info: Awaited<ReturnType<typeof stat>>
    try {
      info = await stat(absPath)
    } catch {
      return // 无权限或已删除，静默跳过
    }

    if (info.isDirectory()) {
      if (depth > MAX_DEPTH) return
      if (SKIP_DIRS.has(basename(absPath))) return

      let entries: string[]
      try {
        entries = await readdir(absPath)
      } catch {
        return
      }
      entries.sort((a, b) => a.localeCompare(b, 'zh-CN'))
      for (const entry of entries) {
        await visit(join(absPath, entry), root ?? absPath, depth + 1)
        if (out.length >= MAX_FILES) return
      }
      return
    }

    if (!info.isFile()) return
    if (info.size === 0) return
    if (seen.has(absPath)) return
    seen.add(absPath)

    const file = toWorkspaceFile(absPath, root)
    file.sizeBytes = info.size
    file.addedAt = new Date().toISOString()
    out.push(file)
  }

  for (const p of paths) {
    await visit(p, undefined, 0)
    if (out.length >= MAX_FILES) break
  }

  return out
}
