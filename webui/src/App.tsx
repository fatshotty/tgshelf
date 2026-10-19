import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'

import { api } from './api/client'
import BrowseView from './views/BrowseView'
import MetricsView from './views/MetricsView'
import SearchView from './views/SearchView'
import OperationsView from './views/OperationsView'

export default function App() {
  const info = useQuery({ queryKey: ['instance-info'], queryFn: api.getInfo, staleTime: Infinity })
  const name = info.data?.name || 'tgshelf'

  useEffect(() => {
    document.title = `${name} — tgshelf`
  }, [name])

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand" title={name}>{name}</span>
        <nav className="nav">
          <NavLink to="/">Files</NavLink>
          <NavLink to="/stats">Stats</NavLink>
          <NavLink to="/operations">Operations</NavLink>
        </nav>
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<BrowseView />} />
          <Route path="/search" element={<SearchView />} />
          <Route path="/stats" element={<MetricsView />} />
          <Route path="/operations" element={<OperationsView />} />
          <Route path="/*" element={<BrowseView />} />
        </Routes>
      </main>
    </div>
  )
}
