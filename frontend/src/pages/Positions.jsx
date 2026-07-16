import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
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

const OVERDUE_OPTIONS = [
  { value: '', label: 'Tutti' },
  { value: 'yes', label: 'Scaduti' },
  { value: 'no', label: 'Non Scaduti' },
]

const CUSTOMER_OPTIONS = [
  { value: '', label: 'Tutti' },
  { value: 'yes', label: 'Solo con Cliente Associato' },
  { value: 'no', label: 'Solo senza Cliente Associato' },
]

const PAYMENT_OPTIONS = [
  { value: 'exclude_paid', label: 'Escludi Pagati' },
  { value: '', label: 'Tutti gli Stati' },
  { value: 'open', label: 'Solo Aperti' },
  { value: 'paid', label: 'Solo Pagati' },
  { value: 'contacted', label: 'Solo Contattati' },
  { value: 'promised', label: 'Solo Promessi' },
  { value: 'disputed', label: 'Solo Contestati' },
]

export default function Positions() {
  const navigate = useNavigate()
  const [positions, setPositions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [skip, setSkip] = useState(0)
  const [total, setTotal] = useState(0)
  const [limit] = useState(50)

  // Filters
  const [statusFilter, setStatusFilter] = useState('exclude_paid')
  const [minAmountFilter, setMinAmountFilter] = useState('')
  const [searchFilter, setSearchFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [overdueFilter, setOverdueFilter] = useState('yes')
  const [hasCustomerFilter, setHasCustomerFilter] = useState('')
  const [issueDateFrom, setIssueDateFrom] = useState('')
  const [issueDateTo, setIssueDateTo] = useState('')
  const [dueDateFrom, setDueDateFrom] = useState('')
  const [dueDateTo, setDueDateTo] = useState('')
  const [sortBy, setSortBy] = useState('')
  const [sortOrder, setSortOrder] = useState('desc')
  const [showDateFilters, setShowDateFilters] = useState(false)

  // CSV Import
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState(null)

  // Status update feedback
  const [updatingId, setUpdatingId] = useState(null)
  const [summaryTotalAmountDue, setSummaryTotalAmountDue] = useState(0)
  const [positionsRefreshKey, setPositionsRefreshKey] = useState(0)

  // Suggestions ("Da confermare")
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [suggestions, setSuggestions] = useState([])
  const [suggestionsTotal, setSuggestionsTotal] = useState(0)
  const [suggestionsLoading, setSuggestionsLoading] = useState(false)
  const [suggestionsError, setSuggestionsError] = useState(null)
  const [suggestionActingId, setSuggestionActingId] = useState(null)

  useEffect(() => {
    const fetchPositions = async () => {
      try {
        setLoading(true)
        const params = { skip, limit }
        if (statusFilter === 'exclude_paid') {
          params.exclude_status = 'paid'
        } else if (statusFilter) {
          params.status = statusFilter
        }
        if (minAmountFilter) params.min_amount = parseFloat(minAmountFilter)
        if (searchFilter) params.search = searchFilter
        if (sourceFilter) params.source = sourceFilter
        if (overdueFilter) params.overdue = overdueFilter
        if (hasCustomerFilter) params.has_customer = hasCustomerFilter
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
        setSummaryTotalAmountDue(response.data.summary_total_amount_due || 0)
      } catch (err) {
        setError('Errore nel caricamento delle posizioni')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchPositions()
  }, [skip, limit, statusFilter, minAmountFilter, searchFilter,
      sourceFilter, overdueFilter, hasCustomerFilter, issueDateFrom, issueDateTo,
      dueDateFrom, dueDateTo, sortBy, sortOrder, positionsRefreshKey])

  const fetchSuggestions = async () => {
    try {
      setSuggestionsLoading(true)
      setSuggestionsError(null)
      const response = await client.get('/positions/suggestions')
      setSuggestions(response.data.items)
      setSuggestionsTotal(response.data.total)
    } catch (err) {
      setSuggestionsError('Errore nel caricamento dei suggerimenti')
      console.error(err)
    } finally {
      setSuggestionsLoading(false)
    }
  }

  useEffect(() => {
    fetchSuggestions()
  }, [])

  const handleConfirmSuggestion = async (sug) => {
    // Sui suggerimenti a bassa confidenza la conferma immediata era
    // un'asimmetria pericolosa (Rifiuta chiedeva conferma, Conferma no).
    if (sug.low_confidence && !window.confirm(
      `Suggerimento a BASSA CONFIDENZA (${sug.suggested_method} ${formatScore(sug.suggested_score)}): `
      + `abbinare davvero la fattura ${sug.invoice_number} a "${sug.suggested_customer?.ragione_sociale}"?`
    )) return
    try {
      setSuggestionActingId(sug.id)
      await client.post(`/positions/${sug.id}/confirm-suggestion`)
      await fetchSuggestions()
      setPositionsRefreshKey(k => k + 1)
    } catch (err) {
      setSuggestionsError('Errore durante la conferma del suggerimento')
      console.error(err)
    } finally {
      setSuggestionActingId(null)
    }
  }

  const handleCreateCustomer = async (sug) => {
    // Terza via quando il suggerimento è sbagliato: prima l'operatore era
    // spinto a confermare l'errore, e Rifiuta rendeva la fattura 'unlinked'
    // per sempre (l'auto-create del sync salta i suggerimenti pendenti).
    if (!window.confirm(
      `Verrà creato il NUOVO cliente "${sug.customer_name_raw}" e la fattura `
      + `${sug.invoice_number} gli verrà abbinata. Continuare?`
    )) return
    try {
      setSuggestionActingId(sug.id)
      await client.post(`/positions/${sug.id}/create-customer`)
      await fetchSuggestions()
      setPositionsRefreshKey(k => k + 1)
    } catch (err) {
      // Il 409 spiega quale cliente esiste già (usare Riassegna): mostrarlo.
      setSuggestionsError(err.response?.data?.detail || 'Errore durante la creazione del cliente')
      console.error(err)
    } finally {
      setSuggestionActingId(null)
    }
  }

  const handleRejectSuggestion = async (id) => {
    if (!window.confirm('La fattura resterà senza cliente e non verrà più proposta automaticamente. Continuare?')) return
    try {
      setSuggestionActingId(id)
      await client.post(`/positions/${id}/reject-suggestion`)
      await fetchSuggestions()
      setPositionsRefreshKey(k => k + 1)
    } catch (err) {
      setSuggestionsError('Errore durante il rifiuto del suggerimento')
      console.error(err)
    } finally {
      setSuggestionActingId(null)
    }
  }

  const formatScore = (score) => {
    if (score === null || score === undefined) return ''
    return Math.round(score <= 1 ? score * 100 : score)
  }

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
    const d = dateStr.length === 10 ? new Date(dateStr + 'T00:00:00') : new Date(dateStr)
    return d.toLocaleDateString('it-IT')
  }

  const getStatusLabel = (status) => {
    const statusObj = STATUSES.find(s => s.value === status)
    return statusObj?.label || status
  }

  const getStatusBadge = (status) => {
    const map = {
      open: 'badge-open',
      contacted: 'badge-contacted',
      promised: 'badge-promised',
      paid: 'badge-paid',
      disputed: 'badge-disputed',
      escalated: 'badge-escalated',
    }
    return map[status] || 'bg-[rgba(148,163,184,0.15)] text-txt-muted'
  }

  const getSourceLabel = (source) => {
    if (source === 'fatturapro') return 'FatturaPro'
    if (source === 'fatture24') return 'Fattura24'
    return source || '-'
  }

  const getSourceBadge = (source) => {
    if (source === 'fatturapro') return 'bg-[rgba(167,139,250,0.15)] text-accent-purple'
    if (source === 'fatture24') return 'bg-[rgba(45,212,191,0.15)] text-accent-teal'
    return 'bg-[rgba(148,163,184,0.15)] text-txt-muted'
  }

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

  const handleCsvImport = async (e) => {
    const file = e.target.files[0]
    if (!file) return

    setImporting(true)
    setImportResult(null)

    const formData = new FormData()
    formData.append('file', file)

    try {
      const response = await client.post('/sync/import-csv', formData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      })
      setImportResult(response.data)
      setSkip(0)
    } catch (err) {
      setImportResult({ errors: [err.message || 'Errore durante l\'importazione'] })
    } finally {
      setImporting(false)
      e.target.value = ''
    }
  }

  return (
    <div className="space-y-6">
      {/* View tabs */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => setShowSuggestions(false)}
          className={!showSuggestions
            ? 'px-4 py-2 rounded-lg text-sm font-medium bg-accent-teal/15 text-accent-teal border border-accent-teal/30 transition-colors'
            : 'sc-btn-secondary'}
        >
          Fatture
        </button>
        <button
          onClick={() => setShowSuggestions(true)}
          className={showSuggestions
            ? 'px-4 py-2 rounded-lg text-sm font-medium bg-accent-teal/15 text-accent-teal border border-accent-teal/30 transition-colors'
            : 'sc-btn-secondary'}
        >
          Da confermare ({suggestionsTotal})
        </button>
      </div>

      {showSuggestions ? (
        /* Suggestions ("Da confermare") */
        <div className="sc-card overflow-hidden">
          <div className="sc-card-header">
            <h2 className="sc-section-title">Abbinamenti da Confermare</h2>
            <span className="text-sm text-txt-muted">{suggestionsTotal} in attesa</span>
          </div>

          {suggestionsError && (
            <div className="mx-5 mt-4 p-3 rounded-lg text-sm bg-accent-red/10 text-accent-red border border-accent-red/20">
              {suggestionsError}
            </div>
          )}

          {suggestionsLoading ? (
            <div className="flex items-center justify-center h-48">
              <svg className="animate-spin-slow w-8 h-8 text-accent-teal" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </div>
          ) : suggestions.length === 0 ? (
            <div className="p-6 text-center text-txt-muted">Nessun abbinamento da confermare ✓</div>
          ) : (
            <div className="divide-y divide-dark-border">
              {suggestions.map(sug => (
                <div key={sug.id} className="p-4 flex flex-wrap items-center gap-4">
                  <div className="w-44 shrink-0">
                    <p className="text-sm font-medium text-txt-primary">{sug.invoice_number}</p>
                    <p className="text-sm text-txt-secondary">{formatCurrency(sug.amount_due)}</p>
                    <div className="flex items-center gap-1.5 mt-0.5">
                      <span className="text-xs text-txt-muted">{formatDate(sug.issue_date)}</span>
                      <span className={`sc-badge ${getSourceBadge(sug.source_platform)}`}>
                        {getSourceLabel(sug.source_platform)}
                      </span>
                    </div>
                  </div>

                  <div className="flex-1 min-w-[180px]">
                    <p className="text-xs font-semibold text-txt-label uppercase tracking-wider mb-1">Destinatario fattura</p>
                    <p className="text-sm text-txt-primary">{sug.customer_name_raw || '-'}</p>
                    <p className="text-xs font-mono text-txt-muted">{sug.customer_piva_raw || '-'}</p>
                  </div>

                  <div className="text-txt-muted text-lg shrink-0">→</div>

                  <div className="flex-1 min-w-[180px]">
                    <p className="text-xs font-semibold text-txt-label uppercase tracking-wider mb-1">Cliente suggerito</p>
                    <p className="text-sm text-accent-teal">{sug.suggested_customer?.ragione_sociale || '-'}</p>
                    <p className="text-xs font-mono text-txt-muted">{sug.suggested_customer?.partita_iva || '-'}</p>
                    <div className="flex items-center gap-2 mt-1">
                      <span className="sc-badge bg-[rgba(148,163,184,0.15)] text-txt-secondary">
                        {sug.suggested_method} {formatScore(sug.suggested_score)}
                      </span>
                      {sug.low_confidence && (
                        <span className="sc-badge bg-[rgba(251,191,36,0.15)] text-accent-amber">bassa confidenza</span>
                      )}
                    </div>
                  </div>

                  <div className="flex gap-2 shrink-0">
                    <button
                      onClick={() => handleConfirmSuggestion(sug)}
                      disabled={suggestionActingId === sug.id}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-accent-green/15 hover:bg-accent-green/25 text-accent-green border border-accent-green/30 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Conferma
                    </button>
                    <button
                      onClick={() => handleCreateCustomer(sug)}
                      disabled={suggestionActingId === sug.id || !sug.customer_name_raw}
                      title={sug.customer_name_raw
                        ? `Crea il cliente "${sug.customer_name_raw}" e abbina la fattura`
                        : 'La fattura non ha un nome destinatario'}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-accent-teal/10 hover:bg-accent-teal/20 text-accent-teal border border-accent-teal/30 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Crea nuovo cliente
                    </button>
                    <button
                      onClick={() => handleRejectSuggestion(sug.id)}
                      disabled={suggestionActingId === sug.id}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-transparent hover:bg-accent-red/10 text-accent-red border border-accent-red/40 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Rifiuta
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
      <>
      {/* Filters */}
      <div className="sc-card p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="sc-section-title">Filtri e Ricerca</h2>
          <div className="flex items-center gap-3">
            <label className={`px-4 py-2 rounded-lg text-sm font-medium cursor-pointer transition-colors
              ${importing ? 'bg-dark-card text-txt-muted' : 'sc-btn-primary'}`}>
              {importing ? 'Importando...' : 'Importa CSV (Fattura24)'}
              <input
                type="file"
                accept=".csv,.tsv,.txt"
                onChange={handleCsvImport}
                disabled={importing}
                className="hidden"
              />
            </label>
          </div>
        </div>

        {importResult && (
          <div className={`mb-4 p-3 rounded-lg text-sm ${importResult.errors?.length > 0 && !importResult.created
            ? 'bg-accent-red/10 text-accent-red border border-accent-red/20'
            : 'bg-accent-green/10 text-accent-green border border-accent-green/20'}`}>
            {importResult.created !== undefined && (
              <p>Importazione completata: {importResult.created} create, {importResult.updated} aggiornate, {importResult.skipped} saltate su {importResult.total_rows} righe.</p>
            )}
            {importResult.errors?.length > 0 && importResult.errors.map((err, i) => (
              <p key={i} className="text-accent-red">{err}</p>
            ))}
            <button onClick={() => setImportResult(null)} className="mt-1 text-xs underline">Chiudi</button>
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div>
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Ricerca Cliente/Fattura</label>
            <input
              type="text"
              value={searchFilter}
              onChange={(e) => { setSearchFilter(e.target.value); setSkip(0) }}
              placeholder="Nome cliente, P.IVA, Fattura..."
              className="sc-input w-full"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Fonte</label>
            <select
              value={sourceFilter}
              onChange={(e) => { setSourceFilter(e.target.value); setSkip(0) }}
              className="sc-input w-full"
            >
              {SOURCES.map(s => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Scaduto</label>
            <select
              value={overdueFilter}
              onChange={(e) => { setOverdueFilter(e.target.value); setSkip(0) }}
              className="sc-input w-full"
            >
              {OVERDUE_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Cliente Associato</label>
            <select
              value={hasCustomerFilter}
              onChange={(e) => { setHasCustomerFilter(e.target.value); setSkip(0) }}
              className="sc-input w-full"
            >
              {CUSTOMER_OPTIONS.map(c => (
                <option key={c.value} value={c.value}>{c.label}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mt-4">
          <div>
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Stato Pagamento</label>
            <select
              value={statusFilter}
              onChange={(e) => { setStatusFilter(e.target.value); setSkip(0) }}
              className="sc-input w-full"
            >
              {PAYMENT_OPTIONS.map(p => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Importo Minimo</label>
            <input
              type="number"
              value={minAmountFilter}
              onChange={(e) => { setMinAmountFilter(e.target.value); setSkip(0) }}
              placeholder="0"
              className="sc-input w-full"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Ordina Per</label>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="sc-input w-full"
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
            className="text-sm font-medium text-accent-teal hover:text-accent-cyan flex items-center gap-1 transition-colors"
          >
            {showDateFilters ? '▼' : '▶'} Filtri per Data
          </button>

          {showDateFilters && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mt-3">
              <div>
                <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Emissione Dal</label>
                <input type="date" value={issueDateFrom} onChange={(e) => { setIssueDateFrom(e.target.value); setSkip(0) }}
                  className="sc-input w-full" />
              </div>
              <div>
                <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Emissione Al</label>
                <input type="date" value={issueDateTo} onChange={(e) => { setIssueDateTo(e.target.value); setSkip(0) }}
                  className="sc-input w-full" />
              </div>
              <div>
                <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Scadenza Dal</label>
                <input type="date" value={dueDateFrom} onChange={(e) => { setDueDateFrom(e.target.value); setSkip(0) }}
                  className="sc-input w-full" />
              </div>
              <div>
                <label className="block text-xs font-semibold text-txt-label uppercase tracking-wider mb-2">Scadenza Al</label>
                <input type="date" value={dueDateTo} onChange={(e) => { setDueDateTo(e.target.value); setSkip(0) }}
                  className="sc-input w-full" />
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Summary Stats */}
      {positions.length > 0 && (
        <div className="bg-accent-blue/5 rounded-xl p-4 border border-accent-blue/20 flex items-center gap-6">
          <div>
            <p className="text-sm font-medium text-accent-blue">Totale Scaduto (filtro attivo)</p>
            <p className="text-2xl font-bold text-accent-blue">{formatCurrency(summaryTotalAmountDue)}</p>
          </div>
          <div>
            <p className="text-sm font-medium text-accent-blue">Posizioni Totali</p>
            <p className="text-2xl font-bold text-accent-blue">{total}</p>
          </div>
          <div>
            <p className="text-sm font-medium text-accent-blue">Mostrate in Pagina</p>
            <p className="text-2xl font-bold text-accent-blue">{positions.length} di {total}</p>
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
        ) : positions.length === 0 ? (
          <div className="p-6 text-center text-txt-muted">Nessuna posizione trovata</div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-dark-surface border-b border-dark-border">
                  <tr>
                    <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Fonte</th>
                    <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Cliente</th>
                    <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">P.IVA</th>
                    <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Fattura</th>
                    <th className="px-3 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary" onClick={() => handleSort('amount_due')}>
                      Saldo{sortArrow('amount_due')}
                    </th>
                    <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary" onClick={() => handleSort('issue_date')}>
                      Emissione{sortArrow('issue_date')}
                    </th>
                    <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary" onClick={() => handleSort('due_date')}>
                      Scadenza{sortArrow('due_date')}
                    </th>
                    <th className="px-3 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary" onClick={() => handleSort('days_overdue')}>
                      GG{sortArrow('days_overdue')}
                    </th>
                    <th className="px-3 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Stato</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-dark-border">
                  {positions.map(pos => (
                    <tr key={pos.id} className={`sc-table-row ${pos.status === 'paid' ? 'bg-accent-green/5 opacity-60' : ''}`}>
                      <td className="px-3 py-3 text-sm">
                        <span className={`sc-badge ${getSourceBadge(pos.source_platform)}`}>
                          {getSourceLabel(pos.source_platform)}
                        </span>
                      </td>
                      <td className="px-3 py-3 text-sm text-txt-primary">
                        <div className="flex flex-col">
                          {pos.customer?.id ? (
                            <span
                              className="text-accent-teal cursor-pointer hover:text-accent-cyan"
                              onClick={(e) => { e.stopPropagation(); navigate(`/customers/${pos.customer.id}`) }}
                            >
                              {pos.customer.ragione_sociale}
                            </span>
                          ) : (
                            <span className="text-txt-secondary">{pos.customer_name_raw || 'Non assegnato'}</span>
                          )}
                          {!pos.customer && pos.customer_name_raw && (
                            <span className="text-xs text-accent-amber">da verificare</span>
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-3 text-txt-muted font-mono text-xs">
                        {pos.customer?.partita_iva || '-'}
                      </td>
                      <td className="px-3 py-3 text-sm text-txt-secondary">
                        {pos.invoice_number}
                      </td>
                      <td className="px-3 py-3 text-sm text-right font-medium text-txt-primary">
                        {formatCurrency(pos.amount_due)}
                      </td>
                      <td className="px-3 py-3 text-sm text-txt-secondary">
                        {formatDate(pos.issue_date)}
                      </td>
                      <td className="px-3 py-3 text-sm text-txt-secondary">
                        <span className="inline-flex items-center gap-1.5">
                          {formatDate(pos.due_date)}
                          {pos.due_date && pos.due_date_source !== 'real' && (
                            <span
                              className="text-xs px-1.5 py-0.5 rounded bg-dark-surface text-txt-muted"
                              title="Scadenza stimata (emissione + 30gg): non presente nel gestionale"
                            >
                              stimata
                            </span>
                          )}
                        </span>
                      </td>
                      <td className="px-3 py-3 text-sm text-right">
                        <span className={`font-medium ${
                          pos.days_overdue > 180 ? 'text-accent-red bg-accent-red/10 px-1.5 py-0.5 rounded' :
                          pos.days_overdue > 90 ? 'text-accent-red' :
                          pos.days_overdue > 30 ? 'text-accent-amber' :
                          pos.days_overdue > 0 ? 'text-accent-amber' :
                          'text-txt-muted'
                        }`}>
                          {pos.days_overdue || 0}
                        </span>
                      </td>
                      <td className="px-3 py-3 text-sm text-center">
                        <span className={`${getStatusBadge(pos.status)} sc-badge`}>
                          {getStatusLabel(pos.status)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            <div className="px-6 py-4 border-t border-dark-border flex items-center justify-between">
              <p className="text-sm text-txt-muted">
                Mostrando {skip + 1} a {Math.min(skip + limit, total)} di {total} posizioni
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
      </>
      )}
    </div>
  )
}
