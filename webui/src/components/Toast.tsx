// Minimal transient notifications via context. useToast() returns a push function;
// each toast auto-dismisses after 4s. Used for mutation errors and the "background
// operation started" message on folder move/copy (202).
import { createContext, type ReactNode, useCallback, useContext, useState } from 'react'

type Kind = 'info' | 'error'
interface Item {
  id: number
  msg: string
  kind: Kind
}

let seq = 0 // module counter — avoids Date.now() for stable keys

const ToastCtx = createContext<(msg: string, kind?: Kind) => void>(() => {})

export function useToast() {
  return useContext(ToastCtx)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Item[]>([])
  const push = useCallback((msg: string, kind: Kind = 'info') => {
    const id = ++seq
    setItems((xs) => [...xs, { id, msg, kind }])
    setTimeout(() => setItems((xs) => xs.filter((t) => t.id !== id)), 4000)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>
            {t.msg}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
