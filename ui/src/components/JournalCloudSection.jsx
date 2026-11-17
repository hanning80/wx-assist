import { useState, useEffect } from 'react'
import { motion } from 'framer-motion'

/**
 * 系统配置·日记与复盘：云存储。
 * 底层可在阿里云 OSS / 腾讯云 COS 之间切换；业务层不感知厂商。
 */
export default function JournalCloudSection() {
  const [info, setInfo] = useState(null)
  const [provider, setProvider] = useState('oss')
  const [form, setForm] = useState({
    oss_bucket: '',
    oss_endpoint: '',
    oss_region: '',
    oss_prefix: '',
    oss_access_key_id: '',
    oss_access_key_secret: '',
    cos_bucket: '',
    cos_region: '',
    cos_prefix: '',
    cos_secret_id: '',
    cos_secret_key: '',
    media_upload: true,
    media_rag: false,
  })
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [restoreInfo, setRestoreInfo] = useState(null)
  const [restoring, setRestoring] = useState(false)
  const [restoreMsg, setRestoreMsg] = useState(null)

  async function load() {
    try {
      const r = await fetch('/api/journal/cloud')
      const j = await r.json()
      if (j.ok) {
        setInfo(j)
        setProvider(j.provider || 'oss')
        setForm((old) => ({
          oss_bucket: old.oss_bucket || j.oss_bucket || '',
          oss_endpoint: old.oss_endpoint || j.oss_endpoint || '',
          oss_region: old.oss_region || j.oss_region || '',
          oss_prefix: old.oss_prefix || j.oss_prefix || '',
          oss_access_key_id: '',
          oss_access_key_secret: '',
          cos_bucket: old.cos_bucket || j.cos_bucket || '',
          cos_region: old.cos_region || j.cos_region || 'ap-beijing',
          cos_prefix: old.cos_prefix || j.cos_prefix || '',
          cos_secret_id: '',
          cos_secret_key: '',
          media_upload: j.media_upload !== false,
          media_rag: j.media_rag === true,
        }))
      }
    } catch { /* 服务没起来就算了 */ }
  }

  useEffect(() => { load(); loadRestore() }, [])

  async function loadRestore() {
    try {
      const r = await fetch('/api/journal/restore/latest')
      const j = await r.json()
      if (j.ok) setRestoreInfo(j)
    } catch { /* 服务没起来就算了 */ }
  }

  async function runRestore() {
    if (!window.confirm('从云端恢复会下载最新全文包和全部聊天增量，生成一个新的恢复目录；当前正在用的日记库不会被覆盖。继续吗？')) return
    setRestoring(true)
    setRestoreMsg(null)
    try {
      const r = await fetch('/api/journal/restore/run', { method: 'POST' })
      const j = await r.json()
      if (j.ok) {
        const c = j.counts || {}
        setRestoreMsg({
          type: 'ok',
          text: `恢复完成：聊天 ${c.messages || 0} 条、日记 ${c.diary || 0} 条、收藏 ${c.favorites || 0} 条、增量 ${j.increments || 0} 个。恢复目录：${j.out_dir}`,
        })
      } else {
        setRestoreMsg({ type: 'err', text: j.error || '恢复失败' })
      }
    } catch (e) {
      setRestoreMsg({ type: 'err', text: '请求没发出去：' + e.message })
    } finally {
      setRestoring(false)
    }
  }

  function set(k, v) {
    setForm((old) => ({ ...old, [k]: v }))
  }

  async function submit(testOnly) {
    setBusy(true)
    setMsg(null)
    try {
      const r = await fetch('/api/journal/cloud', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, test_only: testOnly, ...form }),
      })
      const j = await r.json()
      if (j.ok) {
        setMsg({ type: 'ok', text: j.message || '连接成功' })
        if (!testOnly) {
          setForm((old) => ({
            ...old,
            oss_access_key_id: '', oss_access_key_secret: '',
            cos_secret_id: '', cos_secret_key: '',
          }))
        }
        load()
      } else {
        setMsg({ type: 'err', text: (j.error || '连接失败') + (j.hint ? '（' + j.hint + '）' : '') })
      }
    } catch (e) {
      setMsg({ type: 'err', text: '请求没发出去：' + e.message })
    } finally {
      setBusy(false)
    }
  }

  const isOss = provider === 'oss'
  const configured = isOss
    ? (info?.oss_bucket && info?.oss_access_key_id_set && info?.oss_access_key_secret_set)
    : (info?.cos_bucket && info?.cos_secret_id_set && info?.cos_secret_key_set)
  const canSubmit = isOss ? !!form.oss_bucket.trim() : !!form.cos_bucket.trim()
  const canTest = isOss ? !!info?.oss_access_key_id_set : !!info?.cos_secret_id_set

  const providerLabel = info?.provider_name || (isOss ? '阿里云 OSS' : '腾讯云 COS')

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <h4 className="text-sm font-bold text-text-main">云端存储（{providerLabel}）</h4>
          {configured ? (
            <span className="text-[11px] px-2 py-0.5 rounded-full bg-brand-green/10 text-brand-green">已配置</span>
          ) : (
            <span className="text-[11px] px-2 py-0.5 rounded-full bg-status-warn/10 text-status-warn">未配置</span>
          )}
          {info?.provider === provider && (
            <span className="text-[11px] px-2 py-0.5 rounded-full bg-brand-green/10 text-brand-green">当前主仓</span>
          )}
        </div>
        <p className="text-xs text-text-sub leading-relaxed">
          日记、收藏、公众号全文、聊天记录和复盘会以明文文本存进你自己的私有存储桶；换电脑时可以从云端整库恢复。底层可在阿里云 OSS 与腾讯云 COS 之间切换，业务代码不需要改。图片和视频原件按下面的开关决定是否上云；是否让图片文字和视频转写参与问答也单独控制。
        </p>
      </div>

      <div className="rounded-xl border border-border-main bg-bg-sub/60 p-4">
        <div className="text-xs font-semibold text-text-main mb-2">选哪家云存储</div>
        <div className="flex flex-wrap gap-2">
          {(info?.providers || [
            { value: 'oss', label: '阿里云 OSS', ready: true },
            { value: 'cos', label: '腾讯云 COS', ready: false },
          ]).filter((p) => p.value !== 'weiyun').map((p) => (
            <button
              key={p.value}
              onClick={() => {
                if (p.value !== (info?.provider || provider)) {
                  if (!window.confirm(
                    '切到「' + p.label + '」并保存后，旧云仓「' + (info?.provider_name || providerLabel) +
                    '」根目录下的数据会被全部清空，只保留这一边。确定切换吗？'
                  )) return
                }
                setProvider(p.value)
                setMsg(null)
              }}
              className={'px-3 py-1.5 text-xs rounded-lg border ' + (
                provider === p.value
                  ? 'border-brand-green bg-brand-green/10 text-brand-green'
                  : 'border-border-main text-text-sub hover:text-text-main'
              )}
            >
              {p.label}{p.ready ? '（已配）' : '（未配）'}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-text-sub leading-relaxed mt-2">
          同一时间只用一家云仓：切换到另一家并保存后，旧云仓根目录下的对象会被<b className="text-text-main">全部清空</b>，
          一份数据只留一边，不会两边各存一份、各付一份费用。
        </p>
      </div>

      <div className="rounded-xl border border-border-main bg-bg-sub/60 p-4">
        <div className="text-xs font-semibold text-text-main mb-2">怎么填</div>
        <ol className="text-xs text-text-sub leading-relaxed space-y-1 list-decimal list-inside">
          {isOss ? (
            <>
              <li>存储桶名称填你在阿里云创建的那个桶，本机现在是 <b>zhaixingriji-kb-2026</b></li>
              <li>服务端点填桶所在地域的公网端点，本机现在是 <b>https://oss-cn-beijing.aliyuncs.com</b></li>
              <li>访问密钥 ID 和密码来自阿里云 RAM 子账号；密码只在保存时写进本机 <code>.env</code>，页面不会回显</li>
            </>
          ) : (
            <>
              <li>存储桶名称填腾讯云 COS 的桶名，例如 <b>zhaixingriji-1250000000</b>，要带 APPID 后缀</li>
              <li>地域填桶所在地域，例如 <b>ap-beijing</b></li>
              <li>SecretId / SecretKey 来自腾讯云访问管理控制台；只在保存时写进本机 <code>.env</code>，页面不会回显</li>
            </>
          )}
        </ol>
      </div>

      {isOss ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-text-sub mb-1.5">存储桶名称</label>
            <input
              value={form.oss_bucket}
              onChange={(e) => set('oss_bucket', e.target.value)}
              placeholder="zhaixingriji-kb-2026"
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">服务端点</label>
            <input
              value={form.oss_endpoint}
              onChange={(e) => set('oss_endpoint', e.target.value)}
              placeholder="https://oss-cn-beijing.aliyuncs.com"
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">地域</label>
            <input
              value={form.oss_region}
              onChange={(e) => set('oss_region', e.target.value)}
              placeholder="oss-cn-beijing"
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">云端根目录</label>
            <input
              value={form.oss_prefix}
              onChange={(e) => set('oss_prefix', e.target.value)}
              placeholder="摘星日记"
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">访问密钥 ID</label>
            <input
              type="password"
              value={form.oss_access_key_id}
              onChange={(e) => set('oss_access_key_id', e.target.value)}
              placeholder={info?.oss_access_key_id_set ? '已保存：' + (info.oss_access_key_id_preview || '') + '（要换就贴新的）' : '以 LTAI 开头的一串字符'}
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">访问密钥密码</label>
            <input
              type="password"
              value={form.oss_access_key_secret}
              onChange={(e) => set('oss_access_key_secret', e.target.value)}
              placeholder={info?.oss_access_key_secret_set ? '已保存，留空不改' : '创建密钥时只显示一次，请整段粘贴'}
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-text-sub mb-1.5">存储桶名称</label>
            <input
              value={form.cos_bucket}
              onChange={(e) => set('cos_bucket', e.target.value)}
              placeholder="zhaixingriji-1250000000"
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">地域</label>
            <input
              value={form.cos_region}
              onChange={(e) => set('cos_region', e.target.value)}
              placeholder="ap-beijing"
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">云端根目录</label>
            <input
              value={form.cos_prefix}
              onChange={(e) => set('cos_prefix', e.target.value)}
              placeholder="摘星日记"
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">SecretId</label>
            <input
              type="password"
              value={form.cos_secret_id}
              onChange={(e) => set('cos_secret_id', e.target.value)}
              placeholder={info?.cos_secret_id_set ? '已保存：' + (info.cos_secret_id_preview || '') + '（要换就贴新的）' : '腾讯云访问管理里的 SecretId'}
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
          <div>
            <label className="block text-xs text-text-sub mb-1.5">SecretKey</label>
            <input
              type="password"
              value={form.cos_secret_key}
              onChange={(e) => set('cos_secret_key', e.target.value)}
              placeholder={info?.cos_secret_key_set ? '已保存，留空不改' : '腾讯云访问管理里的 SecretKey'}
              className="w-full px-3 py-2 text-sm rounded-lg border border-border-main bg-bg-card text-text-main outline-none focus:border-brand-green"
            />
          </div>
        </div>
      )}

      <div className="rounded-xl border border-border-main bg-bg-sub/60 p-4 space-y-3">
        <div className="text-xs font-semibold text-text-main">图片与视频</div>
        <label className="flex items-start gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={!!form.media_upload}
            onChange={(e) => set('media_upload', e.target.checked)}
            className="mt-0.5 accent-[#2f9e6f]"
          />
          <span className="text-xs text-text-sub leading-relaxed">
            <b className="text-text-main">允许图片视频上云</b>：原件、缩略图和识别出来的文字进云端。
            关闭后新收到的图片/视频不上云，也不在媒体库建档；已经上云的历史资产不自动删除。
          </span>
        </label>
        <label className="flex items-start gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={!!form.media_rag}
            onChange={(e) => set('media_rag', e.target.checked)}
            className="mt-0.5 accent-[#2f9e6f]"
          />
          <span className="text-xs text-text-sub leading-relaxed">
            <b className="text-text-main">允许参与问答检索</b>：图片里的文字、视频转写出来的文字进入问答检索。
            默认关闭；关闭后媒体仍可上云保存和查看，但不会被知识库问答搜到。
          </span>
        </label>
        <p className="text-[11px] text-text-sub leading-relaxed">
          费用口径：对象存储只对存储和“从云端下载”的流量收费，上传不收费。图片/视频通常不是问答检索的消费大头，
          真正会涨的是大量视频原件的存储，以及有人反复点开视频产生的下行流量；这两个开关可以先只关检索，保留原件备份。
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <motion.button
          whileTap={{ scale: 0.97 }}
          disabled={busy || !canSubmit}
          onClick={() => submit(false)}
          className="px-4 py-2 text-sm rounded-lg bg-brand-green text-white font-medium disabled:opacity-40"
        >
          {busy ? '连接中…' : '保存并测试'}
        </motion.button>
        <button
          disabled={busy || !canTest}
          onClick={() => submit(true)}
          className="px-3 py-2 text-sm rounded-lg border border-border-main text-text-sub hover:text-text-main disabled:opacity-40"
        >
          测试现有
        </button>
        {msg && (
          <div className={'text-xs ' + (msg.type === 'ok' ? 'text-brand-green' : 'text-status-danger')}>
            {msg.text}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs">
        <div className="rounded-lg border border-border-main p-3">
          <div className="text-text-sub mb-1">当前云仓</div>
          <div className="text-text-main font-medium">{info?.provider_name || providerLabel}</div>
        </div>
        <div className="rounded-lg border border-border-main p-3">
          <div className="text-text-sub mb-1">云端根目录</div>
          <div className="text-text-main font-medium">{isOss ? (info?.oss_prefix || '摘星日记') : (info?.cos_prefix || '摘星日记')}</div>
        </div>
        <div className="rounded-lg border border-border-main p-3">
          <div className="text-text-sub mb-1">最近一次上传快照</div>
          <div className="text-text-main font-medium">{info?.last_snapshot ? info.last_snapshot.replace('T', ' ').slice(0, 16) : '还没有'}</div>
        </div>
        <div className="rounded-lg border border-border-main p-3">
          <div className="text-text-sub mb-1">文本是否加密</div>
          <div className="text-text-main font-medium">明文，便于其他工具读取</div>
        </div>
      </div>

      <div className="rounded-xl border border-border-main bg-bg-sub/60 p-4 space-y-3">
        <div className="text-xs font-semibold text-text-main">换机一键恢复</div>
        <p className="text-xs text-text-sub leading-relaxed">
          从当前云仓下载最新全文包和全部聊天增量，合并后生成一个新的恢复目录。
          不会覆盖这台机器正在用的数据库；确认数据没问题后再切换。
        </p>
        <div className="text-[11px] text-text-sub">
          云端最新备份：{restoreInfo?.filename || '还没有'}
          {restoreInfo?.filename ? `（${(restoreInfo.file_size / 1024 / 1024).toFixed(1)} MB）` : ''}
          {restoreInfo?.has_manifest ? '，聊天清单已就绪' : '，聊天清单缺失'}
        </div>
        {restoreInfo?.has_manifest && (
          <div className="text-[11px] text-text-sub">
            清单游标：聊天 rowid {restoreInfo.manifest_last_rowid || 0}
            {restoreInfo.manifest_exported_at ? `，最近导出 ${restoreInfo.manifest_exported_at.replace('T', ' ')}` : ''}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <button
            disabled={restoring}
            onClick={runRestore}
            className="px-3 py-2 text-sm rounded-lg border border-border-main text-text-main hover:border-brand-green disabled:opacity-40"
          >
            {restoring ? '恢复中…' : '从云端恢复'}
          </button>
          {restoreMsg && (
            <div className={'text-xs ' + (restoreMsg.type === 'ok' ? 'text-brand-green' : 'text-status-danger')}>
              {restoreMsg.text}
            </div>
          )}
        </div>
      </div>

      <p className="text-[11px] text-text-sub leading-relaxed">
        说明：访问密钥只存在本机 <code>.env</code>，不会写进日志或网页；页面只显示尾号。私有桶不能匿名访问，程序取文件时用一小时有效的签名地址。
        同一时间只允许一家云仓生效；切换并保存时，旧云仓的数据会被清空，只在新云仓保留一份。
      </p>
    </div>
  )
}
