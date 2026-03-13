import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'

const STATUS_LABELS = {
  idle: 'Da Gestire',
  first_contact: 'I Contatto',
  second_contact: 'II Contatto',
  lawyer: 'Avvocato',
  archived: 'Archiviato',
  waiting: 'In Attesa',
}

const STATUS_COLORS = {
  idle: 'bg-slate-100 text-slate-600',
  first_contact: 'bg-blue-100 text-blue-700',
  second_contact: 'bg-amber-100 text-amber-700',
  lawyer: 'bg-red-100 text-red-700',
  archived: 'bg-slate-200 text-slate-500',
  waiting: 'bg-purple-100 text-purple-700',
}

export default function Customers() {
  const navigate = useNavigate()
  const [customers, setCustomers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [skip, setSkip] = useState(0)
  const [total, setTotal] = useState(0)
  const [limit] = useState(50)
  const [search, setSearch] = useState('')
  const [onlyOverdue, setOnlyOverdue] = useState(true)
  const [sortBy, setSortBy] = useState('total_overdue')
  const [sortOrder, setSortOrder] = useState('desc')
  const [excludedToggle, setExcludedToggle] = useState({})
  const [summaryTotalOverdue, setSummaryTotalOverdue] = useState(0)
  const [summaryOverdueCustomers, setSummaryOverdueCustomers] = useState(0)

  useEffect(() => {
    const fetchCustomers = async () => {
      try {
        setLoading(true)
        const params = { skip, limit, only_overdue: onlyOverdue, sort_by: sortBy, sort_order: sortOrder }
        if (search) params.search = search

        const response = await client.get('/customers', { params })
        setCustomers(response.data.items)
        setTotal(response.data.total)
        setSummaryTotalOverdue(response.data.summary_total_overdue || 0)
        setSummaryOverdueCustomers(response.data.summary_overdue_customers || 0)

        const toggleState = {}
        response.data.items.forEach(c => {
          toggleState[c.id] = c.excluded
        })
        setExcludedToggle(toggleState)
      } catch (err) {
        setError('Errore nel caricamento dei clienti')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchCustomers()
  }, [skip, limit, search, onlyOverdue, sortBy, sortOrder])

  const handleToggleExcluded = async (customerId, newValue, e) => {
    e.stopPropagation()
    try {
      await client.put(`/customers/${customerId}/exclude`, null, {
        params: { exclude: newValue },
      })
      setExcludedToggle({
        ...excludedToggle,
        [customerId]: newValue,
      })
    } catch (err) {
      console.error(err)
    }
  }

  const formatCurrency = (value) =>
    new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(value)

  const formatDate = (dateStr) => {
    if (!dateStr) return '-'
    const d = dateStr.length === 10 ? new Date(dateStr + 'T00:00:00') : new Date(dateStr)
    return d.toLocaleDateString('it-IT')
  }

  const handleSort = (field) => {
    if (sortBy === field) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc')
    } else {
      setSortBy(field)
      setSortOrder('desc')
    }
    setSkip(0)
  }

  const sortArrow = (field) => {
    if (sortBy !== field) return ''
    return sortOrder === 'asc' ? ' ↑' : ' ↓'
  }

  return (
    <div className="space-y-6">
      {/* Filters */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <div className="flex flex-col md:flex-row gap-4 items-start md:items-end">
          <div className="flex-1">
            <label className="block text-sm font-medium text-slate-700 mb-2">Ricerca Azienda</label>
            <input
              type="text"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setSkip(0)
              }}
              placeholder="Ragione Sociale, P.IVA, Email..."
              className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={onlyOverdue}
                onChange={(e) => { setOnlyOverdue(e.target.checked); setSkip(0) }}
                className="rounded border-slate-300 text-red-600 focus:ring-red-500"
              />
              <span className="text-sm font-medium text-slate-700">Solo con scadute</span>
            </label>
          </div>
        </div>
      </div>

      {/* Summary */}
      {customers.length > 0 && (
        <div className="bg-red-50 rounded-lg p-4 border border-red-200 flex items-center gap-6">
          <div>
            <p className="text-sm font-medium text-red-900">Aziende con Fatture Scadute</p>
            <p className="text-2xl font-bold text-red-600">{summaryOverdueCustomers}</p>
          </div>
          <div>
            <p className="text-sm font-medium text-red-900">Totale Scaduto</p>
            <p className="text-2xl font-bold text-red-600">
              {formatCurrency(summaryTotalOverdue)}
            </p>
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
        ) : customers.length === 0 ? (
          <div className="p-6 text-center text-slate-500">
            {onlyOverdue ? 'Nessuna azienda con fatture scadute' : 'Nessun cliente trovato'}
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-slate-50 border-b border-slate-200">
                  <tr>
                    <th className="px-4 py-3 text-left text-sm font-semibold text-slate-900">Azienda</th>
                    <th className="px-4 py-3 text-left text-sm font-semibold text-slate-900">P.IVA</th>
                    <th
                      className="px-4 py-3 text-right text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100"
                      onClick={() => handleSort('total_overdue')}
                    >
                      Scaduto{sortArrow('total_overdue')}
                    </th>
                    <th
                      className="px-4 py-3 text-center text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100"
                      onClick={() => handleSort('overdue_count')}
                    >
                      Fatt. Scadute{sortArrow('overdue_count')}
                    </th>
                    <th
                      className="px-4 py-3 text-center text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100"
                      onClick={() => handleSort('earliest_due_date')}
                    >
                      Scadenza{sortArrow('earliest_due_date')}
                    </th>
                    <th className="px-4 py-3 text-center text-sm font-semibold text-slate-900">Stato</th>
                    <th className="px-4 py-3 text-center text-sm font-semibold text-slate-900">Pross. Azione</th>
                    <th className="px-4 py-3 text-center text-sm font-semibold text-slate-900">Telefono</th>
                    <th className="px-4 py-3 text-center text-sm font-semibold text-slate-900">Escluso</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {customers.map(customer => (
                    <tr
                      key={customer.id}
                      className={`hover:bg-blue-50 cursor-pointer transition-colors ${
                        customer.excluded || excludedToggle[customer.id] ? 'opacity-50' : ''
                      }`}
                      onClick={() => navigate(`/customers/${customer.id}`)}
                    >
                      <td className="px-4 py-3">
                        <div className="text-sm font-medium text-blue-700 hover:text-blue-900">
                          {customer.ragione_sociale || customer.email || `Cliente #${customer.id}`}
                        </div>
                        {!customer.ragione_sociale && (
                          <span className="text-xs text-slate-400">(nome mancante)</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600 font-mono text-xs">
                        {customer.partita_iva || '-'}
                      </td>
                      <td className="px-4 py-3 text-sm text-right font-bold">
                        {(customer.total_overdue || 0) > 0 ? (
                          <span className="text-red-600">{formatCurrency(customer.total_overdue)}</span>
                        ) : (
                          <span className="text-slate-400">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-center">
                        {customer.overdue_count > 0 ? (
                          <span className="bg-red-100 text-red-700 px-2 py-0.5 rounded-full text-xs font-medium">
                            {customer.overdue_count}
                          </span>
                        ) : (
                          <span className="text-slate-400 text-xs">0</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-center text-slate-600">
                        {customer.earliest_due_date ? (
                          <span className="text-xs">{formatDate(customer.earliest_due_date)}</span>
                        ) : '-'}
                      </td>
                      <td className="px-4 py-3 text-sm text-center">
                        <span className={`${STATUS_COLORS[customer.recovery_status] || STATUS_COLORS.idle} px-2 py-0.5 rounded-full text-xs font-medium`}>
                          {STATUS_LABELS[customer.recovery_status] || 'Da Gestire'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-center text-slate-500">
                        {customer.next_action_date ? (
                          <span className="text-xs">{formatDate(customer.next_action_date)}</span>
                        ) : '-'}
                      </td>
                      <td className="px-4 py-3 text-sm text-center">
                        {customer.phone ? (
                          <div className="flex items-center justify-center gap-1">
                            <span className="text-xs text-slate-600">{customer.phone}</span>
                            {customer.phone && (
                              <a
                                href={`https://wa.me/${customer.phone.replace(/[^+\d]/g, '')}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                onClick={(e) => e.stopPropagation()}
                                className="w-5 h-5 bg-green-500 text-white rounded-full text-xs flex items-center justify-center hover:bg-green-600"
                                title="WhatsApp"
                              >
                                W
                              </a>
                            )}
                          </div>
                        ) : (
                          <span className="text-slate-400 text-xs">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-center" onClick={(e) => e.stopPropagation()}>
                        <button
                          onClick={(e) => handleToggleExcluded(customer.id, !excludedToggle[customer.id], e)}
                          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                            excludedToggle[customer.id] ? 'bg-red-500' : 'bg-green-500'
                          }`}
                        >
                          <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                            excludedToggle[customer.id] ? 'translate-x-4.5' : 'translate-x-0.5'
                          }`} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            <div className="px-6 py-4 border-t border-slate-200 flex items-center justify-between">
              <p className="text-sm text-slate-600">
                Mostrando {skip + 1} a {Math.min(skip + limit, total)} di {total} aziende
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
