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
  idle: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
  first_contact: 'badge-open',
  second_contact: 'badge-contacted',
  lawyer: 'badge-disputed',
  archived: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
  waiting: 'badge-promised',
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
      <div className="sc-card p-5">
        <div className="flex flex-col md:flex-row gap-4 items-start md:items-end">
          <div className="flex-1">
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Ricerca Azienda</label>
            <input
              type="text"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setSkip(0)
              }}
              placeholder="Ragione Sociale, P.IVA, Email..."
              className="sc-input w-full"
            />
          </div>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={onlyOverdue}
                onChange={(e) => { setOnlyOverdue(e.target.checked); setSkip(0) }}
                className="rounded border-dark-border bg-dark-surface text-accent-teal focus:ring-accent-teal"
              />
              <span className="text-sm font-medium text-txt-secondary">Solo con scadute</span>
            </label>
          </div>
        </div>
      </div>

      {/* Summary */}
      {customers.length > 0 && (
        <div className="bg-accent-red/5 rounded-xl p-4 border border-accent-red/20 flex items-center gap-6">
          <div>
            <p className="text-sm font-medium text-accent-red">Aziende con Fatture Scadute</p>
            <p className="text-2xl font-bold text-accent-red">{summaryOverdueCustomers}</p>
          </div>
          <div>
            <p className="text-sm font-medium text-accent-red">Totale Scaduto</p>
            <p className="text-2xl font-bold text-accent-red">
              {formatCurrency(summaryTotalOverdue)}
            </p>
          </div>
        </div>
      )}

      {/* Table */}
      <div className="sc-card overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-96">
            <svg className="animate-spin-slow w-8 h-8 text-accent-teal" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </div>
        ) : error ? (
          <div className="p-6 text-accent-red">{error}</div>
        ) : customers.length === 0 ? (
          <div className="p-6 text-center text-txt-muted">
            {onlyOverdue ? 'Nessuna azienda con fatture scadute' : 'Nessun cliente trovato'}
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-dark-surface border-b border-dark-border">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Azienda</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">P.IVA</th>
                    <th
                      className="px-4 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary"
                      onClick={() => handleSort('total_overdue')}
                    >
                      Scaduto{sortArrow('total_overdue')}
                    </th>
                    <th
                      className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary"
                      onClick={() => handleSort('overdue_count')}
                    >
                      Fatt. Scadute{sortArrow('overdue_count')}
                    </th>
                    <th
                      className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary"
                      onClick={() => handleSort('earliest_due_date')}
                    >
                      Scadenza{sortArrow('earliest_due_date')}
                    </th>
                    <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Stato</th>
                    <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Pross. Azione</th>
                    <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Telefono</th>
                    <th className="px-4 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Escluso</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-dark-border">
                  {customers.map(customer => (
                    <tr
                      key={customer.id}
                      className={`sc-table-row cursor-pointer ${
                        customer.excluded || excludedToggle[customer.id] ? 'opacity-50' : ''
                      }`}
                      onClick={() => navigate(`/customers/${customer.id}`)}
                    >
                      <td className="px-4 py-3">
                        <div className="text-sm font-medium text-accent-teal hover:text-accent-cyan">
                          {customer.ragione_sociale || customer.email || `Cliente #${customer.id}`}
                        </div>
                        {!customer.ragione_sociale && (
                          <span className="text-xs text-txt-muted">(nome mancante)</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-txt-muted font-mono text-xs">
                        {customer.partita_iva || '-'}
                      </td>
                      <td className="px-4 py-3 text-sm text-right font-bold">
                        {(customer.total_overdue || 0) > 0 ? (
                          <span className="text-accent-red">{formatCurrency(customer.total_overdue)}</span>
                        ) : (
                          <span className="text-txt-muted">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-center">
                        {customer.overdue_count > 0 ? (
                          <span className="bg-accent-red/15 text-accent-red px-2 py-0.5 rounded-full text-xs font-medium">
                            {customer.overdue_count}
                          </span>
                        ) : (
                          <span className="text-txt-muted text-xs">0</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-center text-txt-secondary">
                        {customer.earliest_due_date ? (
                          <span className="text-xs">{formatDate(customer.earliest_due_date)}</span>
                        ) : '-'}
                      </td>
                      <td className="px-4 py-3 text-sm text-center">
                        <span className={`${STATUS_COLORS[customer.recovery_status] || STATUS_COLORS.idle} sc-badge`}>
                          {STATUS_LABELS[customer.recovery_status] || 'Da Gestire'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-center text-txt-muted">
                        {customer.next_action_date ? (
                          <span className="text-xs">{formatDate(customer.next_action_date)}</span>
                        ) : '-'}
                      </td>
                      <td className="px-4 py-3 text-sm text-center">
                        {customer.phone ? (
                          <div className="flex items-center justify-center gap-1">
                            <span className="text-xs text-txt-secondary">{customer.phone}</span>
                            {customer.phone && (
                              <a
                                href={`https://wa.me/${customer.phone.replace(/[^+\d]/g, '')}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                onClick={(e) => e.stopPropagation()}
                                className="w-5 h-5 bg-accent-green text-dark-bg rounded-full text-xs flex items-center justify-center hover:brightness-110 font-bold"
                                title="WhatsApp"
                              >
                                W
                              </a>
                            )}
                          </div>
                        ) : (
                          <span className="text-txt-muted text-xs">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-center" onClick={(e) => e.stopPropagation()}>
                        <button
                          onClick={(e) => handleToggleExcluded(customer.id, !excludedToggle[customer.id], e)}
                          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                            excludedToggle[customer.id] ? 'bg-accent-red' : 'bg-accent-green'
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
            <div className="px-6 py-4 border-t border-dark-border flex items-center justify-between">
              <p className="text-sm text-txt-muted">
                Mostrando {skip + 1} a {Math.min(skip + limit, total)} di {total} aziende
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => setSkip(Math.max(0, skip - limit))}
                  disabled={skip === 0}
                  className="sc-btn-secondary disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Precedente
                </button>
                <button
                  onClick={() => setSkip(skip + limit)}
                  disabled={skip + limit >= total}
                  className="sc-btn-secondary disabled:opacity-50 disabled:cursor-not-allowed"
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
