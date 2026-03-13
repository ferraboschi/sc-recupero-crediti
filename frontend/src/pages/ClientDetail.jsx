import React, { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import client from '../api/client'

const ACTION_LABELS = {
  first_contact: 'I Contatto',
  second_contact: 'II Contatto',
  lawyer: 'Avvocato',
  archive: 'Archivia',
  wait: 'Attendi',
  note: 'Nota',
}

const ACTION_COLORS = {
  first_contact: 'bg-blue-600 hover:bg-blue-700',
  second_contact: 'bg-amber-600 hover:bg-amber-700',
  lawyer: 'bg-red-600 hover:bg-red-700',
  archive: 'bg-slate-500 hover:bg-slate-600',
  wait: 'bg-purple-600 hover:bg-purple-700',
}

const STATUS_LABELS = {
  idle: 'Da Gestire',
  first_contact: 'I Contatto',
  second_contact: 'II Contatto',
  lawyer: 'Avvocato',
  archived: 'Archiviato',
  waiting: 'In Attesa',
}

const STATUS_COLORS = {
  idle: 'bg-slate-100 text-slate-700',
  first_contact: 'bg-blue-100 text-blue-700',
  second_contact: 'bg-amber-100 text-amber-700',
  lawyer: 'bg-red-100 text-red-700',
  archived: 'bg-slate-200 text-slate-500',
  waiting: 'bg-purple-100 text-purple-700',
}

const INVOICE_STATUS_COLORS = {
  open: 'bg-blue-100 text-blue-700',
  contacted: 'bg-amber-100 text-amber-700',
  promised: 'bg-purple-100 text-purple-700',
  paid: 'bg-green-100 text-green-700',
  disputed: 'bg-red-100 text-red-700',
  escalated: 'bg-orange-100 text-orange-700',
}

const OUTCOME_LABELS = {
  contacted: 'Contattato',
  promised: 'Promessa Pagamento',
  partial_payment: 'Pagamento Parziale',
  paid: 'Pagato',
  unreachable: 'Irraggiungibile',
  disputed: 'Contestazione',
  no_answer: 'Non Risponde',
}

const OUTCOME_COLORS = {
  contacted: 'bg-blue-100 text-blue-700',
  promised: 'bg-amber-100 text-amber-700',
  partial_payment: 'bg-teal-100 text-teal-700',
  paid: 'bg-green-100 text-green-700',
  unreachable: 'bg-slate-100 text-slate-600',
  disputed: 'bg-red-100 text-red-700',
  no_answer: 'bg-orange-100 text-orange-700',
}

export default function ClientDetail() {
  const { customerId } = useParams()
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [actionLoading, setActionLoading] = useState(false)
  const [actionNotes, setActionNotes] = useState('')
  const [showNoteInput, setShowNoteInput] = useState(false)
  const [pdfLoading, setPdfLoading] = useState(false)
  const [singlePdfLoading, setSinglePdfLoading] = useState(null)
  const [selectedInvoices, setSelectedInvoices] = useState(new Set())
  const [phoneEdit, setPhoneEdit] = useState(null)
  const [updatingInvoice, setUpdatingInvoice] = useState(null)
  const [showAllInvoices, setShowAllInvoices] = useState(false)
  const [invoiceSortBy, setInvoiceSortBy] = useState('due_date')
  const [invoiceSortOrder, setInvoiceSortOrder] = useState('asc')
  // Navigation state
  const [neighbors, setNeighbors] = useState({ prev_id: null, next_id: null, position: null, total: null })
  // Action completion state
  const [completingAction, setCompletingAction] = useState(null)
  const [selectedOutcome, setSelectedOutcome] = useState('')
  const [copiedWhatsApp, setCopiedWhatsApp] = useState(false)
  const [promemoria, setPromemoria] = useState(false)
  const [selectedWhatsAppPhone, setSelectedWhatsAppPhone] = useState(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      const response = await client.get(`/customers/${customerId}`)
      setData(response.data)
      // Auto-select all overdue invoices
      const overdueIds = (response.data.invoices?.items || [])
        .filter(inv => inv.days_overdue > 0 && inv.status !== 'paid')
        .map(inv => inv.id)
      setSelectedInvoices(new Set(overdueIds))
    } catch (err) {
      setError('Errore nel caricamento del cliente')
      console.error(err)
    } finally {
      setLoading(false)
    }
  }, [customerId])

  const fetchNeighbors = useCallback(async () => {
    try {
      const response = await client.get(`/customers/${customerId}/neighbors`)
      setNeighbors(response.data)
    } catch (err) {
      console.error('Error fetching neighbors:', err)
    }
  }, [customerId])

  useEffect(() => {
    fetchData()
    fetchNeighbors()
  }, [fetchData, fetchNeighbors])

  const formatCurrency = (value) =>
    new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(value)

  const formatDate = (dateStr) => {
    if (!dateStr) return '-'
    // Append T00:00:00 to date-only strings to avoid UTC timezone shift (off-by-one day)
    const d = dateStr.length === 10 ? new Date(dateStr + 'T00:00:00') : new Date(dateStr)
    return d.toLocaleDateString('it-IT')
  }

  const handleAction = async (actionType) => {
    setActionLoading(true)
    try {
      await client.post(`/recovery/customers/${customerId}/actions`, {
        action_type: actionType,
        notes: actionNotes || null,
      })
      setActionNotes('')
      setShowNoteInput(false)
      await fetchData()
    } catch (err) {
      console.error('Error creating action:', err)
      alert('Errore nella creazione dell\'azione')
    } finally {
      setActionLoading(false)
    }
  }

  const handleCompleteAction = async (actionId, outcome) => {
    try {
      await client.put(`/recovery/customers/${customerId}/actions/${actionId}/complete`, null, {
        params: { outcome: outcome || undefined },
      })
      setCompletingAction(null)
      setSelectedOutcome('')
      await fetchData()
    } catch (err) {
      console.error('Error completing action:', err)
      alert('Errore nel completamento dell\'azione')
    }
  }

  // Download selected invoices as ZIP of individual PDFs
  const handleDownloadInvoicesZip = async () => {
    if (selectedInvoices.size === 0) return
    setPdfLoading(true)
    try {
      const ids = Array.from(selectedInvoices).join(',')
      const response = await client.get(`/recovery/customers/${customerId}/invoices-zip`, {
        responseType: 'blob',
        params: { invoice_ids: ids },
      })
      const url = window.URL.createObjectURL(new Blob([response.data]))
      const link = document.createElement('a')
      link.href = url
      link.setAttribute('download', `fatture_${data?.ragione_sociale?.replace(/\s/g, '_')}.zip`)
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      console.error('Error downloading invoices ZIP:', err)
      alert('Errore nello scaricamento delle fatture')
    } finally {
      setPdfLoading(false)
    }
  }

  // Download promemoria (riassunto fatture con IBAN e giorni ritardo)
  const handleDownloadPromemoria = async () => {
    if (selectedInvoices.size === 0) return
    setPromemoria(true)
    try {
      const ids = Array.from(selectedInvoices).join(',')
      const response = await client.get(`/recovery/customers/${customerId}/pdf-selected`, {
        responseType: 'blob',
        params: { invoice_ids: ids },
      })
      const url = window.URL.createObjectURL(new Blob([response.data]))
      const link = document.createElement('a')
      link.href = url
      link.setAttribute('download', `promemoria_${data?.ragione_sociale?.replace(/\s/g, '_')}.pdf`)
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      console.error('Error downloading promemoria:', err)
      alert('Errore nella generazione del promemoria')
    } finally {
      setPromemoria(false)
    }
  }

  // Download single invoice PDF
  const handleDownloadSinglePdf = async (invoiceId, invoiceNumber) => {
    setSinglePdfLoading(invoiceId)
    try {
      const response = await client.get(`/recovery/invoices/${invoiceId}/pdf`, {
        responseType: 'blob',
      })
      const url = window.URL.createObjectURL(new Blob([response.data]))
      const link = document.createElement('a')
      link.href = url
      link.setAttribute('download', `fattura_${invoiceNumber?.replace(/\//g, '_')}.pdf`)
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      console.error('Error downloading single PDF:', err)
      alert('Errore nella generazione del PDF')
    } finally {
      setSinglePdfLoading(null)
    }
  }

  const handlePhoneUpdate = async () => {
    if (!phoneEdit && phoneEdit !== '') return
    try {
      await client.put(`/customers/${customerId}/phone`, null, {
        params: { phone: phoneEdit },
      })
      await fetchData()
      setPhoneEdit(null)
    } catch (err) {
      console.error('Error updating phone:', err)
    }
  }

  const handleMarkPaid = async (invoiceId) => {
    setUpdatingInvoice(invoiceId)
    try {
      await client.put(`/positions/${invoiceId}/status`, null, { params: { new_status: 'paid' } })
      await fetchData()
    } catch (err) {
      console.error('Error marking as paid:', err)
    } finally {
      setUpdatingInvoice(null)
    }
  }

  const handleReopenInvoice = async (invoiceId) => {
    setUpdatingInvoice(invoiceId)
    try {
      await client.put(`/positions/${invoiceId}/status`, null, { params: { new_status: 'open' } })
      await fetchData()
    } catch (err) {
      console.error('Error reopening:', err)
    } finally {
      setUpdatingInvoice(null)
    }
  }

  const toggleInvoiceSelection = (id) => {
    setSelectedInvoices(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAllOverdue = () => {
    if (!data?.invoices?.items) return
    const overdueIds = data.invoices.items
      .filter(inv => inv.days_overdue > 0 && inv.status !== 'paid')
      .map(inv => inv.id)
    setSelectedInvoices(new Set(overdueIds))
  }

  // Build WhatsApp message with selected invoices
  const buildWhatsAppMessage = () => {
    if (!data || selectedInvoices.size === 0) return ''
    const selected = (data.invoices?.items || []).filter(inv => selectedInvoices.has(inv.id))
    const totalSelected = selected.reduce((sum, inv) => sum + inv.amount_due, 0)

    let msg = `Gentile ${data.ragione_sociale},\n\n`
    msg += `le scriviamo per ricordarle che risultano in sospeso le seguenti fatture:\n\n`

    selected.forEach(inv => {
      const dueDate = inv.due_date ? new Date(inv.due_date + 'T00:00:00').toLocaleDateString('it-IT') : 'N/D'
      const orderRef = inv.shopify_order_number ? ` [Ordine ${inv.shopify_order_number}]` : ''
      msg += `- Fatt. ${inv.invoice_number}${orderRef}: ${new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(inv.amount_due)} (scad. ${dueDate})\n`
    })

    msg += `\nTotale: ${new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(totalSelected)}\n\n`
    msg += `Coordinate bancarie:\nIBAN: IT44N0200801671000105175151\nIntestatario: Sake Company srl\nCausale: Saldo fatture ${data.ragione_sociale}\n\n`
    msg += `La preghiamo di provvedere al saldo o contattarci per chiarimenti.\n\nGrazie,\nSake Company`

    return msg
  }

  const getWhatsAppNumber = () => {
    const raw = selectedWhatsAppPhone || data?.phone || ''
    return raw.replace(/[^+\d]/g, '')
  }

  const handleWhatsAppSend = () => {
    const number = getWhatsAppNumber()
    if (!number) return
    const message = buildWhatsAppMessage()
    const url = `https://wa.me/${number}?text=${encodeURIComponent(message)}`
    window.open(url, '_blank')
  }

  const handleCopyWhatsApp = async () => {
    const message = buildWhatsAppMessage()
    if (!message) return
    try {
      await navigator.clipboard.writeText(message)
      setCopiedWhatsApp(true)
      setTimeout(() => setCopiedWhatsApp(false), 2000)
    } catch {
      // Fallback for older browsers
      const textarea = document.createElement('textarea')
      textarea.value = message
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      document.body.removeChild(textarea)
      setCopiedWhatsApp(true)
      setTimeout(() => setCopiedWhatsApp(false), 2000)
    }
  }

  // Progressive action numbering
  const ACTION_NUMBER_LABELS = ['PRIMA', 'SECONDA', 'TERZA', 'QUARTA', 'QUINTA', 'SESTA', 'SETTIMA', 'OTTAVA', 'NONA', 'DECIMA']
  const contactActionCount = data?.contact_action_count || 0
  const nextActionNumber = contactActionCount + 1
  const nextActionLabel = ACTION_NUMBER_LABELS[contactActionCount] || `${nextActionNumber}ª`
  const shouldSuggestLawyer = contactActionCount >= 3

  // Invoice sorting
  const handleInvoiceSort = (field) => {
    if (invoiceSortBy === field) {
      setInvoiceSortOrder(invoiceSortOrder === 'asc' ? 'desc' : 'asc')
    } else {
      setInvoiceSortBy(field)
      setInvoiceSortOrder(field === 'due_date' ? 'asc' : 'desc')
    }
  }

  const invoiceSortArrow = (field) => {
    if (invoiceSortBy !== field) return ''
    return invoiceSortOrder === 'asc' ? ' ↑' : ' ↓'
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <svg className="animate-spin w-8 h-8 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      </div>
    )
  }

  if (error) return <div className="p-6 text-red-600">{error}</div>
  if (!data) return null

  const overdueInvoices = data.invoices?.items?.filter(inv => inv.days_overdue > 0 && inv.status !== 'paid') || []
  const totalOverdue = overdueInvoices.reduce((sum, inv) => sum + inv.amount_due, 0)
  const allUnpaid = data.invoices?.items?.filter(inv => inv.status !== 'paid') || []
  const whatsappNumber = getWhatsAppNumber() || null

  // By default show only overdue, unless user toggles
  let visibleInvoices = showAllInvoices
    ? (data.invoices?.items || [])
    : overdueInvoices

  // Sort visible invoices
  visibleInvoices = [...visibleInvoices].sort((a, b) => {
    let valA, valB
    if (invoiceSortBy === 'due_date') {
      valA = a.due_date || '9999-12-31'
      valB = b.due_date || '9999-12-31'
    } else if (invoiceSortBy === 'amount_due') {
      valA = a.amount_due
      valB = b.amount_due
    } else if (invoiceSortBy === 'days_overdue') {
      valA = a.days_overdue || 0
      valB = b.days_overdue || 0
    } else {
      valA = a.invoice_number
      valB = b.invoice_number
    }
    if (valA < valB) return invoiceSortOrder === 'asc' ? -1 : 1
    if (valA > valB) return invoiceSortOrder === 'asc' ? 1 : -1
    return 0
  })

  const selectedTotal = (data.invoices?.items || [])
    .filter(inv => selectedInvoices.has(inv.id))
    .reduce((sum, inv) => sum + inv.amount_due, 0)

  return (
    <div className="space-y-6">
      {/* Navigation bar */}
      <div className="flex items-center justify-between">
        <button
          onClick={() => navigate('/customers')}
          className="flex items-center gap-2 text-sm text-slate-600 hover:text-blue-600"
        >
          &larr; Torna ai Clienti
        </button>
        <div className="flex items-center gap-3">
          {neighbors.position && (
            <span className="text-xs text-slate-400">{neighbors.position} di {neighbors.total}</span>
          )}
          <button
            onClick={() => neighbors.prev_id && navigate(`/customers/${neighbors.prev_id}`)}
            disabled={!neighbors.prev_id}
            className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              neighbors.prev_id ? 'bg-slate-100 text-slate-700 hover:bg-slate-200' : 'bg-slate-50 text-slate-300 cursor-not-allowed'
            }`}
          >
            &larr; Precedente
          </button>
          <button
            onClick={() => neighbors.next_id && navigate(`/customers/${neighbors.next_id}`)}
            disabled={!neighbors.next_id}
            className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              neighbors.next_id ? 'bg-slate-100 text-slate-700 hover:bg-slate-200' : 'bg-slate-50 text-slate-300 cursor-not-allowed'
            }`}
          >
            Successivo &rarr;
          </button>
        </div>
      </div>

      {/* Customer Header */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-bold text-slate-900">{data.ragione_sociale}</h1>
            <div className="mt-2 space-y-1 text-sm text-slate-600">
              {data.partita_iva && <p>P.IVA: <span className="font-mono">{data.partita_iva}</span></p>}
              {data.codice_fiscale && <p>C.F.: <span className="font-mono">{data.codice_fiscale}</span></p>}
              {data.email && <p>Email: {data.email}</p>}
              {/* Phone numbers with source labels */}
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <span>Telefono:</span>
                  <button
                    onClick={() => setPhoneEdit(data.phone || '')}
                    className="text-blue-600 text-xs underline"
                  >
                    {data.phone ? 'Modifica' : 'Aggiungi'}
                  </button>
                </div>
                {phoneEdit !== null ? (
                  <div className="flex items-center gap-2 ml-2">
                    <input
                      type="text"
                      value={phoneEdit}
                      onChange={(e) => setPhoneEdit(e.target.value)}
                      className="px-2 py-1 border border-slate-300 rounded text-sm w-48"
                      placeholder="+39..."
                    />
                    <button onClick={handlePhoneUpdate} className="text-green-600 text-sm font-medium">Salva</button>
                    <button onClick={() => setPhoneEdit(null)} className="text-slate-400 text-sm">Annulla</button>
                  </div>
                ) : (
                  <div className="ml-2 space-y-0.5">
                    {(data.phones && data.phones.length > 0) ? (
                      data.phones.map((p, idx) => (
                        <div key={idx} className="flex items-center gap-2">
                          <span className="font-mono text-slate-900">{p.number}</span>
                          <span className="text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">{p.label}</span>
                          {p.number.replace(/[^+\d]/g, '') !== (selectedWhatsAppPhone || data.phone || '').replace(/[^+\d]/g, '') && (
                            <button
                              onClick={() => setSelectedWhatsAppPhone(p.number)}
                              className="text-xs text-green-600 hover:underline"
                            >
                              Usa per WhatsApp
                            </button>
                          )}
                          {p.number.replace(/[^+\d]/g, '') === (selectedWhatsAppPhone || data.phone || '').replace(/[^+\d]/g, '') && (
                            <span className="text-xs text-green-600 font-medium">WhatsApp</span>
                          )}
                        </div>
                      ))
                    ) : (
                      <span className="font-mono text-slate-400">{data.phone || 'Non disponibile'}</span>
                    )}
                  </div>
                )}
              </div>
              {data.source && <p>Fonte: <span className="capitalize">{data.source}</span></p>}
            </div>
          </div>
          <div className="flex flex-col items-end gap-3">
            {/* Recovery status badge */}
            <span className={`${STATUS_COLORS[data.recovery_status] || STATUS_COLORS.idle} px-3 py-1 rounded-full text-sm font-medium`}>
              {STATUS_LABELS[data.recovery_status] || 'Da Gestire'}
            </span>
            {data.next_action_date && (
              <p className="text-sm text-slate-500">
                Prossima azione: <span className="font-medium">{formatDate(data.next_action_date)}</span>
              </p>
            )}
          </div>
        </div>

        {/* Summary stats */}
        <div className="mt-6 grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="bg-red-50 rounded-lg p-3 text-center">
            <p className="text-xs text-red-600">Fatture Scadute</p>
            <p className="text-xl font-bold text-red-700">{overdueInvoices.length}</p>
          </div>
          <div className="bg-amber-50 rounded-lg p-3 text-center">
            <p className="text-xs text-amber-600">Totale Scaduto</p>
            <p className="text-xl font-bold text-amber-700">{formatCurrency(totalOverdue)}</p>
          </div>
          <div className="bg-blue-50 rounded-lg p-3 text-center">
            <p className="text-xs text-blue-600">Totale Dovuto</p>
            <p className="text-xl font-bold text-blue-700">{formatCurrency(data.invoices?.total_due || 0)}</p>
          </div>
          <div className="bg-slate-50 rounded-lg p-3 text-center">
            <p className="text-xs text-slate-500">Fatture Totali</p>
            <p className="text-xl font-bold text-slate-900">{data.invoices?.count || 0}</p>
          </div>
        </div>
      </div>

      {/* ═══════════════════════════════════════════════════════ */}
      {/* SEZIONE 1: FATTURE — select invoices, per-row PDF, riepilogativo */}
      {/* ═══════════════════════════════════════════════════════ */}
      <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <h2 className="text-lg font-bold text-slate-900">
                {showAllInvoices ? `Tutte le Fatture (${data.invoices?.count || 0})` : `Fatture Scadute (${overdueInvoices.length})`}
              </h2>
              <button
                onClick={() => setShowAllInvoices(!showAllInvoices)}
                className="text-sm text-blue-600 hover:text-blue-800 font-medium"
              >
                {showAllInvoices ? 'Solo Scadute' : 'Mostra Tutte'}
              </button>
            </div>
            <button
              onClick={selectAllOverdue}
              className="text-sm text-blue-600 hover:text-blue-800 font-medium"
            >
              Seleziona Scadute
            </button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="px-3 py-3 text-center text-sm font-semibold text-slate-600 w-10">
                  <input
                    type="checkbox"
                    checked={selectedInvoices.size === allUnpaid.length && allUnpaid.length > 0}
                    onChange={() => {
                      if (selectedInvoices.size === allUnpaid.length) {
                        setSelectedInvoices(new Set())
                      } else {
                        setSelectedInvoices(new Set(allUnpaid.map(i => i.id)))
                      }
                    }}
                    className="rounded"
                  />
                </th>
                <th
                  className="px-3 py-3 text-left text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100"
                  onClick={() => handleInvoiceSort('invoice_number')}
                >
                  Fattura{invoiceSortArrow('invoice_number')}
                </th>
                <th className="px-3 py-3 text-left text-sm font-semibold text-slate-900">Ordine</th>
                <th className="px-3 py-3 text-left text-sm font-semibold text-slate-900">Fonte</th>
                <th
                  className="px-3 py-3 text-right text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100"
                  onClick={() => handleInvoiceSort('amount_due')}
                >
                  Dovuto{invoiceSortArrow('amount_due')}
                </th>
                <th
                  className="px-3 py-3 text-left text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100"
                  onClick={() => handleInvoiceSort('due_date')}
                >
                  Scadenza{invoiceSortArrow('due_date')}
                </th>
                <th
                  className="px-3 py-3 text-right text-sm font-semibold text-slate-900 cursor-pointer hover:bg-slate-100"
                  onClick={() => handleInvoiceSort('days_overdue')}
                >
                  GG{invoiceSortArrow('days_overdue')}
                </th>
                <th className="px-3 py-3 text-center text-sm font-semibold text-slate-900">Stato</th>
                <th className="px-3 py-3 text-center text-sm font-semibold text-slate-900">Azioni</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200">
              {visibleInvoices.map(inv => (
                <tr
                  key={inv.id}
                  className={`
                    ${inv.status === 'paid' ? 'bg-green-50 opacity-60' : ''}
                    ${inv.days_overdue > 0 && inv.status !== 'paid' ? 'bg-red-50' : ''}
                    ${selectedInvoices.has(inv.id) ? 'ring-2 ring-inset ring-blue-300' : ''}
                    hover:bg-slate-50
                  `}
                >
                  <td className="px-3 py-3 text-center">
                    <input
                      type="checkbox"
                      checked={selectedInvoices.has(inv.id)}
                      onChange={() => toggleInvoiceSelection(inv.id)}
                      className="rounded"
                      disabled={inv.status === 'paid'}
                    />
                  </td>
                  <td className="px-3 py-3 text-sm font-medium text-slate-900">{inv.invoice_number}</td>
                  <td className="px-3 py-3 text-sm">
                    {inv.shopify_order_number ? (
                      <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">
                        {inv.shopify_order_number}
                      </span>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-sm">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                      inv.source_platform === 'fatturapro' ? 'bg-indigo-100 text-indigo-700' : 'bg-teal-100 text-teal-700'
                    }`}>
                      {inv.source_platform === 'fatturapro' ? 'FPro' : 'F24'}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-sm text-right font-medium">{formatCurrency(inv.amount_due)}</td>
                  <td className="px-3 py-3 text-sm text-slate-600">{formatDate(inv.due_date)}</td>
                  <td className="px-3 py-3 text-sm text-right">
                    {(inv.days_overdue || 0) > 0 ? (
                      <span className={inv.days_overdue > 30 ? 'text-red-600 font-medium' : 'text-amber-600'}>
                        {inv.days_overdue}
                      </span>
                    ) : (inv.days_overdue || 0) < 0 ? (
                      <span className="text-green-600 font-medium">
                        {inv.days_overdue}
                      </span>
                    ) : (
                      <span className="text-slate-400">0</span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-sm text-center">
                    <span className={`${INVOICE_STATUS_COLORS[inv.status] || 'bg-slate-100 text-slate-600'} px-2 py-1 rounded-full text-xs font-medium`}>
                      {inv.status === 'open' ? 'Aperto' : inv.status === 'paid' ? 'Pagato' : inv.status}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-sm text-center">
                    <div className="flex items-center justify-center gap-1">
                      {/* Per-row PDF download */}
                      <button
                        onClick={() => handleDownloadSinglePdf(inv.id, inv.invoice_number)}
                        disabled={singlePdfLoading === inv.id}
                        className="px-2 py-1 bg-amber-100 text-amber-700 rounded text-xs font-medium hover:bg-amber-200 disabled:opacity-50"
                        title="Scarica PDF"
                      >
                        {singlePdfLoading === inv.id ? '...' : 'PDF'}
                      </button>
                      {/* Pagato/Riapri rimossi: lo stato pagamento arriva solo dal refresh sync */}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Action bar for selected invoices */}
        {selectedInvoices.size > 0 && (
          <div className="px-6 py-4 bg-green-50 border-t border-green-200">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-green-900">
                  {selectedInvoices.size} fattur{selectedInvoices.size === 1 ? 'a' : 'e'} selezionat{selectedInvoices.size === 1 ? 'a' : 'e'} — {formatCurrency(selectedTotal)}
                </p>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <button
                  onClick={handleDownloadInvoicesZip}
                  disabled={pdfLoading}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-bold hover:bg-blue-700 disabled:opacity-50"
                >
                  {pdfLoading ? '...' : `Scarica ${selectedInvoices.size} Fattur${selectedInvoices.size === 1 ? 'a' : 'e'}`}
                </button>
                <button
                  onClick={handleDownloadPromemoria}
                  disabled={promemoria}
                  className="px-4 py-2 bg-amber-600 text-white rounded-lg text-sm font-bold hover:bg-amber-700 disabled:opacity-50"
                >
                  {promemoria ? '...' : 'Scarica Promemoria'}
                </button>
                <button
                  onClick={handleCopyWhatsApp}
                  className={`px-4 py-2 rounded-lg text-sm font-bold transition-colors ${
                    copiedWhatsApp
                      ? 'bg-green-100 text-green-700 border border-green-400'
                      : 'bg-slate-100 text-slate-700 hover:bg-slate-200 border border-slate-300'
                  }`}
                >
                  {copiedWhatsApp ? 'Copiato!' : 'Copia Messaggio'}
                </button>
                {whatsappNumber ? (
                  <button
                    onClick={handleWhatsAppSend}
                    className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm font-bold hover:bg-green-700"
                  >
                    WhatsApp
                  </button>
                ) : (
                  <button
                    onClick={() => setPhoneEdit(data.phone || '')}
                    className="px-4 py-2 bg-yellow-500 text-white rounded-lg text-sm font-bold hover:bg-yellow-600"
                  >
                    Aggiungi Tel
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* ═══════════════════════════════════════════════════════ */}
      {/* SEZIONE 2: AZIONI DI RECUPERO */}
      {/* ═══════════════════════════════════════════════════════ */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-bold text-slate-900">Azioni di Recupero</h2>
          {contactActionCount > 0 && (
            <span className="text-sm text-slate-500">
              Azioni registrate: <span className="font-bold text-slate-700">{contactActionCount}</span>
            </span>
          )}
        </div>

        {/* Lawyer suggestion banner */}
        {shouldSuggestLawyer && data.recovery_status !== 'lawyer' && data.recovery_status !== 'archived' && (
          <div className="mb-4 bg-red-50 border-2 border-red-300 rounded-lg p-4 flex items-center justify-between">
            <div>
              <p className="text-sm font-bold text-red-800">Suggerimento: passare all'Avvocato</p>
              <p className="text-xs text-red-600 mt-1">
                Sono state effettuate {contactActionCount} azioni di contatto senza esito. Si consiglia di procedere con l'avvocato.
              </p>
            </div>
            <button
              onClick={() => handleAction('lawyer')}
              disabled={actionLoading}
              className="px-5 py-2 bg-red-600 text-white rounded-lg text-sm font-bold hover:bg-red-700 disabled:opacity-50 shrink-0"
            >
              Passa ad Avvocato
            </button>
          </div>
        )}

        {/* REGISTRA AZIONE — main action button with progressive numbering */}
        <div className="mb-4">
          <div className="flex flex-wrap gap-3 items-center">
            {data.recovery_status !== 'lawyer' && data.recovery_status !== 'archived' && (
              <button
                onClick={() => {
                  // Determine the correct action type based on count
                  const actionType = contactActionCount === 0 ? 'first_contact' : 'second_contact'
                  handleAction(actionType)
                }}
                disabled={actionLoading}
                className="px-6 py-3 bg-blue-600 text-white rounded-lg text-sm font-bold hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2 shadow-sm"
              >
                {actionLoading ? '...' : `REGISTRA ${nextActionLabel} AZIONE`}
              </button>
            )}
            <button
              onClick={() => handleAction('lawyer')}
              disabled={actionLoading}
              className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm font-medium hover:bg-red-700 disabled:opacity-50"
            >
              {actionLoading ? '...' : 'Avvocato'}
            </button>
            <button
              onClick={() => handleAction('wait')}
              disabled={actionLoading}
              className="px-4 py-2 bg-purple-600 text-white rounded-lg text-sm font-medium hover:bg-purple-700 disabled:opacity-50"
            >
              {actionLoading ? '...' : 'Attendi'}
            </button>
            <button
              onClick={() => handleAction('archive')}
              disabled={actionLoading}
              className="px-4 py-2 bg-slate-500 text-white rounded-lg text-sm font-medium hover:bg-slate-600 disabled:opacity-50"
            >
              {actionLoading ? '...' : 'Archivia'}
            </button>
            <button
              onClick={() => setShowNoteInput(!showNoteInput)}
              className="px-4 py-2 bg-slate-100 text-slate-700 rounded-lg text-sm font-medium hover:bg-slate-200"
            >
              + Nota
            </button>
          </div>
        </div>

        {/* Note input */}
        {showNoteInput && (
          <div className="mb-4 flex gap-2">
            <input
              type="text"
              value={actionNotes}
              onChange={(e) => setActionNotes(e.target.value)}
              placeholder="Note sull'azione..."
              className="flex-1 px-3 py-2 border border-slate-300 rounded-lg text-sm"
            />
            <button
              onClick={() => handleAction('note')}
              disabled={!actionNotes || actionLoading}
              className="px-4 py-2 bg-slate-700 text-white rounded-lg text-sm font-medium disabled:opacity-50"
            >
              Salva Nota
            </button>
          </div>
        )}

        {/* Action history timeline */}
        {data.recovery_actions && data.recovery_actions.length > 0 && (
          <div className="mt-4 border-l-2 border-slate-200 pl-4 space-y-3">
            {data.recovery_actions.map(action => (
              <div key={action.id} className="relative">
                <div className={`absolute -left-[21px] top-1 w-3 h-3 rounded-full border-2 border-white ${
                  action.completed_at ? 'bg-green-500' : 'bg-slate-400'
                }`}></div>
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium text-slate-900">
                    {ACTION_LABELS[action.action_type] || action.action_type}
                  </span>
                  <span className="text-xs text-slate-400">{formatDate(action.created_at)}</span>
                  {action.completed_at && action.outcome && (
                    <span className={`text-xs px-1.5 py-0.5 rounded ${OUTCOME_COLORS[action.outcome] || 'bg-green-100 text-green-700'}`}>
                      {OUTCOME_LABELS[action.outcome] || action.outcome}
                    </span>
                  )}
                  {action.completed_at && !action.outcome && (
                    <span className="text-xs bg-green-100 text-green-600 px-1.5 py-0.5 rounded">completata</span>
                  )}
                  {/* Complete button for pending actions */}
                  {!action.completed_at && (
                    <>
                      {completingAction === action.id ? (
                        <div className="flex items-center gap-1 flex-wrap">
                          {Object.entries(OUTCOME_LABELS).map(([key, label]) => (
                            <button
                              key={key}
                              onClick={() => handleCompleteAction(action.id, key)}
                              className={`text-xs px-2 py-0.5 rounded border transition-colors ${
                                OUTCOME_COLORS[key] || 'bg-slate-100 text-slate-600'
                              } hover:opacity-80`}
                            >
                              {label}
                            </button>
                          ))}
                          <button
                            onClick={() => setCompletingAction(null)}
                            className="text-xs text-slate-400 ml-1"
                          >
                            Annulla
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setCompletingAction(action.id)}
                          className="text-xs bg-green-50 text-green-700 px-2 py-0.5 rounded border border-green-200 hover:bg-green-100"
                        >
                          Completa
                        </button>
                      )}
                    </>
                  )}
                </div>
                {action.notes && (
                  <p className="text-sm text-slate-500 mt-0.5">{action.notes}</p>
                )}
                {action.scheduled_date && !action.completed_at && (
                  <p className="text-xs text-blue-500 mt-0.5">Pianificata: {formatDate(action.scheduled_date)}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ═══════════════════════════════════════════════════════ */}
      {/* SEZIONE 3: RIEPILOGO — summary status after actions */}
      {/* ═══════════════════════════════════════════════════════ */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <h2 className="text-lg font-bold text-slate-900 mb-4">Riepilogo</h2>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Status */}
          <div className="border border-slate-200 rounded-lg p-4">
            <p className="text-xs text-slate-500 mb-1">Stato Recupero</p>
            <span className={`${STATUS_COLORS[data.recovery_status] || STATUS_COLORS.idle} px-3 py-1 rounded-full text-sm font-medium`}>
              {STATUS_LABELS[data.recovery_status] || 'Da Gestire'}
            </span>
          </div>

          {/* Next action */}
          <div className="border border-slate-200 rounded-lg p-4">
            <p className="text-xs text-slate-500 mb-1">Prossima Azione</p>
            {data.next_action_date ? (
              <div>
                <p className="text-sm font-medium text-slate-900">
                  {ACTION_LABELS[data.next_action_type] || data.next_action_type || '-'}
                </p>
                <p className="text-xs text-blue-600">{formatDate(data.next_action_date)}</p>
              </div>
            ) : (
              <p className="text-sm text-slate-400">Nessuna pianificata</p>
            )}
          </div>

          {/* Actions taken */}
          <div className="border border-slate-200 rounded-lg p-4">
            <p className="text-xs text-slate-500 mb-1">Azioni Effettuate</p>
            <p className="text-xl font-bold text-slate-900">{data.recovery_actions?.length || 0}</p>
          </div>
        </div>

        {/* Financial summary */}
        <div className="mt-4 grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="border border-red-200 rounded-lg p-4 bg-red-50">
            <p className="text-xs text-red-600 mb-1">Scaduto</p>
            <p className="text-lg font-bold text-red-700">{formatCurrency(totalOverdue)}</p>
            <p className="text-xs text-red-500">{overdueInvoices.length} fatture</p>
          </div>
          <div className="border border-blue-200 rounded-lg p-4 bg-blue-50">
            <p className="text-xs text-blue-600 mb-1">Totale Dovuto</p>
            <p className="text-lg font-bold text-blue-700">{formatCurrency(data.invoices?.total_due || 0)}</p>
            <p className="text-xs text-blue-500">{allUnpaid.length} fatture non pagate</p>
          </div>
          <div className="border border-green-200 rounded-lg p-4 bg-green-50">
            <p className="text-xs text-green-600 mb-1">Pagato</p>
            <p className="text-lg font-bold text-green-700">
              {formatCurrency(
                (data.invoices?.items || [])
                  .filter(inv => inv.status === 'paid')
                  .reduce((sum, inv) => sum + inv.amount_due, 0)
              )}
            </p>
            <p className="text-xs text-green-500">
              {(data.invoices?.items || []).filter(inv => inv.status === 'paid').length} fatture pagate
            </p>
          </div>
        </div>

        {/* Quick actions from riepilogo */}
        <div className="mt-4 pt-4 border-t border-slate-200 flex flex-wrap gap-2">
          {selectedInvoices.size > 0 && (
            <>
              <button
                onClick={handleDownloadInvoicesZip}
                disabled={pdfLoading}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
              >
                {pdfLoading ? '...' : `Scarica ${selectedInvoices.size} Fattur${selectedInvoices.size === 1 ? 'a' : 'e'}`}
              </button>
              <button
                onClick={handleDownloadPromemoria}
                disabled={promemoria}
                className="px-4 py-2 bg-amber-600 text-white rounded-lg text-sm font-medium hover:bg-amber-700 disabled:opacity-50"
              >
                {promemoria ? '...' : 'Promemoria'}
              </button>
              <button
                onClick={handleCopyWhatsApp}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                  copiedWhatsApp ? 'bg-green-100 text-green-700' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
                }`}
              >
                {copiedWhatsApp ? 'Copiato!' : 'Copia Messaggio'}
              </button>
              {whatsappNumber && (
                <button
                  onClick={handleWhatsAppSend}
                  className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm font-medium hover:bg-green-700"
                >
                  WhatsApp
                </button>
              )}
            </>
          )}
          <button
            onClick={() => navigate('/customers')}
            className="px-4 py-2 bg-slate-100 text-slate-700 rounded-lg text-sm font-medium hover:bg-slate-200"
          >
            Torna alla Lista
          </button>
        </div>
      </div>
    </div>
  )
}
