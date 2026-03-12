import React, { useState, useEffect } from 'react'
import client from '../api/client'

const STATUSES = [
  { value: 'open', label: 'Aperto', color: 'blue' },
  { value: 'contacted', label: 'Contattato', color: 'amber' },
  { value: 'promised', label: 'Promesso', color: 'purple' },
  { value: 'paid', label: 'Pagato', color: 'green' },
  { value: 'disputed', label: 'Contestato', color: 'red' },
  { value: 'escalated', label: 'Escalato', color: 'orange' },
]

const SOURCES = [
  { value: '', label: 'Tutte' },
  { value: 'fatturapro', label: 'FatturaPro' },
  { value: 'fatture24', label: 'Fattura24' },
]

export default function Positions() {
  const [positions, setPositions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [skip, setSkip] = useState(0)
  const [total, setTotal] = useState(0)
  const [limit] = useState(50)

  // Filters
  const [statusFilter, setStatusFilter] = useState('')
  const [escalationFilter, setEscalationFilter] = useState('')
  const [minAmountFilter, setMinAmountFilter] = useState('')
  const [searchFilter, setSearchFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [issueDateFrom, setIssueDateFrom] = useState('')
  const [issueDateTo, setIssueDateTo] = useState('')
  const [dueDateFrom, setDueDateFrom] = useState('')
  const [dueDateTo, setDueDateTo] = useState('')
  const [sortBy, setSortBy] = useState('')
  const [sortOrder, setSortOrder] = useState('desc')
  const [showDateFilters, setShowDateFilters] = useState(false)

  useEffect(() => {
    const fetchPositions = async () => {
      try {
        setLoading(true)
        const params = { skip, limit }
        if (statusFilter) params.status = statusFilter
        if (escalationFilter) params.escalation_level = parseInt(escalationFilter)
        if (minAmountFilter) params.min_amount = parseFloat(minAmountFilter)
        if (searchFilter) params.search = searchFilter
        if (sourceFilter) params.source = sourceFilter
        if (issueDateFrom) params.issue_date_from = issueDateFrom
        if (issueDateTo) params.issue_date_to = issueDateTo
        if (dueDateFrom) params.due_date_from = dueDateFrom
        if (dueDateTo) params.due_date_to = dueDateTo
        if (sortBy) {
          params.sort_by = sortBy
          params.sort_order = sortOrder
        }

        const response = await client.get('/positions', { params })
        setPositions(response.data.items)
        setTotal(response.data.total)
      } catch (err) {
        setError('Errore nel caricamento delle posizioni')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchPositions()
  }, [skip, limit, statusFilter, escalationFilter, minAmountFilter, searchFilter,
      sourceFilter, issueDateFrom, issueDateTo, dueDateFrom, dueDateTo, sortBy, sortOrder])

  const formatCurrency = (value) => {
    return new Intl.NumberFormat('it-IT', {
      style: 'currency',
      currency: 'EUR',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value)
  }

  const formatDate = (dateStr) => {
    if (!dateStr) return '-'
    return new Date(dateStr).toLocaleDateString('it-IT')
  }

  const getStatusLabel = (status) => {
    const statusObj = STATUSES.find(s => s.value === status)
    return statusObj?.label || status
  }

  const getSourceLabel = (source) => {
    if (source === 'fatturapro') return 'FatturaPro'
    if (source === 'fatture24') return 'Fattura24'
    return source || '-'
  }

  const getSourceColor = (source) => {
    if (source === 'fatturapro') return 'bg-indigo-100 text-indigo-700'
    if (source === 'fatture24') return 'bg-teal-100 text-teal-700'
    return 'bg-slate-100 text-slate-600'
  }

  const totalAmountDue = positions.reduce((sum, pos) => sum + (pos.amount_due || 0), 0)

  const handleSort = (field) => {
    if (sortBy === field) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc')
    } else {
      setSortBy(field)
      setSortOrder('desc')
    }
  }

  const sortArrow = (field) => {
    if (sortBy !== field) return ''
    return sortOrder === 'asc' ? ' ↑' : ' ↓'
  }

  return (
    <div className="space-y-6">
      {/* Filters */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <h2 className="text-lg font-bold text-slate-900 mb-4">Filtri e Ricerca</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Ricerca Cliente/Fattura</label>
            <input
              type="text"
              value={searchFilter}
              onChange={(e) => { setSearchFilter(e.target.value); setSkip(0) }}
              placeholder="Nome cliente, P.IVA, Fattura..."
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Fonte</label>
            <select
              value={sourceFilter}
              onChange={(e) => { setSourceFilter(e.target.value); setSkip(0) }}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {SOURCES.map(s => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Stato</label>
            <select
              value={statusFilter}
              onChange={(e) => { setStatusFilter(e.target.value); setSkip(0) }}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Tutti</option>
              {STATUSES.map(status => (
                <option key={status.value} value={status.value}>{status.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Importo Minimo (€)</label>
            <input
              type="number"
              value={minAmountFilter}
              onChange={(e) => { setMinAmountFilter(e.target.value); setSkip(0) }}
              placeholder="0"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Ordina Per</label>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Nessuno</option>
              <option value="amount_due">Importo Dovuto</option>
              <option value="days_overdue">Giorni Ritardo</option>
              <option value="issue_date">Data Emissione</option>
              <option value="due_date">Data Scadenza</option>
            </select>
          </div>
        </div>

        {/* Date filters toggle */}
        <div className="mt-4">
          <button
            onClick={() => setShowDateFilters(!showDateFilters)}
            className="text-sm font-medium text-blue-600 hover:text-blue-800 flex items-center gap-1"
          >
            {showDateFilters ? '▼' : '▶'} Filtri per Data
          </button>

          {showDateFilters && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mt-3">
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-2">Emissione Dal</label>
                <input
                  type="date"
                  value={issueDateFrom}
                  onChange={(e) => { setIssueDateFrom(e.target.value); setSkip(0) }}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-2">Emissione Al</label>
                <input
                  type="date"
                  value={issueDateTo}
                  onChange={(e) => { setIssueDateTo(e.target.value); setSkip(0) }}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-2">Scadenza Dal</label>
                <input
                  type="date"
                  value={dueDateFrom}
                  onChange={(e) => { setDueDateFrom(e.target.value); setSkip(0) }}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-2">Scadenza Al</label>
                <input
                  type="date"
                  value={dueDateTo}
                  onChange={(e) => { setDueDateTo(e.target.value); setSkip(0) }}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Summary Stats */}
      {positions.length > 0 && (
        <div className="bg-blue-50 rounded-lg p-4 border border-blue-200 flex items-center gap-6">
          <div>
            <p className="text-sm font-medium text-blue-900">Totale Importo Dovuto</p>
            <p className="text-2xl font-bold text-blue-600">{formatCurrency(totalAmountDue)}</p>
          </div>
          <div>
            <p className="text-sm font-medium text-blue-900">Posizioni Mostrate</p>
            <p className="text-2xl font-bold text-blue-600">{positions.length} di {total}</p>
          </div>
        </div>
      )}

      {/* Table */}
      <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-96">
            <svg className="animate-spin-slow w-8 h-8 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </div>
        ) : error ? (
          <div className="p-6 text-red-600">{error}</div>
        ) : positions.length === 0 ? (
          <div className="p-6 text-center text-slate-500">Nessuna posizione trovata</div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-slate-50 border-b border-slate-200">
                  <tr>
                    <th className="px-4 py-3 text-left text-sm font-semibold text-slate-900">Fonte</th>
                    <th className="px-4 py-3 text-left text-sm font-semibold text-slate-900">Cliente</th>
                    <th className="px-4 py-3 text-left text-sm font-semibold text-slate-900">Fattura</th>
                    <th className="px-4 py-3 text-right text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100" onClick={() => handleSort('amount_due')}>
                      Saldo{sortArrow('amount_due')}
                    </th>
                    <th className="px-4 py-3 text-left text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100" onClick={() => handleSort('issue_date')}>
                      Emissione{sortArrow('issue_date')}
                    </th>
                    <th className="px-4 py-3 text-left text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100" onClick={() => handleSort('due_date')}>
                      Scadenza{sortArrow('due_date')}
                    </th>
                    <th className="px-4 py-3 text-right text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100" onClick={() => handleSort('days_overdue')}>
                      GG Ritardo{sortArrow('days_overdue')}
                    </th>
                    <th className="px-4 py-3 text-center text-sm font-semibold text-slate-900">Stato</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {positions.map(pos => (
                    <tr key={pos.id} className="hover:bg-slate-50 cursor-pointer">
                      <td className="px-4 py-3 text-sm">
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${getSourceColor(pos.source_platform)}`}>
                          {getSourceLabel(pos.source_platform)}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-900">
                        {pos.customer?.ragione_sociale || pos.customer_name_raw || 'Non assegnato'}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {pos.invoice_number}
                      </td>
                      <td className="px-4 py-3 text-sm text-right font-medium text-slate-900">
                        {formatCurrency(pos.amount_due)}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {formatDate(pos.issue_date)}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {formatDate(pos.due_date)}
                      </td>
                      <td className="px-4 py-3 text-sm text-right">
                        <span className={pos.days_overdue > 30 ? 'text-red-600 font-medium' : 'text-slate-600'}>
                          {pos.days_overdue || 0}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-center">
                        <span className={`badge-${pos.status} px-3 py-1 rounded-full text-xs font-medium`}>
                          {getStatusLabel(pos.status)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            <div className="px-6 py-4 border-t border-slate-200 flex items-center justify-between">
              <p className="text-sm text-slate-600">
                Mostrando {skip + 1} a {Math.min(skip + limit, total)} di {total} posizioni
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => setSkip(Math.max(0, skip - limit))}
                  disabled={skip === 0}
                  className="px-4 py-2 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Precedente
                </button>
                <button
                  onClick={() => setSkip(skip + limit)}
                  disabled={skip + limit >= total}
                  className="px-4 py-2 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Successivo
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
