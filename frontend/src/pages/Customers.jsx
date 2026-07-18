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

// Ordinamenti disponibili (rispecchiano list_customers lato backend).
const SORT_OPTIONS = [
  { value: 'total_overdue', label: 'Scaduto (€)' },
  { value: 'overdue_count', label: 'N. fatture scadute' },
  { value: 'days_overdue', label: 'Giorni di scaduto' },
  { value: 'last_action', label: 'Ultimo sollecito' },
  { value: 'earliest_due_date', label: 'Scadenza più vicina' },
  { value: 'ragione_sociale', label: 'Nome (A→Z)' },
]

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
  const [toSanitize, setToSanitize] = useState(false)
  const [noPhone, setNoPhone] = useState(false)
  const [recoveryStatus, setRecoveryStatus] = useState('')
  // 'all' = tutti, 'hide' = nascondi esclusi, 'only' = solo esclusi
  const [excludedFilter, setExcludedFilter] = useState('all')
  const [sortBy, setSortBy] = useState('total_overdue')
  const [sortOrder, setSortOrder] = useState('desc')
  const [excludedToggle, setExcludedToggle] = useState({})
  const [summaryTotalOverdue, setSummaryTotalOverdue] = useState(0)
  const [summaryOverdueCustomers, setSummaryOverdueCustomers] = useState(0)
  const [suggestions, setSuggestions] = useState([])
  const [sanitizeCount, setSanitizeCount] = useState(null)
  const [bonificaCount, setBonificaCount] = useState(0)

  // Parametri correnti della lista (un solo punto di verità, usato anche
  // dalla richiesta dedicata al riepilogo).
  const buildListParams = () => {
    const params = { skip, limit, only_overdue: onlyOverdue, sort_by: sortBy, sort_order: sortOrder }
    if (search) params.search = search
    if (toSanitize) params.to_sanitize = true
    if (noPhone) params.no_phone = true
    if (recoveryStatus) params.recovery_status = recoveryStatus
    if (excludedFilter === 'hide') params.excluded = false
    if (excludedFilter === 'only') params.excluded = true
    return params
  }

  // Il riepilogo dice "fuori dai totali": DEVE escludere gli esclusi anche
  // quando il filtro li MOSTRA in lista. Richiesta dedicata con
  // excluded=false (limit minimo: servono solo i summary_* del backend).
  const fetchSummary = async () => {
    try {
      const params = { ...buildListParams(), excluded: false, skip: 0, limit: 1 }
      const res = await client.get('/customers', { params })
      setSummaryTotalOverdue(res.data.summary_total_overdue || 0)
      setSummaryOverdueCustomers(res.data.summary_overdue_customers || 0)
    } catch (err) {
      console.error('Errore aggiornamento riepilogo:', err)
    }
  }

  useEffect(() => {
    let cancelled = false
    const fetchCustomers = async () => {
      try {
        setLoading(true)
        const params = buildListParams()

        // In modalità 'nascondi' la lista è già excluded=false: i suoi
        // summary_* sono giusti. Negli altri casi il riepilogo arriva da
        // una richiesta parallela che esclude gli esclusi.
        const needsSummaryRequest = excludedFilter !== 'hide'
        const [response, summaryRes] = await Promise.all([
          client.get('/customers', { params }),
          needsSummaryRequest
            ? client.get('/customers', { params: { ...params, excluded: false, skip: 0, limit: 1 } })
            : null,
        ])
        if (cancelled) return
        setCustomers(response.data.items)
        setTotal(response.data.total)
        const summarySource = summaryRes ? summaryRes.data : response.data
        setSummaryTotalOverdue(summarySource.summary_total_overdue || 0)
        setSummaryOverdueCustomers(summarySource.summary_overdue_customers || 0)

        const toggleState = {}
        response.data.items.forEach(c => {
          toggleState[c.id] = c.excluded
        })
        setExcludedToggle(toggleState)
      } catch (err) {
        if (cancelled) return
        setError('Errore nel caricamento dei clienti')
        console.error(err)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchCustomers()
    return () => { cancelled = true }
  }, [skip, limit, search, onlyOverdue, toSanitize, noPhone, recoveryStatus, excludedFilter, sortBy, sortOrder])

  // Conteggio globale "da sanificare" (audit): un solo giro, indipendente
  // dai filtri della lista. Alimenta il chip/contatore cliccabile.
  const fetchSanitizeCount = async () => {
    try {
      const res = await client.get('/customers/audit-summary')
      setSanitizeCount(res.data.to_sanitize_count)
    } catch (err) {
      console.error('Errore audit-summary:', err)
      setSanitizeCount(null)
    }
  }
  useEffect(() => { fetchSanitizeCount() }, [])

  // Quanti clienti sono bonificabili in blocco (P.IVA sulle fatture, assente
  // sul profilo). Alimenta il banner-scorciatoia verso la vista di revisione.
  const fetchBonificaCount = async () => {
    try {
      const res = await client.get('/customers/bonifica-suggestions')
      setBonificaCount(res.data.total || 0)
    } catch (err) {
      console.error('Errore bonifica-suggestions:', err)
      setBonificaCount(0)
    }
  }
  useEffect(() => { fetchBonificaCount() }, [])

  // "Forse intendevi": suggerimenti approssimati (accenti/forme legali/
  // refusi tollerati). Debounced, su TUTTI i clienti (anche senza scadute).
  useEffect(() => {
    const q = search.trim()
    if (q.length < 2) {
      setSuggestions([])
      return
    }
    let cancelled = false
    const t = setTimeout(async () => {
      try {
        const res = await client.get('/customers/suggest', { params: { q, limit: 6 } })
        if (!cancelled) setSuggestions(res.data.items || [])
      } catch {
        if (!cancelled) setSuggestions([])
      }
    }, 300)
    return () => { cancelled = true; clearTimeout(t) }
  }, [search])

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
      // Il riepilogo cambia in ENTRAMBE le direzioni (escluso ↔ riportato
      // nel recupero): si riallinea subito, senza aspettare un reload.
      fetchSummary()
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
      {/* Banner-scorciatoia: bonifica P.IVA in blocco. Compare SOLO quando c'è
          qualcosa da bonificare (come il chip "Da sanificare"), così non
          aggiunge rumore quando l'anagrafica è già completa. */}
      {bonificaCount > 0 && (
        <button
          onClick={() => navigate('/customers/bonifica')}
          className="w-full flex items-center justify-between gap-4 rounded-xl border border-accent-teal/40 bg-accent-teal/5 px-5 py-3 text-left hover:bg-accent-teal/10 transition-colors"
        >
          <div>
            <p className="text-sm font-semibold text-accent-teal">
              {bonificaCount} client{bonificaCount === 1 ? 'e' : 'i'} bonificabil{bonificaCount === 1 ? 'e' : 'i'} in blocco
            </p>
            <p className="text-xs text-txt-secondary mt-0.5">
              Hanno la P.IVA sulle fatture ma non sul profilo: completa l&apos;anagrafica in un colpo.
            </p>
          </div>
          <span className="text-accent-teal font-bold shrink-0" aria-hidden="true">Rivedi →</span>
        </button>
      )}

      {/* Filters */}
      <div className="sc-card p-5">
        <div className="flex flex-col lg:flex-row gap-4 lg:items-end">
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
          <div>
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Ordina per</label>
            <div className="flex gap-2">
              <select
                value={sortBy}
                onChange={(e) => { setSortBy(e.target.value); setSkip(0) }}
                className="sc-input"
              >
                {SORT_OPTIONS.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              <button
                onClick={() => { setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc'); setSkip(0) }}
                className="sc-btn-secondary px-3"
                title={sortOrder === 'asc' ? 'Crescente — clicca per decrescente' : 'Decrescente — clicca per crescente'}
              >
                {sortOrder === 'asc' ? '↑' : '↓'}
              </button>
            </div>
          </div>
        </div>

        {/* Chip filtri: viste rapide della lista. Un colore = un significato
            (ambra = da verificare/sanificare, teal = filtro attivo). */}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button
            onClick={() => { setOnlyOverdue(!onlyOverdue); setSkip(0) }}
            className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors ${
              onlyOverdue
                ? 'bg-accent-teal/15 text-accent-teal border-accent-teal/40'
                : 'bg-dark-surface text-txt-secondary border-dark-border hover:border-accent-teal/40'
            }`}
          >
            Solo con scadute
          </button>

          <button
            onClick={() => {
              const next = !toSanitize
              setToSanitize(next)
              // Attivandolo mostro TUTTI i clienti da sanificare, anche senza
              // scadute (un abbinamento sbagliato non dipende dallo scaduto).
              if (next) setOnlyOverdue(false)
              setSkip(0)
            }}
            className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors flex items-center gap-1.5 ${
              toSanitize
                ? 'bg-accent-amber/15 text-accent-amber border-accent-amber/40'
                : 'bg-dark-surface text-txt-secondary border-dark-border hover:border-accent-amber/40'
            }`}
            title="Clienti con abbinamenti da controllare o suggerimenti in attesa"
          >
            Da sanificare
            {sanitizeCount !== null && (
              <span className={`px-1.5 py-0.5 rounded-full text-xs font-bold ${
                sanitizeCount > 0 ? 'bg-accent-amber/25 text-accent-amber' : 'bg-accent-green/20 text-accent-green'
              }`}>
                {sanitizeCount}
              </span>
            )}
          </button>

          <button
            onClick={() => { setNoPhone(!noPhone); setSkip(0) }}
            className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors ${
              noPhone
                ? 'bg-accent-teal/15 text-accent-teal border-accent-teal/40'
                : 'bg-dark-surface text-txt-secondary border-dark-border hover:border-accent-teal/40'
            }`}
            title="Clienti senza telefono: non sollecitabili via WhatsApp"
          >
            Senza telefono
          </button>

          <select
            value={recoveryStatus}
            onChange={(e) => { setRecoveryStatus(e.target.value); setSkip(0) }}
            className="sc-input py-1.5 text-sm"
            title="Filtra per stato pratica"
          >
            <option value="">Tutte le pratiche</option>
            {Object.entries(STATUS_LABELS).map(([key, label]) => (
              <option key={key} value={key}>{label}</option>
            ))}
          </select>

          <select
            value={excludedFilter}
            onChange={(e) => { setExcludedFilter(e.target.value); setSkip(0) }}
            className="sc-input py-1.5 text-sm"
            title="Gli esclusi sono fuori dai totali della cascata"
          >
            <option value="all">Esclusi: mostra</option>
            <option value="hide">Esclusi: nascondi</option>
            <option value="only">Solo esclusi</option>
          </select>
        </div>

        {/* "Forse intendevi": suggerimenti approssimati non già in elenco */}
        {(() => {
          const shown = new Set(customers.map(c => c.id))
          const alt = suggestions.filter(s => !shown.has(s.id))
          if (alt.length === 0) return null
          return (
            <div className="mt-4 pt-4 border-t border-dark-border">
              <p className="text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">
                Forse intendevi
              </p>
              <div className="flex flex-wrap gap-2">
                {alt.map(s => (
                  <button
                    key={s.id}
                    onClick={() => navigate(`/customers/${s.id}`)}
                    title={`P.IVA: ${s.partita_iva || '—'} · corrispondenza ${s.score}%`}
                    className="group flex items-center gap-2 px-3 py-1.5 rounded-lg bg-dark-surface border border-dark-border hover:border-accent-teal/50 hover:bg-dark-cardHover transition-colors"
                  >
                    <span className="text-sm font-medium text-txt-primary group-hover:text-accent-teal">
                      {s.ragione_sociale}
                    </span>
                    {s.overdue_count > 0 && (
                      <span className="sc-badge bg-accent-red/15 text-accent-red">
                        {s.overdue_count} scadut{s.overdue_count === 1 ? 'a' : 'e'}
                      </span>
                    )}
                    {s.excluded && (
                      <span className="sc-badge bg-[rgba(148,163,184,0.15)] text-txt-muted">escluso</span>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )
        })()}
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
                      onClick={() => handleSort('days_overdue')}
                    >
                      Giorni{sortArrow('days_overdue')}
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
                  {customers.map(customer => {
                    // Un solo punto di verità per riga: il toggle locale
                    // (inizializzato dal server) — così escludere E
                    // ri-includere si riflettono subito, simmetricamente.
                    const isExcluded = excludedToggle[customer.id] ?? customer.excluded
                    const displayName = customer.ragione_sociale || customer.email || `Cliente #${customer.id}`
                    return (
                    <tr
                      key={customer.id}
                      className={`sc-table-row cursor-pointer ${isExcluded ? 'opacity-50' : ''}`}
                      onClick={() => navigate(`/customers/${customer.id}`)}
                    >
                      <td className="px-4 py-3">
                        <div className="text-sm font-medium text-accent-teal hover:text-accent-cyan">
                          {displayName}
                        </div>
                        {!customer.ragione_sociale && (
                          <span className="text-xs text-txt-muted">(nome mancante)</span>
                        )}
                        {isExcluded && (
                          <span
                            className="mt-1 inline-block sc-badge bg-[rgba(148,163,184,0.15)] text-txt-muted"
                            title="Cliente escluso: non conteggiato nei totali della cascata di riconciliazione"
                          >
                            Escluso · fuori dai totali
                          </span>
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
                      <td className="px-4 py-3 text-sm text-center">
                        {(customer.max_days_overdue || 0) > 0 ? (
                          <span className={`text-xs font-medium ${customer.max_days_overdue > 30 ? 'text-accent-red' : 'text-accent-amber'}`}>
                            +{customer.max_days_overdue}gg
                          </span>
                        ) : (
                          <span className="text-txt-muted text-xs">-</span>
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
                        {customer.last_action && (
                          <span className="block text-[10px] text-txt-muted mt-0.5" title="Ultimo sollecito registrato">
                            ult. sollecito {formatDate(customer.last_action)}
                          </span>
                        )}
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
                          onClick={(e) => handleToggleExcluded(customer.id, !isExcluded, e)}
                          role="switch"
                          aria-checked={isExcluded}
                          aria-label={isExcluded
                            ? `Riporta ${displayName} nel recupero`
                            : `Escludi ${displayName} dal recupero`}
                          title={isExcluded
                            ? `Riporta ${displayName} nel recupero`
                            : `Escludi ${displayName} dal recupero`}
                          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                            isExcluded ? 'bg-accent-red' : 'bg-accent-green'
                          }`}
                        >
                          <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                            isExcluded ? 'translate-x-4.5' : 'translate-x-0.5'
                          }`} />
                        </button>
                      </td>
                    </tr>
                    )
                  })}
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
