import {
  Combine,
  FileImage,
  Info,
  Minimize2,
  RotateCw,
  ScanText,
  Scissors,
  Split,
  Wrench
} from 'lucide-react'
import { ToolWorkbench, type ToolDefinition } from '@/features/toolbox/ToolWorkbench'

/**
 * PDF 工具箱。
 *
 * 这个页面**不针对任何具体功能写代码** —— 参数表单由内核返回的
 * `paramsSchema` 生成，聚合/逐个文件处理由 `ActionInfo.aggregate` 决定。
 * 新增一个 PDF 工具只需要在内核注册动作，然后在这里加一行图标配置。
 */
const TOOLS: ToolDefinition[] = [
  { id: 'pdf.merge', icon: <Combine size={15} />, blurb: '把多个 PDF 合并为一个，可选生成书签' },
  { id: 'pdf.split', icon: <Split size={15} />, blurb: '按每 N 页或自定义区间拆成多个文件' },
  { id: 'pdf.extract_pages', icon: <Scissors size={15} />, blurb: '把指定页面提取成一个新 PDF' },
  { id: 'pdf.rotate', icon: <RotateCw size={15} />, blurb: '旋转指定页面，自动叠加已有旋转角' },
  { id: 'pdf.compress', icon: <Minimize2 size={15} />, blurb: '字体子集化 + 图片降采样，显著减小体积' },
  {
    id: 'pdf.searchable',
    icon: <ScanText size={15} />,
    blurb: '给扫描件叠加不可见文字层，外观不变但可搜索、可复制'
  },
  { id: 'pdf.to_images', icon: <FileImage size={15} />, blurb: '把页面渲染成 PNG / JPEG 图片' },
  { id: 'pdf.info', icon: <Info size={15} />, blurb: '导出页数、尺寸、元数据，并识别疑似扫描页' }
]

export function PdfToolbox(): React.JSX.Element {
  return (
    <ToolWorkbench
      title="PDF 工具箱"
      icon={<Wrench size={14} strokeWidth={1.9} />}
      tools={TOOLS}
      acceptFilter={(file) => file.ext === 'pdf'}
      emptyHint="把 PDF 拖到窗口任意位置即可导入"
      fileNoun="PDF"
    />
  )
}
