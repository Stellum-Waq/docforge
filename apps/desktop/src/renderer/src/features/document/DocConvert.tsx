import { ArrowLeftRight, FileOutput, FileType2 } from 'lucide-react'
import { ToolWorkbench, type ToolDefinition } from '@/features/toolbox/ToolWorkbench'

/**
 * 文档转换页 —— 对应需求 3「PDF 和 word 转换」。
 *
 * 两个方向各一个动作：
 *   * 文档转 PDF：按保真度自动选择 Office COM → LibreOffice → 内置渲染
 *   * PDF 转 Word：pdf2docx 还原版面；扫描件可自动走 OCR
 *
 * 界面这里只声明"有哪些工具、接受哪些文件"，参数表单与交互细节
 * 全部由内核的 `paramsSchema` 与 `aggregate` 元数据驱动。
 */
const TOOLS: ToolDefinition[] = [
  {
    id: 'doc.to_pdf',
    icon: <FileOutput size={15} />,
    blurb: 'Word / Excel / PPT 转 PDF，自动选用保真度最高的引擎'
  },
  {
    id: 'pdf.to_word',
    icon: <FileType2 size={15} />,
    blurb: 'PDF 还原为可编辑 Word，保留文字、图片与表格'
  }
]

/** 可参与转换的扩展名（与内核 doc.to_pdf / pdf.to_word 的 accepts 保持一致） */
const CONVERTIBLE = new Set([
  'pdf',
  'doc',
  'docx',
  'docm',
  'rtf',
  'odt',
  'wps',
  'txt',
  'md',
  'xls',
  'xlsx',
  'xlsm',
  'xlsb',
  'ods',
  'et',
  'csv',
  'tsv',
  'ppt',
  'pptx',
  'pptm',
  'odp',
  'dps'
])

export function DocConvert(): React.JSX.Element {
  return (
    <ToolWorkbench
      title="文档转换"
      icon={<ArrowLeftRight size={14} strokeWidth={1.9} />}
      tools={TOOLS}
      acceptFilter={(file) => CONVERTIBLE.has(file.ext)}
      emptyHint="把 Word / Excel / PPT / PDF 拖到窗口任意位置即可导入"
      fileNoun="文档"
    />
  )
}
