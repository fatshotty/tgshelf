import { NavLink, Navigate, Route, Routes } from 'react-router-dom'

import BrowseView from './views/BrowseView'
import MetricsView from './views/MetricsView'
import SearchView from './views/SearchView'

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">tgshelf</span>
        <nav className="nav">
          <NavLink to="/b">Files</NavLink>
          <NavLink to="/stats">Stats</NavLink>
        </nav>
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/b" replace />} />
          <Route path="/b/*" element={<BrowseView />} />
          <Route path="/search" element={<SearchView />} />
          <Route path="/stats" element={<MetricsView />} />
          <Route path="*" element={<Navigate to="/b" replace />} />
        </Routes>
      </main>
    </div>
  )
}
