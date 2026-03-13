import React, { useState, useEffect, useCallback } from 'react'
import { Routes, Route, Link, useLocation } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Positions from './pages/Positions'
import Customers from './pages/Customers'
import ClientDetail from './pages/ClientDetail'
import Attivita from './pages/Attivita'
import System from './pages/System'
import Login from './pages/Login'
import SyncButton from './components/SyncButton'

/* ── SVG icon components ─────────────────────────────────────────── */
function IconDashboard() {
  return (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z" />
    </svg>
  )
}
function IconActivity() {
  return (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
    </svg>
  )
}
function IconCustomers() {
  return (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  )
}
function IconInvoices() {
  return (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
    </svg>
  )
}
function IconSystem() {
  return (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  )
}

export default function App() {
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(true)

  // ── Auth state ──────────────────────────────────────────────────
  const [isAuthenticated, setIsAuthenticated] = useState(() => {
    const token = localStorage.getItem('sc_token')
    const expires = localStorage.getItem('sc_token_expires')
    if (!token || !expires) return false
    return new Date(expires) > new Date()
  })

  const handleLogin = useCallback((data) => {
    setIsAuthenticated(true)
  }, [])

  const handleLogout = useCallback(() => {
    localStorage.removeItem('sc_token')
    localStorage.removeItem('sc_user')
    localStorage.removeItem('sc_token_expires')
    setIsAuthenticated(false)
  }, [])

  // Check token expiration periodically
  useEffect(() => {
    const interval = setInterval(() => {
      const expires = localStorage.getItem('sc_token_expires')
      if (expires && new Date(expires) <= new Date()) {
        handleLogout()
      }
    }, 60000) // check every minute
    return () => clearInterval(interval)
  }, [handleLogout])

  // If not authenticated, show login
  if (!isAuthenticated) {
    return <Login onLogin={handleLogin} />
  }

  const navItems = [
    { path: '/', label: 'Dashboard', icon: <IconDashboard />, hint: 'Panoramica crediti' },
    { path: '/attivita', label: 'Attivita', icon: <IconActivity />, hint: 'Azioni di recupero' },
    { path: '/customers', label: 'Clienti', icon: <IconCustomers />, hint: 'Anagrafica debitori' },
    { path: '/positions', label: 'Fatture', icon: <IconInvoices />, hint: 'Elenco fatture' },
    { path: '/system', label: 'Sistema', icon: <IconSystem />, hint: 'Diagnostica' },
  ]

  const isActive = (path) => {
    if (path === '/customers') {
      return location.pathname === '/customers' || location.pathname.startsWith('/customers/')
    }
    if (path === '/attivita') return location.pathname === '/attivita'
    return location.pathname === path
  }

  const currentPage = navItems.find(item => isActive(item.path))

  return (
    <div className="flex h-screen bg-dark-bg">
      {/* ── Sidebar ────────────────────────────────────────────── */}
      <div className={`${sidebarOpen ? 'w-56' : 'w-16'} bg-dark-surface border-r border-dark-border transition-all duration-300 flex flex-col`}>
        {/* Logo */}
        <div className="h-14 flex items-center px-4 border-b border-dark-border">
          <div className="w-7 h-7 rounded-lg bg-accent-teal flex items-center justify-center text-dark-bg font-bold text-sm flex-shrink-0">
            SC
          </div>
          {sidebarOpen && (
            <span className="ml-3 font-semibold text-sm text-txt-primary tracking-wide">
              Recupero Crediti
            </span>
          )}
        </div>

        {/* Navigation */}
        <nav className="flex-1 py-3 px-2 space-y-0.5">
          {navItems.map(item => (
            <Link
              key={item.path}
              to={item.path}
              title={!sidebarOpen ? item.label : undefined}
              className={`group flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all text-sm ${
                isActive(item.path)
                  ? 'bg-accent-teal/10 text-accent-teal border-l-2 border-accent-teal'
                  : 'text-txt-secondary hover:bg-dark-card hover:text-txt-primary border-l-2 border-transparent'
              }`}
            >
              <span className="flex-shrink-0">{item.icon}</span>
              {sidebarOpen && (
                <div className="flex flex-col min-w-0">
                  <span className="font-medium">{item.label}</span>
                  {!isActive(item.path) && (
                    <span className="text-[10px] text-txt-muted truncate">{item.hint}</span>
                  )}
                </div>
              )}
            </Link>
          ))}
        </nav>

        {/* Toggle */}
        <div className="px-2 py-3 border-t border-dark-border">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="w-full flex items-center justify-center p-2 rounded-lg hover:bg-dark-card text-txt-muted hover:text-txt-secondary transition-colors"
          >
            <svg className={`w-4 h-4 transition-transform ${sidebarOpen ? '' : 'rotate-180'}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
            </svg>
          </button>
        </div>
      </div>

      {/* ── Main Content ───────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top Bar */}
        <div className="h-14 bg-dark-surface border-b border-dark-border px-6 flex items-center justify-between flex-shrink-0">
          <div className="flex items-center gap-3">
            <h1 className="text-base font-semibold text-txt-primary">
              {currentPage?.label || 'Dashboard'}
            </h1>
            {currentPage?.hint && (
              <span className="text-xs text-txt-muted hidden sm:block">
                — {currentPage.hint}
              </span>
            )}
          </div>
          <div className="flex items-center gap-4">
            <SyncButton />
            <div className="flex items-center gap-2 text-xs text-txt-muted">
              <div className="w-1.5 h-1.5 bg-accent-green rounded-full pulse-glow"></div>
              Attivo
            </div>
            <button
              onClick={handleLogout}
              title="Esci"
              className="p-1.5 rounded-lg hover:bg-dark-card text-txt-muted hover:text-accent-red transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
              </svg>
            </button>
          </div>
        </div>

        {/* Content Area */}
        <div className="flex-1 overflow-auto p-6">
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
