import { useCallback, useEffect, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  Database,
  FolderOpen,
  KeyRound,
  Loader2,
  RefreshCw,
  ShieldCheck,
  ShieldOff,
  Trash2,
  Wallet,
  Zap
} from 'lucide-react'
import type { AppSettings, OcrEngineReport, OcrUsage } from '@shared/index'
import { cn } from '@/lib/utils'
import { Badge, Button, Card, Switch, TextInput } from '@/components/ui'
import { useCapabilities } from '@/hooks/useCore'

export function SettingsPage(): React.JSX.Element {
  const caps = useCapabilities(true)

  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [engines, setEngines] = useState<OcrEngineReport | null>(null)
  const [usage, setUsage] = useState<OcrUsage | null>(null)

  const [apiKeyInput, setApiKeyInput] = useState('')
  const [patternsText, setPatternsText] = useState('')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [message, setMessage] = useState<{ tone: 'ok' | 'err'; text: string } | null>(null)

  const load = useCallback(async () => {
    const [nextSettings, nextEngines, nextUsage] = await Promise.all([
      window.docforge.settings.get(),
      window.docforge.settings.ocrEngines(),
      window.docforge.settings.ocrUsage()
    ])
    setSettings(nextSettings)
    setEngines(nextEngines)
    setUsage(nextUsage.usage)
    setPatternsText((nextSettings.sensitivePatterns ?? []).join('\n'))
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const save = async (patch: Parameters<typeof window.docforge.settings.update>[0]): Promise<void> => {
    setSaving(true)
    setMessage(null)
    try {
      const next = await window.docforge.settings.update(patch)
      setSettings(next)
      setEngines(await window.docforge.settings.ocrEngines())
      setApiKeyInput('')
      setMessage({ tone: 'ok', text: '设置已保存' })
    } catch (err) {
      setMessage({ tone: 'err', text: (err as Error).message })
    } finally {
      setSaving(false)
    }
  }

  const testConnection = async (): Promise<void> => {
    setTesting(true)
    setMessage(null)
    try {
      const result = await window.docforge.settings.testOcr()
      setMessage({ tone: 'ok', text: result.message })
    } catch (err) {
      setMessage({ tone: 'err', text: (err as Error).message })
    } finally {
      setTesting(false)
    }
  }

  if (!settings) {
    return (
      <div className="flex flex-1 items-center justify-center text-[12px] text-ink-3">
        正在读取设置…
      </div>
    )
  }

  const cloudReady = settings.hasApiKey && settings.cloudOcrEnabled

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-[880px] px-7 py-6">
        <div className="mb-5">
          <h1 className="flex items-center gap-2.5 text-[20px] font-semibold tracking-tight text-ink">
            <KeyRound size={19} className="text-aurora-cyan" strokeWidth={1.8} />
            设置
          </h1>
          <p className="mt-1 text-[12px] text-ink-3">
            云端 OCR 默认关闭 · 密钥加密存储 · 本地引擎永远可用
          </p>
        </div>

        {message && (
          <div
            className={cn(
              'mb-4 flex items-start gap-2 rounded-lg border px-3 py-2 text-[11.5px]',
              message.tone === 'ok'
                ? 'border-ok/30 bg-ok/8 text-ok'
                : 'border-err/30 bg-err/8 text-err'
            )}
          >
            {message.tone === 'ok' ? (
              <CheckCircle2 size={13} className="mt-0.5 shrink-0" />
            ) : (
              <AlertCircle size={13} className="mt-0.5 shrink-0" />
            )}
            <span className="selectable">{message.text}</span>
          </div>
        )}

        <div className="space-y-4">
          {/* ---------------- OCR 引擎 ---------------- */}
          <Card title="OCR 引擎状态" icon={<Zap size={13} strokeWidth={1.9} />}>
            <div className="space-y-2.5">
              {(engines?.engines ?? []).map((engine) => (
                <div
                  key={engine.id}
                  className={cn(
                    'flex items-start gap-3 rounded-xl border p-3',
                    engine.available ? 'border-ok/25 bg-ok/5' : 'border-hairline/60 bg-panel-2/40'
                  )}
                >
                  <span className={cn('mt-0.5', engine.available ? 'text-ok' : 'text-ink-4')}>
                    {engine.offline ? <ShieldCheck size={15} /> : <Cloud size={15} />}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[12.5px] font-medium text-ink">{engine.label}</span>
                      {engine.offline && <Badge tone="ok">离线</Badge>}
                      <Badge tone="idle">保真 {engine.fidelity}</Badge>
                    </div>
                    <div className="mt-0.5 text-[10.5px] text-ink-4">
                      {engine.available
                        ? engine.offline
                          ? '完全本地运行，文件不出本机，零费用'
                          : '云端多模态识别，复杂表格与手写效果最好'
                        : (engine.reason ?? '不可用')}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </Card>

          {/* ---------------- 云端 OCR 配置 ---------------- */}
          <Card title="DeepSeek Vision 云端识别" icon={<Cloud size={13} strokeWidth={1.9} />}>
            <div className="space-y-3.5">
              <Switch
                checked={settings.cloudOcrEnabled}
                onChange={(next) => void save({ cloudOcrEnabled: next })}
                label="启用云端识别"
                hint="关闭时所有识别都在本地完成，图片不会上传"
              />

              <div className="space-y-1.5 border-t border-hairline/40 pt-3">
                <div className="flex items-baseline gap-2">
                  <span className="text-[11.5px] font-medium text-ink-2">API Key</span>
                  {settings.hasApiKey && (
                    <span className="font-mono text-[10.5px] text-ink-4">
                      当前：{settings.deepseekApiKeyMasked}
                    </span>
                  )}
                </div>
                <div className="flex gap-2">
                  <TextInput
                    value={apiKeyInput}
                    onChange={setApiKeyInput}
                    placeholder={settings.hasApiKey ? '留空表示不修改' : 'sk-...'}
                    mono
                  />
                  <Button
                    size="sm"
                    variant="primary"
                    className="shrink-0"
                    disabled={saving || !apiKeyInput.trim()}
                    onClick={() => void save({ deepseekApiKey: apiKeyInput })}
                  >
                    保存
                  </Button>
                </div>
                <div className="flex flex-wrap gap-2 pt-0.5">
                  <Button size="sm" disabled={testing || !settings.hasApiKey} onClick={() => void testConnection()}>
                    {testing ? <Loader2 size={12} className="animate-spin" /> : <Zap size={12} />}
                    测试连接
                  </Button>
                  {settings.hasApiKey && (
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={saving}
                      onClick={() => void save({ clearApiKey: true })}
                    >
                      <Trash2 size={12} />
                      清除密钥
                    </Button>
                  )}
                </div>
              </div>

              <div className="flex items-start gap-2 rounded-lg border border-hairline/60 bg-panel-2/40 px-3 py-2.5">
                {settings.encryptionAvailable ? (
                  <ShieldCheck size={14} className="mt-0.5 shrink-0 text-ok" />
                ) : (
                  <ShieldOff size={14} className="mt-0.5 shrink-0 text-warn" />
                )}
                <div className="text-[10.5px] leading-relaxed text-ink-3">
                  {settings.encryptionAvailable ? (
                    <>
                      密钥使用 <span className="text-ok">Windows DPAPI</span> 加密存储，
                      只能由当前 Windows 用户解密；界面与日志**永远不会回显明文**。
                      密钥保存在：
                      <span className="selectable ml-1 font-mono text-ink-4">
                        {caps.data?.dataDir ?? '（数据目录）'}
                      </span>
                    </>
                  ) : (
                    <>
                      当前系统不支持 DPAPI，密钥以明文保存在仅本人可读的文件中。
                      建议改在「本地离线」模式下工作，或使用系统级加密磁盘。
                    </>
                  )}
                </div>
              </div>
            </div>
          </Card>

          {/* ---------------- 隐私规则 ---------------- */}
          <Card title="隐私规则" icon={<ShieldCheck size={13} strokeWidth={1.9} />}>
            <div className="space-y-2.5">
              <div className="text-[11.5px] font-medium text-ink-2">
                敏感文件名规则（每行一条，支持 * 通配符）
              </div>
              <textarea
                value={patternsText}
                onChange={(e) => setPatternsText(e.target.value)}
                rows={4}
                spellCheck={false}
                placeholder={'*身份证*\n*合同*\n*工资*'}
                className="w-full resize-y rounded-lg border border-hairline bg-panel-2/70 px-2.5 py-2 font-mono
                  text-[11.5px] leading-relaxed text-ink transition-colors outline-none
                  placeholder:text-ink-4 hover:border-aurora-cyan/40 focus:border-aurora-cyan/60"
              />
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={saving}
                  onClick={() =>
                    void save({
                      sensitivePatterns: patternsText
                        .split('\n')
                        .map((line) => line.trim())
                        .filter(Boolean)
                    })
                  }
                >
                  保存规则
                </Button>
                <span className="text-[10.5px] text-ink-4">
                  命中的文件会被强制用本地引擎识别，即使已启用云端
                </span>
              </div>
            </div>
          </Card>

          {/* ---------------- 用量与缓存 ---------------- */}
          <Card title="用量与缓存" icon={<Wallet size={13} strokeWidth={1.9} />}>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat label="云端调用" value={String(usage?.calls ?? 0)} />
                <Stat label="输入 tokens" value={formatNumber(usage?.promptTokens ?? 0)} />
                <Stat label="输出 tokens" value={formatNumber(usage?.completionTokens ?? 0)} />
                <Stat
                  label="估算费用"
                  value={`$${(usage?.estimatedCostUsd ?? 0).toFixed(4)}`}
                  tone={cloudReady ? 'warn' : 'idle'}
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <Stat label="缓存条目" value={String(settings.cache.entries)} icon={<Database size={11} />} />
                <Stat label="缓存命中" value={String(settings.cache.hits)} icon={<Zap size={11} />} />
              </div>

              <div className="flex flex-wrap gap-2 border-t border-hairline/40 pt-3">
                <Button size="sm" onClick={() => void load()}>
                  <RefreshCw size={12} />
                  刷新
                </Button>
                <Button size="sm" onClick={() => void window.docforge.settings.resetOcrUsage().then(load)}>
                  重置用量统计
                </Button>
                <Button size="sm" variant="danger" onClick={() => void window.docforge.settings.clearOcrCache().then(load)}>
                  <Trash2 size={12} />
                  清空结果缓存
                </Button>
              </div>
              <div className="text-[10.5px] leading-relaxed text-ink-4">
                结果缓存以「文件内容 + 提示词版本 + 参数」为键：同一个文件重复处理**不会重复计费**。
                调整提示词后旧缓存会自动失效。费用按 DeepSeek 高峰价保守估算，仅供参考。
              </div>
            </div>
          </Card>

          {/* ---------------- 数据目录 ---------------- */}
          <Card title="数据与输出" icon={<FolderOpen size={13} strokeWidth={1.9} />}>
            <div className="space-y-2.5">
              <PathRow label="默认输出目录" value={caps.data?.defaultOutputDir ?? '—'} />
              <PathRow label="数据目录" value={caps.data?.dataDir ?? '—'} />
              <div className="text-[10.5px] leading-relaxed text-ink-4">
                任务历史、结果缓存、密钥都保存在数据目录中。卸载程序不会删除它，
                重装后可继续使用原有配置与历史记录。
              </div>
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */

function Stat({
  label,
  value,
  tone = 'idle',
  icon
}: {
  label: string
  value: string
  tone?: 'idle' | 'warn'
  icon?: React.ReactNode
}): React.JSX.Element {
  return (
    <div className="rounded-lg border border-hairline/60 bg-panel-2/40 px-3 py-2">
      <div className="flex items-center gap-1 text-[10px] text-ink-4">
        {icon}
        {label}
      </div>
      <div
        className={cn(
          'mt-0.5 font-mono text-[14px] font-semibold',
          tone === 'warn' ? 'text-warn' : 'text-ink'
        )}
      >
        {value}
      </div>
    </div>
  )
}

function PathRow({ label, value }: { label: string; value: string }): React.JSX.Element {
  return (
    <div className="flex items-start gap-3">
      <span className="w-24 shrink-0 text-[11.5px] text-ink-3">{label}</span>
      <span className="selectable min-w-0 flex-1 truncate font-mono text-[11px] text-ink-2" title={value}>
        {value}
      </span>
      <Button
        size="sm"
        variant="ghost"
        className="shrink-0"
        onClick={() => void window.docforge.shell.openPath(value)}
      >
        打开
      </Button>
    </div>
  )
}

function formatNumber(value: number): string {
  if (value < 1000) return String(value)
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)}K`
  return `${(value / 1_000_000).toFixed(2)}M`
}
