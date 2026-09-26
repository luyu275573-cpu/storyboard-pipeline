import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from './api'

export function useApi<T>(path: string | null, version: number) {
  const [state, setState] = useState<{ data: T | null; error: string; loading: boolean }>({
    data: null, error: '', loading: false,
  })
  useEffect(() => {
    if (!path) { setState({ data: null, error: '', loading: false }); return }
    const controller = new AbortController()
    setState({ data: null, error: '', loading: true })
    api<T>(path, { signal: controller.signal }).then(data => {
      if (!controller.signal.aborted) setState({ data, error: '', loading: false })
    }).catch(error => {
      if (!controller.signal.aborted) setState({ data: null, error: String(error.message), loading: false })
    })
    return () => controller.abort()
  }, [path, version])
  return state
}

export function Icon({ path }: { path: string }) {
  return <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={path} /></svg>
}

export function Dialog({ title, busy, close, children }: { title: string; busy: boolean; close: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current!
    dialog.showModal()
    return () => dialog.close()
  }, [])
  return <dialog ref={ref} aria-labelledby="dialog-title" onCancel={event => {
    event.preventDefault(); if (!busy) close()
  }}><div className="dialog-heading"><h2 id="dialog-title">{title}</h2>
    <button className="icon-button" aria-label="关闭弹窗" disabled={busy} onClick={close}>×</button></div>{children}</dialog>
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return <div className="empty"><div className="empty-mark" aria-hidden="true">◇</div><h2>{title}</h2>{children}</div>
}

