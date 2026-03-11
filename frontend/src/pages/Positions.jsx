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

  useEffect(() => {
    const fetchPositions = async () => {
      try {
        setLoading(true)
        const params = {
          skip,
          limit,
        }
        if (statusFilter) params.status = statusFilter
        if (escalationFilter) params.escalation_level = parseInt(escalationFilter)
        if (minAmountFilter) params.min_amount = parseFloat(minAmountFilter)
        if (searchFilter) params.search = searchFilter

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
  }, [skip, limit, statusFilter, escalationFilter, minAmountFilter, searchFilter])

  const formatCurrency = (value) => {
    return new Intl.NumberFormat('it-IT', {
      style: 'currency',
      currency: 'EUR',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value)
  }

  const getStatusColor = (status) => {
    const statusObj = STATUSES.find(s => s.value === status)
    if (!statusObj) return 'gray'
    return statusObj.color
  }

  const getStatusLabel = (status) => {
    const statusObj = STATUSES.find(s => s.value === status)
    return statusObj?.label || status
  }

  return (
    <div className="space-y-6">
      {/* Filters */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <h2 className="text-lg font-bold text-slate-900 mb-4">Filtri</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Stato</label>
            <select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value)
                setSkip(0)
              }}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Tutti</option>
              {STATUSES.map(status => (
                <option key={status.value} value={status.value}>
                  {status.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Livello Escalation</label>
            <select
              value={escalationFilter}
              onChange={(e) => {
                setEscalationFilter(e.target.value)
                setSkip(0)
              }}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Tutti</option>
              <option value="0">Livello 0</option>
              <option value="1">Livello 1</option>
              <option value="2">Livello 2</option>
              <option value="3">Livello 3</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Importo Minimo (€)</label>
            <input
              type="number"
              value={minAmountFilter}
              onChange={(e) => {
                setMinAmountFilter(e.target.value)
                setSkip(0)
              }}
              placeholder="0"
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">Ricerca</label>
            <input
              type="text"
              value={searchFilter}
              onChange={(e) => {
                setSearchFilter(e.target.value)
                setSkip(0)
              }}
              placeholder="Cliente, Fattura..."
              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>
      </div>

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
                    <th className="px-6 py-3 text-left text-sm font-semibold text-slate-900">Cliente</th>
                    <th className="px-6 py-3 text-left text-sm font-semibold text-slate-900">Fattura</th>
                    <th className="px-6 py-3 text-right text-sm font-semibold text-slate-900">Importo</th>
                    <th className="px-6 py-3 text-right text-sm font-semibold text-slate-900">Saldo</th>
                    <th className="px-6 py-3 text-left text-sm font-semibold text-slate-900">Scadenza</th>
                    <th className="px-6 py-3 text-right text-sm font-semibold text-slate-900">Giorni Ritardo</th>
                    <th className="px-6 py-3 text-center text-sm font-semibold text-slate-900">Livello</th>
                    <th className="px-6 py-3 text-center text-sm font-semibold text-slate-900">Stato</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {positions.map(pos => (
                    <tr key={pos.id} className="hover:bg-slate-50 cursor-pointer">
                      <td className="px-6 py-3 text-sm text-slate-900">
                        {pos.customer?.ragione_sociale || 'Non assegnato'}
                      </td>
                      <td className="px-6 py-3 text-sm text-slate-600">
                        {pos.invoice_number}
                      </td>
                      <td className="px-6 py-3 text-sm text-right font-medium text-slate-900">
                        {formatCurrency(pos.amount)}
                      </td>
                      <td className="px-6 py-3 text-sm text-right font-medium text-slate-900">
                        {formatCurrency(pos.amount_due)}
                      </td>
                      <td className="px-6 py-3 text-sm text-slate-600">
                        {pos.due_date ? new Date(pos.due_date).toLocaleDateString('it-IT') : '-'}
                      </td>
                      <td className="px-6 py-3 text-sm text-right">
                        <span className={pos.days_overdue > 30 ? 'text-red-600 font-medium' : 'text-slate-600'}>
                          {pos.days_overdue || 0}
                        </span>
                      </td>
                      <td className="px-6 py-3 text-sm text-center text-slate-600">-</td>
                      <td className="px-6 py-3 text-sm text-center">
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
