import React, { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 日记与复盘：嵌现有服务里的 /journal 页面。
 * 不在这里重画界面，复盘的排版只有一处，网页和面板看到的是同一份。
 *
 * 页面高度跟随内容，不再在面板里套一个上下滚动的小框；
 * 打开周/月/年复盘后，固定显示“返回日记总览”按钮，避免找不到回头路。
 */
export default function JournalTab({ theme }) {
  const frameRef = useRef(null)
  const [isReview, setIsReview] = useState(false)

  const syncFrame = useCallback(() => {
    const frame = frameRef.current
    if (!frame) return
    try {
      const win = frame.contentWindow
      const doc = frame.contentDocument
      if (!win || !doc) return

      // 主题由面板传给 iframe；同源 localStorage 也能兜底，这里显式传一次更稳。
      win.postMessage({ type: 'journal-theme', theme }, '*')

      const path = win.location.pathname
      const params = new URLSearchParams(win.location.search)
      const review = path === '/journal/file'
        || path === '/journal/now'
        || params.get('period') === 'week'
        || params.get('period') === 'month'
        || params.get('period') === 'year'
      setIsReview(review)

      const height = Math.max(
        doc.documentElement?.scrollHeight || 0,
        doc.body?.scrollHeight || 0,
        480,
      )
      frame.style.height = `${height + 2}px`
    } catch {
      /* 同源页面读取失败时保留最小高度，不影响使用 */
    }
  }, [theme])

  useEffect(() => {
    const onResize = () => syncFrame()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [syncFrame])

  useEffect(() => {
    syncFrame()
  }, [theme, syncFrame])

  function handleLoad() {
    syncFrame()
    // 字体、图表渲染完成后再量一次，避免高度少几十像素又出现小滚动条。
    window.setTimeout(syncFrame, 120)
    window.setTimeout(syncFrame, 500)
  }

  function backToJournal() {
    const frame = frameRef.current
    if (!frame) return
    frame.contentWindow.location.href = '/journal'
  }

  return (
    <>
      <div className="w-full overflow-hidden rounded-xl bg-surface dark:bg-surface">
        <iframe
          ref={frameRef}
          title="日记与复盘"
          src="/journal"
          scrolling="no"
          onLoad={handleLoad}
          className="w-full border-0 block"
          style={{ minHeight: 'calc(100vh - 160px)' }}
        />
      </div>

      {isReview && (
        <button
          type="button"
          onClick={backToJournal}
          className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[160] rounded-full bg-brand-green px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-black/20 transition hover:brightness-110 active:scale-95"
        >
          ← 返回日记总览
        </button>
      )}
    </>
  )
}
