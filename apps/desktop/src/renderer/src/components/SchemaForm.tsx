import { useMemo } from 'react'
import type { JsonSchema } from '@shared/index'
import { Field, Segmented, Select, Slider, Switch, TextInput } from '@/components/ui'

/**
 * 由 JSON Schema 自动生成参数表单。
 *
 * 这是"动作注册表"设计的回报：内核里的 `params_schema` 是唯一事实来源，
 * 新增一个功能只需要在后端声明参数，前端**一行都不用改**就能得到完整表单。
 * 手工为每个功能写表单不仅重复劳动，还会随着参数演进而逐渐与后端脱节。
 *
 * 字段类型映射：
 *   enum              → 下拉框（只有两个选项时用分段控件更顺手）
 *   boolean           → 开关
 *   number + min/max  → 滑块（带数值显示）
 *   number（无范围）   → 文本框
 *   string            → 文本框
 */

interface SchemaFormProps {
  schema: JsonSchema
  value: Record<string, unknown>
  onChange: (value: Record<string, unknown>) => void
  /** 跳过这些字段（由页面自己用更合适的控件渲染） */
  exclude?: string[]
}

/** 把标题转成更像人话的提示语 */
function humanize(title: string | undefined, key: string): string {
  return title || key
}

export function SchemaForm({ schema, value, onChange, exclude = [] }: SchemaFormProps): React.JSX.Element {
  const entries = useMemo(() => {
    const properties = schema.properties ?? {}
    return Object.entries(properties).filter(([key]) => !exclude.includes(key))
  }, [schema, exclude])

  const set = (key: string, next: unknown): void => {
    onChange({ ...value, [key]: next })
  }

  if (entries.length === 0) {
    return <div className="text-[11.5px] text-ink-4">该功能没有可调参数</div>
  }

  return (
    <div className="space-y-3.5">
      {entries.map(([key, prop]) => {
        const label = humanize(prop.title, key)
        const hint = prop.description
        const current = value[key] ?? prop.default

        // ---- 枚举 ----
        if (Array.isArray(prop.enum) && prop.enum.length > 0) {
          const options = (prop.enum as (string | number)[]).map((item) => ({
            value: String(item),
            label: String(item)
          }))

          // 选项少且标签短时用分段控件，视觉上更轻
          if (options.length <= 3 && options.every((o) => o.label.length <= 10)) {
            return (
              <Field key={key} label={label} hint={hint}>
                <Segmented
                  value={String(current ?? options[0].value)}
                  options={options}
                  onChange={(next) => set(key, next)}
                />
              </Field>
            )
          }

          return (
            <Field key={key} label={label} hint={hint}>
              <Select
                value={String(current ?? options[0].value)}
                options={options}
                onChange={(next) => set(key, next)}
              />
            </Field>
          )
        }

        // ---- 布尔 ----
        if (prop.type === 'boolean') {
          return <Switch key={key} checked={Boolean(current)} onChange={(next) => set(key, next)} label={label} hint={hint} />
        }

        // ---- 数值 ----
        if (prop.type === 'number' || prop.type === 'integer') {
          const min = typeof prop.minimum === 'number' ? prop.minimum : undefined
          const max = typeof prop.maximum === 'number' ? prop.maximum : undefined

          if (min !== undefined && max !== undefined && max - min <= 1000) {
            const step = prop.type === 'integer' ? 1 : max - min <= 2 ? 0.01 : 0.01
            return (
              <Field key={key} label={label} hint={hint}>
                <Slider
                  value={Number(current ?? prop.default ?? min)}
                  min={min}
                  max={max}
                  step={step}
                  onChange={(next) => set(key, next)}
                  format={(v) => (prop.type === 'integer' ? String(Math.round(v)) : v.toFixed(2))}
                />
              </Field>
            )
          }

          return (
            <Field key={key} label={label} hint={hint}>
              <TextInput
                value={String(current ?? '')}
                onChange={(next) => set(key, next === '' ? '' : Number(next))}
                mono
              />
            </Field>
          )
        }

        // ---- 字符串 ----
        return (
          <Field key={key} label={label} hint={hint}>
            <TextInput value={String(current ?? '')} onChange={(next) => set(key, next)} />
          </Field>
        )
      })}
    </div>
  )
}

/** 用 Schema 的默认值初始化一份参数对象 */
export function defaultsFromSchema(schema: JsonSchema): Record<string, unknown> {
  const result: Record<string, unknown> = {}
  for (const [key, prop] of Object.entries(schema.properties ?? {})) {
    if (prop.default !== undefined) result[key] = prop.default
  }
  return result
}
