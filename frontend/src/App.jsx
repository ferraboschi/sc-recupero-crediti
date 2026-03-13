import React, { useState } from 'react'
import { Routes, Route, Link, useLocation } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Positions from './pages/Positions'
import Customers from './pages/Customers'
import ClientDetail from './pages/ClientDetail'
import Attivita from './pages/Attivita'
import System from './pages/System'
import SyncButton from './components/SyncButton'

export default function App() {
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(true)

  const navItems = [
    { path: '/', label: 'Dashboard', icon: '📊' },
    { path: '/attivita', label: 'Attività', icon: '📅' },
    { path: '/customers', label: 'Clienti', icon: '👥' },
    { path: '/positions', label: 'Fatture', icon: '📋' },
    { path: '/system', label: 'Sistema', icon: '⚙️' },
  ]

  const isActive = (path) => {
    if (path === '/customers') {
      return location.pathname === '/customers' || location.pathname.startsWith('/customers/')
    }
    if (path === '/attivita') {
      return location.pathname === '/attivita'
    }
    return location.pathname === path
  }

  return (
    <div className="flex h-screen bg-slate-50">
      {/* Sidebar */}
      <div className={`${sidebarOpen ? 'w-64' : 'w-20'} bg-slate-800 text-white transition-all duration-300 flex flex-col border-r border-slate-700`}>
        {/* Logo */}
        <div className="p-4 border-b border-slate-700">
          <div className="flex items-center gap-3">
            <div className="text-2xl">💳</div>
            {sidebarOpen && <span className="font-bold text-lg">SC Crediti</span>}
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 p-4 space-y-2">
          {navItems.map(item => (
            <Link
              key={item.path}
              to={item.path}
              className={`flex items-center gap-3 px-4 py-3 rounded-lg transition-colors ${
                isActive(item.path)
                  ? 'bg-blue-600 text-white'
                  : 'text-slate-300 hover:bg-slate-700'
              }`}
            >
              <span className="text-xl">{item.icon}</span>
              {sidebarOpen && <span className="font-medium">{item.label}</span>}
            </Link>
          ))}
        </nav>

        {/* Toggle Button */}
        <div className="p-4 border-t border-slate-700">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="w-full flex items-center justify-center p-2 rounded-lg hover:bg-slate-700 transition-colors"
          >
            {sidebarOpen ? '◀' : '▶'}
          </button>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex flex-col">
        {/* Top Bar */}
        <div className="bg-white border-b border-slate-200 px-8 py-4 flex items-center justify-between shadow-sm">
          <div className="flex items-center gap-3">
            <div className="text-3xl">💳</div>
            <div>
              <h1 className="text-2xl font-bold text-slate-900">SC Recupero Crediti</h1>
              <p className="text-xs text-slate-500">
                {navItems.find(item => isActive(item.path))?.label || 'Dashboard'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <SyncButton />
            <div className="flex items-center gap-2 text-sm text-slate-600">
              <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse"></div>
              Sistema Attivo
            </div>
          </div>
        </div>

        {/* Content Area */}
        <div className="flex-1 overflow-auto p-8">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/attivita" element={<Attivita />} />
            <Route path="/customers" element={<Customers />} />
            <Route path="/customers/:customerId" element={<ClientDetail />} />
            <Route path="/positions" element={<Positions />} />
            <Route path="/system" element={<System />} />
          </Routes>
        </div>
      </div>
    </div>
  )
}
