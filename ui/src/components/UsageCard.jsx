import { useEffect, useState } from 'react'
import { ArrowsClockwise, Coins } from '@phosphor-icons/react'
import { API_BASE } from './SharedComponents'

function Cell({ label, data }) {
  return (
    <div className="rounded-xl border border-border-main bg-bg-raised px-3 py-2.5">
      <div className="text-[11px] text-text-muted">{label}</div>
      <div className="text-lg font-semibold text-text-main mt-0.5">{(data?.cost ?? 0).toFixed(3)} 元</div>
      <div className="text-[10px] text-text-muted mt-0.5">
        {data?.calls || 0} 次 · 入 {data?.in || 0} / 出 {data?.out || 0} tokens
      </div>
    </div>
  )
}

export default function UsageCard() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function load() {
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`${API_BASE}/api/usage`)
      const json = await res.json()
      if (json.ok) setData(json)
      else setError(json.error || '取不到用量')
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  return (
    <div className="mt-6 rounded-2xl border border-border-main bg-bg-panel/60 p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-1.5 text-sm font-semibold text-text-main">
          <Coins size={16} weight="fill" /> 模型用量与花费
        </div>
        <button
          type="button"
          onClick={load}
          className="flex items-center gap-1 text-[11px] text-text-muted hover:text-text-main transition-colors cursor-pointer"
        >
          <ArrowsClockwise size={12} className={loading ? 'animate-spin' : ''} />
          刷新
        </button>
      </div>

      {error ? (
        <p className="text-xs text-status-error">{error}</p>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            <Cell label="今天" data={data?.today} />
            <Cell label="近 7 天" data={data?.days7} />
            <Cell label="近 30 天" data={data?.days30} />
          </div>

          {data?.by_use?.length > 0 && (
            <div className="mt-3 space-y-1">
              <div className="text-[11px] text-text-muted">近 30 天按用途</div>
              {data.by_use.map(([use, v]) => (
                <div key={use} className="flex items-center justify-between text-[11px] text-text-main">
                  <span className="truncate">{use}</span>
                  <span className="text-text-muted">{v.calls} 次 · {v.cost.toFixed(3)} 元</span>
                </div>
              ))}
            </div>
          )}

          {data?.recent?.length > 0 && (
            <div className="mt-3">
              <div className="text-[11px] text-text-muted mb-1">最近几次</div>
              <div className="space-y-1 max-h-40 overflow-auto">
                {data.recent.map((r, i) => (
                  <div key={i} className="flex items-center justify-between text-[10px] text-text-muted">
                    <span className="truncate mr-2">{r.time} · {r.use}</span>
                    <span className="shrink-0">入 {r.in} / 出 {r.out} · {r.cost.toFixed(4)} 元</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <p className="mt-3 text-[10px] leading-relaxed text-text-muted">
            计价口径：闲时 输入 1 元 / 输出 4 元 每百万 tokens，高峰（工作日 9-12 点、14-18 点）翻倍。
            一次调用重试几次就记几行，所以次数会比实际对话条数多。
            {(data?.total?.since) ? ` 记账起点 ${data.total.since}，之前的只能估算。` : ' 还没有记账。'}
            只统计 DeepSeek 文本模型的 token；火山语音转写、实时通话和别的厂商调用不在这一页。
          </p>
        </>
      )}
    </div>
  )
}
