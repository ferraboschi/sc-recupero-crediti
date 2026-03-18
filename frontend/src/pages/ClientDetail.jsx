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
  first_contact: 'bg-accent-blue hover:brightness-110',
  second_contact: 'bg-accent-amber hover:brightness-110',
  lawyer: 'bg-accent-red hover:brightness-110',
  archive: 'bg-slate-500 hover:brightness-110',
  wait: 'bg-accent-purple hover:brightness-110',
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
  idle: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
  first_contact: 'badge-open',
  second_contact: 'badge-contacted',
  lawyer: 'badge-disputed',
  archived: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
  waiting: 'badge-promised',
}

const INVOICE_STATUS_COLORS = {
  open: 'badge-open',
  contacted: 'badge-contacted',
  promised: 'badge-promised',
  paid: 'badge-paid',
  disputed: 'badge-disputed',
  escalated: 'badge-escalated',
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
  contacted: 'bg-accent-blue/15 text-accent-blue',
  promised: 'bg-accent-amber/15 text-accent-amber',
  partial_payment: 'bg-accent-teal/15 text-accent-teal',
  paid: 'bg-accent-green/15 text-accent-green',
  unreachable: 'bg-[rgba(148,163,184,0.15)] text-txt-muted',
  disputed: 'bg-accent-red/15 text-accent-red',
  no_answer: 'bg-accent-amber/15 text-accent-amber',
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
  const [neighbors, setNeighbors] = useState({ prev_id: null, next_id: null, position: null, total: null })
  const [completingAction, setCompletingAction] = useState(null)
  const [selectedOutcome, setSelectedOutcome] = useState('')
  const [copiedWhatsApp, setCopiedWhatsApp] = useState(false)
  const [promemoria, setPromemoria] = useState(false)
  const [selectedWhatsAppPhone, setSelectedWhatsAppPhone] = useState(null)
  const [showDatePicker, setShowDatePicker] = useState(false)
  const [pendingActionType, setPendingActionType] = useState(null)
  const [scheduledDate, setScheduledDate] = useState('')

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      const response = await client.get(`/customers/${customerId}`)
      setData(response.data)
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
    const d = dateStr.length === 10 ? new Date(dateStr + 'T00:00:00') : new Date(dateStr)
    return d.toLocaleDateString('it-IT')
  }

  const handleAction = async (actionType) => {
    // For contact/lawyer actions, show date picker first
    if (['first_contact', 'second_contact', 'lawyer'].includes(actionType) && !showDatePicker) {
      setPendingActionType(actionType)
      // Default date based on action type
      const defaults = { first_contact: 7, second_contact: 14, lawyer: 30 }
      const d = new Date()
      d.setDate(d.getDate() + (defaults[actionType] || 7))
      setScheduledDate(d.toISOString().split('T')[0])
      setShowDatePicker(true)
      return
    }

    setActionLoading(true)
    try {
      await client.post(`/recovery/customers/${customerId}/actions`, {
        action_type: actionType,
        scheduled_date: scheduledDate || null,
        notes: actionNotes || null,
      })
      setActionNotes('')
      setShowNoteInput(false)
      setShowDatePicker(false)
      setPendingActionType(null)
      setScheduledDate('')
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

  const ACTION_NUMBER_LABELS = ['PRIMA', 'SECONDA', 'TERZA', 'QUARTA', 'QUINTA', 'SESTA', 'SETTIMA', 'OTTAVA', 'NONA', 'DECIMA']
  const contactActionCount = data?.contact_action_count || 0
  const nextActionNumber = contactActionCount + 1
  const nextActionLabel = ACTION_NUMBER_LABELS[contactActionCount] || `${nextActionNumber}ª`
  const shouldSuggestLawyer = contactActionCount >= 3

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
        <svg className="animate-spin w-8 h-8 text-accent-teal" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      </div>
    )
  }

  if (error) return <div className="p-6 text-accent-red">{error}</div>
  if (!data) return null

  const overdueInvoices = data.invoices?.items?.filter(inv => inv.days_overdue > 0 && inv.status !== 'paid') || []
  const totalOverdue = overdueInvoices.reduce((sum, inv) => sum + inv.amount_due, 0)
  const allUnpaid = data.invoices?.items?.filter(inv => inv.status !== 'paid') || []
  const paidInvoices = data.invoices?.items?.filter(inv => inv.status === 'paid') || []
  const totalPaid = paidInvoices.reduce((sum, inv) => sum + inv.amount, 0)
  const whatsappNumber = getWhatsAppNumber() || null

  let visibleInvoices = showAllInvoices
    ? (data.invoices?.items || [])
    : overdueInvoices

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
          className="flex items-center gap-2 text-sm text-txt-secondary hover:text-accent-teal transition-colors"
        >
          &larr; Torna ai Clienti
        </button>
        <div className="flex items-center gap-3">
          {neighbors.position && (
            <span className="text-xs text-txt-muted">{neighbors.position} di {neighbors.total}</span>
          )}
          <button
            onClick={() => neighbors.prev_id && navigate(`/customers/${neighbors.prev_id}`)}
            disabled={!neighbors.prev_id}
            className={`sc-btn-secondary ${!neighbors.prev_id ? 'opacity-30 cursor-not-allowed' : ''}`}
          >
            &larr; Precedente
          </button>
          <button
            onClick={() => neighbors.next_id && navigate(`/customers/${neighbors.next_id}`)}
            disabled={!neighbors.next_id}
            className={`sc-btn-secondary ${!neighbors.next_id ? 'opacity-30 cursor-not-allowed' : ''}`}
          >
            Successivo &rarr;
          </button>
        </div>
      </div>

      {/* Customer Header */}
      <div className="sc-card p-6">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-bold text-txt-primary">{data.ragione_sociale}</h1>
            <div className="mt-2 space-y-1 text-sm text-txt-secondary">
              {data.partita_iva && <p>P.IVA: <span className="font-mono text-txt-primary">{data.partita_iva}</span></p>}
              {data.codice_fiscale && <p>C.F.: <span className="font-mono text-txt-primary">{data.codice_fiscale}</span></p>}
              {data.email && <p>Email: <span className="text-txt-primary">{data.email}</span></p>}
              {/* Phone numbers */}
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <span>Telefono:</span>
                  <button
                    onClick={() => setPhoneEdit(data.phone || '')}
                    className="text-accent-teal text-xs underline"
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
                      className="sc-input w-48 text-sm"
                      placeholder="+39..."
                    />
                    <button onClick={handlePhoneUpdate} className="text-accent-green text-sm font-medium">Salva</button>
                    <button onClick={() => setPhoneEdit(null)} className="text-txt-muted text-sm">Annulla</button>
                  </div>
                ) : (
                  <div className="ml-2 space-y-0.5">
                    {(data.phones && data.phones.length > 0) ? (
                      data.phones.map((p, idx) => (
                        <div key={idx} className="flex items-center gap-2">
                          <span className="font-mono text-txt-primary">{p.number}</span>
                          <span className="text-xs px-1.5 py-0.5 rounded bg-dark-surface text-txt-muted">{p.label}</span>
                          {p.number.replace(/[^+\d]/g, '') !== (selectedWhatsAppPhone || data.phone || '').replace(/[^+\d]/g, '') && (
                            <button
                              onClick={() => setSelectedWhatsAppPhone(p.number)}
                              className="text-xs text-accent-green hover:underline"
                            >
                              Usa per WhatsApp
                            </button>
                          )}
                          {p.number.replace(/[^+\d]/g, '') === (selectedWhatsAppPhone || data.phone || '').replace(/[^+\d]/g, '') && (
                            <span className="text-xs text-accent-green font-medium">WhatsApp</span>
                          )}
                        </div>
                      ))
                    ) : (
                      <span className="font-mono text-txt-muted">{data.phone || 'Non disponibile'}</span>
                    )}
                  </div>
                )}
              </div>
              {data.source && <p>Fonte: <span className="capitalize text-txt-primary">{data.source}</span></p>}
            </div>
          </div>
          <div className="flex flex-col items-end gap-3">
            <span className={`${STATUS_COLORS[data.recovery_status] || STATUS_COLORS.idle} sc-badge text-sm`}>
              {STATUS_LABELS[data.recovery_status] || 'Da Gestire'}
            </span>
            {data.next_action_date && (
              <p className="text-sm text-txt-muted">
                Prossima azione: <span className="font-medium text-txt-secondary">{formatDate(data.next_action_date)}</span>
              </p>
            )}
          </div>
        </div>

        {/* Summary stats */}
        <div className="mt-6 grid grid-cols-2 md:grid-cols-5 gap-4">
          <div className="bg-accent-red/5 border border-accent-red/20 rounded-lg p-3 text-center">
            <p className="text-xs text-accent-red">Fatture Scadute</p>
            <p className="text-xl font-bold text-accent-red">{overdueInvoices.length}</p>
          </div>
          <div className="bg-accent-amber/5 border border-accent-amber/20 rounded-lg p-3 text-center">
            <p className="text-xs text-accent-amber">Totale Scaduto</p>
            <p className="text-xl font-bold text-accent-amber">{formatCurrency(totalOverdue)}</p>
          </div>
          <div className="bg-accent-blue/5 border border-accent-blue/20 rounded-lg p-3 text-center">
            <p className="text-xs text-accent-blue">Totale Dovuto</p>
            <p className="text-xl font-bold text-accent-blue">{formatCurrency(data.invoices?.total_due || 0)}</p>
          </div>
          <div className="bg-accent-green/5 border border-accent-green/20 rounded-lg p-3 text-center">
            <p className="text-xs text-accent-green">Pagato</p>
            <p className="text-xl font-bold text-accent-green">{formatCurrency(totalPaid)}</p>
            <p className="text-xs text-accent-green/60 mt-0.5">{paidInvoices.length} fattur{paidInvoices.length === 1 ? 'a' : 'e'}</p>
          </div>
          <div className="bg-dark-surface border border-dark-border rounded-lg p-3 text-center">
            <p className="text-xs text-txt-muted">Fatture Totali</p>
            <p className="text-xl font-bold text-txt-primary">{data.invoices?.count || 0}</p>
          </div>
        </div>
      </div>

      {/* SEZIONE 1: FATTURE */}
      <div className="sc-card overflow-hidden">
        <div className="sc-card-header">
          <div className="flex items-center gap-4">
            <h2 className="text-base font-bold text-txt-primary">
              {showAllInvoices ? `Tutte le Fatture (${data.invoices?.count || 0})` : `Fatture Scadute (${overdueInvoices.length})`}
            </h2>
            <button
              onClick={() => setShowAllInvoices(!showAllInvoices)}
              className="text-sm text-accent-teal hover:text-accent-cyan font-medium transition-colors"
            >
              {showAllInvoices ? 'Solo Scadute' : 'Mostra Tutte'}
            </button>
          </div>
          <button
            onClick={selectAllOverdue}
            className="text-sm text-accent-teal hover:text-accent-cyan font-medium transition-colors"
          >
            Seleziona Scadute
          </button>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="bg-dark-surface border-b border-dark-border">
              <tr>
                <th className="px-3 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider w-10">
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
                    className="rounded border-dark-border bg-dark-bg"
                  />
                </th>
                <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary" onClick={() => handleInvoiceSort('invoice_number')}>
                  Fattura{invoiceSortArrow('invoice_number')}
                </th>
                <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Ordine</th>
                <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Fonte</th>
                <th className="px-3 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary" onClick={() => handleInvoiceSort('amount_due')}>
                  Dovuto{invoiceSortArrow('amount_due')}
                </th>
                <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary" onClick={() => handleInvoiceSort('due_date')}>
                  Scadenza{invoiceSortArrow('due_date')}
                </th>
                <th className="px-3 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider cursor-pointer hover:text-txt-primary" onClick={() => handleInvoiceSort('days_overdue')}>
                  GG{invoiceSortArrow('days_overdue')}
                </th>
                <th className="px-3 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Stato</th>
                <th className="px-3 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Azioni</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-dark-border">
              {visibleInvoices.map(inv => (
                <tr
                  key={inv.id}
                  className={`
                    ${inv.status === 'paid' ? 'bg-accent-green/5 opacity-60' : ''}
                    ${inv.days_overdue > 0 && inv.status !== 'paid' ? 'bg-accent-red/5' : ''}
                    ${selectedInvoices.has(inv.id) ? 'ring-2 ring-inset ring-accent-teal/30' : ''}
                    hover:bg-dark-cardHover transition-colors
                  `}
                >
                  <td className="px-3 py-3 text-center">
                    <input
                      type="checkbox"
                      checked={selectedInvoices.has(inv.id)}
                      onChange={() => toggleInvoiceSelection(inv.id)}
                      className="rounded border-dark-border bg-dark-bg"
                      disabled={inv.status === 'paid'}
                    />
                  </td>
                  <td className="px-3 py-3 text-sm font-medium text-txt-primary">{inv.invoice_number}</td>
                  <td className="px-3 py-3 text-sm">
                    {inv.shopify_order_number ? (
                      <span className="badge-paid sc-badge">
                        {inv.shopify_order_number}
                      </span>
                    ) : (
                      <span className="text-txt-muted">—</span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-sm">
                    <span className={`sc-badge ${
                      inv.source_platform === 'fatturapro' ? 'bg-accent-purple/15 text-accent-purple' : 'bg-accent-teal/15 text-accent-teal'
                    }`}>
                      {inv.source_platform === 'fatturapro' ? 'FPro' : 'F24'}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-sm text-right font-medium text-txt-primary">{formatCurrency(inv.amount_due)}</td>
                  <td className="px-3 py-3 text-sm text-txt-secondary">{formatDate(inv.due_date)}</td>
                  <td className="px-3 py-3 text-sm text-right">
                    {inv.status === 'paid' ? (
                      <span className="text-accent-green font-medium">Pagato</span>
                    ) : (inv.days_overdue || 0) > 0 ? (
                      <span className={inv.days_overdue > 30 ? 'text-accent-red font-medium' : 'text-accent-amber'}>
                        +{inv.days_overdue}gg
                      </span>
                    ) : (inv.days_overdue || 0) < 0 ? (
                      <span className="text-accent-teal text-xs" title={`Scadenza: ${formatDate(inv.due_date)}`}>
                        Scade tra {Math.abs(inv.days_overdue)}gg
                      </span>
                    ) : (
                      <span className="text-accent-amber font-medium">Oggi</span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-sm text-center">
                    <span className={`${INVOICE_STATUS_COLORS[inv.status] || 'bg-[rgba(148,163,184,0.15)] text-txt-muted'} sc-badge`}>
                      {inv.status === 'open' ? 'Aperto' : inv.status === 'paid' ? 'Pagato' : inv.status}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-sm text-center">
                    <button
                      onClick={() => handleDownloadSinglePdf(inv.id, inv.invoice_number)}
                      disabled={singlePdfLoading === inv.id}
                      className="px-2 py-1 bg-accent-amber/15 text-accent-amber rounded text-xs font-medium hover:bg-accent-amber/25 disabled:opacity-50 transition-colors"
                      title="Scarica PDF"
                    >
                      {singlePdfLoading === inv.id ? '...' : 'PDF'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Action bar for selected invoices */}
        {selectedInvoices.size > 0 && (
          <div className="px-6 py-4 bg-accent-green/5 border-t border-accent-green/20">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-accent-green">
                  {selectedInvoices.size} fattur{selectedInvoices.size === 1 ? 'a' : 'e'} selezionat{selectedInvoices.size === 1 ? 'a' : 'e'} — {formatCurrency(selectedTotal)}
                </p>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <button
                  onClick={handleDownloadInvoicesZip}
                  disabled={pdfLoading}
                  className="sc-btn-primary text-sm font-bold disabled:opacity-50"
                >
                  {pdfLoading ? '...' : `Scarica ${selectedInvoices.size} Fattur${selectedInvoices.size === 1 ? 'a' : 'e'}`}
                </button>
                <button
                  onClick={handleDownloadPromemoria}
                  disabled={promemoria}
                  className="px-4 py-2 bg-accent-amber text-dark-bg rounded-lg text-sm font-bold hover:brightness-110 disabled:opacity-50"
                >
                  {promemoria ? '...' : 'Scarica Promemoria'}
                </button>
                <button
                  onClick={handleCopyWhatsApp}
                  className={`sc-btn-secondary text-sm font-bold transition-colors ${
                    copiedWhatsApp ? 'border-accent-green text-accent-green' : ''
                  }`}
                >
                  {copiedWhatsApp ? 'Copiato!' : 'Copia Messaggio'}
                </button>
                {whatsappNumber ? (
                  <button
                    onClick={handleWhatsAppSend}
                    className="px-4 py-2 bg-accent-green text-dark-bg rounded-lg text-sm font-bold hover:brightness-110"
                  >
                    WhatsApp
                  </button>
                ) : (
                  <button
                    onClick={() => setPhoneEdit(data.phone || '')}
                    className="px-4 py-2 bg-accent-amber text-dark-bg rounded-lg text-sm font-bold hover:brightness-110"
                  >
                    Aggiungi Tel
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* SEZIONE FATTURE PAGATE */}
      {paidInvoices.length > 0 && (
        <div className="sc-card overflow-hidden">
          <div className="sc-card-header">
            <h2 className="text-base font-bold text-accent-green">
              Fatture Pagate ({paidInvoices.length})
            </h2>
            <span className="text-sm font-bold text-accent-green">{formatCurrency(totalPaid)}</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-dark-surface border-b border-dark-border">
                <tr>
                  <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Fattura</th>
                  <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Fonte</th>
                  <th className="px-3 py-3 text-right text-xs font-semibold text-txt-label uppercase tracking-wider">Importo</th>
                  <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Scadenza</th>
                  <th className="px-3 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Stato</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-dark-border">
                {paidInvoices.map(inv => (
                  <tr key={inv.id} className="bg-accent-green/5 hover:bg-accent-green/10 transition-colors">
                    <td className="px-3 py-3 text-sm font-medium text-txt-primary">{inv.invoice_number}</td>
                    <td className="px-3 py-3 text-sm">
                      <span className={`sc-badge ${
                        inv.source_platform === 'fatturapro' ? 'bg-accent-purple/15 text-accent-purple' : 'bg-accent-teal/15 text-accent-teal'
                      }`}>
                        {inv.source_platform === 'fatturapro' ? 'FPro' : 'F24'}
                      </span>
                    </td>
                    <td className="px-3 py-3 text-sm text-right font-medium text-accent-green">{formatCurrency(inv.amount)}</td>
                    <td className="px-3 py-3 text-sm text-txt-secondary">{formatDate(inv.due_date)}</td>
                    <td className="px-3 py-3 text-sm text-center">
                      <span className="badge-paid sc-badge">Pagato</span>
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot className="border-t-2 border-accent-green/30">
                <tr className="bg-accent-green/10">
                  <td colSpan="2" className="px-3 py-3 text-sm font-bold text-accent-green">Totale Incassato</td>
                  <td className="px-3 py-3 text-sm text-right font-bold text-accent-green">{formatCurrency(totalPaid)}</td>
                  <td colSpan="2"></td>
                </tr>
              </tfoot>
            </table>
          </div>
        </div>
      )}

      {/* SEZIONE 2: AZIONI DI RECUPERO */}
      <div className="sc-card p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-bold text-txt-primary">Azioni di Recupero</h2>
          {contactActionCount > 0 && (
            <span className="text-sm text-txt-muted">
              Azioni registrate: <span className="font-bold text-txt-primary">{contactActionCount}</span>
            </span>
          )}
        </div>

        {/* Lawyer suggestion banner */}
        {shouldSuggestLawyer && data.recovery_status !== 'lawyer' && data.recovery_status !== 'archived' && (
          <div className="mb-4 bg-accent-red/10 border-2 border-accent-red/30 rounded-lg p-4 flex items-center justify-between">
            <div>
              <p className="text-sm font-bold text-accent-red">Suggerimento: passare all'Avvocato</p>
              <p className="text-xs text-accent-red/70 mt-1">
                Sono state effettuate {contactActionCount} azioni di contatto senza esito. Si consiglia di procedere con l'avvocato.
              </p>
            </div>
            <button
              onClick={() => handleAction('lawyer')}
              disabled={actionLoading}
              className="px-5 py-2 bg-accent-red text-dark-bg rounded-lg text-sm font-bold hover:brightness-110 disabled:opacity-50 shrink-0"
            >
              Passa ad Avvocato
            </button>
          </div>
        )}

        {/* REGISTRA AZIONE */}
        <div className="mb-4">
          <div className="flex flex-wrap gap-3 items-center">
            {data.recovery_status !== 'lawyer' && data.recovery_status !== 'archived' && (
              <button
                onClick={() => {
                  const actionType = contactActionCount === 0 ? 'first_contact' : 'second_contact'
                  handleAction(actionType)
                }}
                disabled={actionLoading}
                className="px-6 py-3 bg-accent-teal text-dark-bg rounded-lg text-sm font-bold hover:brightness-110 disabled:opacity-50 flex items-center gap-2 shadow-sm"
              >
                {actionLoading ? '...' : `REGISTRA ${nextActionLabel} AZIONE`}
              </button>
            )}
            <button
              onClick={() => handleAction('lawyer')}
              disabled={actionLoading}
              className="px-4 py-2 bg-accent-red text-dark-bg rounded-lg text-sm font-medium hover:brightness-110 disabled:opacity-50"
            >
              {actionLoading ? '...' : 'Avvocato'}
            </button>
            <button
              onClick={() => handleAction('wait')}
              disabled={actionLoading}
              className="px-4 py-2 bg-accent-purple text-dark-bg rounded-lg text-sm font-medium hover:brightness-110 disabled:opacity-50"
            >
              {actionLoading ? '...' : 'Attendi'}
            </button>
            <button
              onClick={() => handleAction('archive')}
              disabled={actionLoading}
              className="px-4 py-2 bg-slate-500 text-dark-bg rounded-lg text-sm font-medium hover:brightness-110 disabled:opacity-50"
            >
              {actionLoading ? '...' : 'Archivia'}
            </button>
            <button
              onClick={() => setShowNoteInput(!showNoteInput)}
              className="sc-btn-secondary"
            >
              + Nota
            </button>
          </div>
        </div>

        {/* Date picker modal for action scheduling */}
        {showDatePicker && (
          <div className="mb-4 sc-card p-4 border-2 border-accent-teal/30 bg-accent-teal/5">
            <p className="text-sm font-bold text-txt-primary mb-3">
              Quando vuoi ricontattare questo cliente?
            </p>
            <div className="flex flex-wrap items-end gap-3">
              <div>
                <label className="text-xs text-txt-label block mb-1">Data prossimo sollecito</label>
                <input
                  type="date"
                  value={scheduledDate}
                  onChange={(e) => setScheduledDate(e.target.value)}
                  className="sc-input"
                />
              </div>
              <div className="flex gap-2">
                {[7, 14, 30].map(d => (
                  <button
                    key={d}
                    onClick={() => {
                      const dt = new Date()
                      dt.setDate(dt.getDate() + d)
                      setScheduledDate(dt.toISOString().split('T')[0])
                    }}
                    className="px-3 py-2 rounded-lg text-xs font-medium bg-dark-surface text-txt-secondary hover:bg-dark-border transition-colors"
                  >
                    +{d}gg
                  </button>
                ))}
              </div>
              <div className="flex-1" />
              <button
                onClick={() => handleAction(pendingActionType)}
                disabled={!scheduledDate || actionLoading}
                className="px-5 py-2 bg-accent-teal text-dark-bg rounded-lg text-sm font-bold hover:brightness-110 disabled:opacity-50"
              >
                {actionLoading ? '...' : 'Conferma'}
              </button>
              <button
                onClick={() => { setShowDatePicker(false); setPendingActionType(null) }}
                className="px-4 py-2 text-sm text-txt-muted hover:text-txt-primary"
              >
                Annulla
              </button>
            </div>
          </div>
        )}

        {/* Note input */}
        {showNoteInput && (
          <div className="mb-4 flex gap-2">
            <input
              type="text"
              value={actionNotes}
              onChange={(e) => setActionNotes(e.target.value)}
              placeholder="Note sull'azione..."
              className="sc-input flex-1"
            />
            <button
              onClick={() => handleAction('note')}
              disabled={!actionNotes || actionLoading}
              className="sc-btn-secondary disabled:opacity-50"
            >
              Salva Nota
            </button>
          </div>
        )}

        {/* Action history timeline */}
        {data.recovery_actions && data.recovery_actions.length > 0 && (
          <div className="mt-4 border-l-2 border-dark-border pl-4 space-y-3">
            {data.recovery_actions.map(action => (
              <div key={action.id} className="relative">
                <div className={`absolute -left-[21px] top-1 w-3 h-3 rounded-full border-2 border-dark-card ${
                  action.completed_at ? 'bg-accent-green' : 'bg-txt-muted'
                }`}></div>
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium text-txt-primary">
                    {ACTION_LABELS[action.action_type] || action.action_type}
                  </span>
                  <span className="text-xs text-txt-muted">{formatDate(action.created_at)}</span>
                  {action.completed_at && action.outcome && (
                    <span className={`text-xs px-1.5 py-0.5 rounded ${OUTCOME_COLORS[action.outcome] || 'bg-accent-green/15 text-accent-green'}`}>
                      {OUTCOME_LABELS[action.outcome] || action.outcome}
                    </span>
                  )}
                  {action.completed_at && !action.outcome && (
                    <span className="text-xs bg-accent-green/15 text-accent-green px-1.5 py-0.5 rounded">completata</span>
                  )}
                  {!action.completed_at && (
                    <>
                      {completingAction === action.id ? (
                        <div className="flex items-center gap-1 flex-wrap">
                          {Object.entries(OUTCOME_LABELS).map(([key, label]) => (
                            <button
                              key={key}
                              onClick={() => handleCompleteAction(action.id, key)}
                              className={`text-xs px-2 py-0.5 rounded border border-dark-border transition-colors ${
                                OUTCOME_COLORS[key] || 'bg-[rgba(148,163,184,0.15)] text-txt-muted'
                              } hover:opacity-80`}
                            >
                              {label}
                            </button>
                          ))}
                          <button
                            onClick={() => setCompletingAction(null)}
                            className="text-xs text-txt-muted ml-1"
                          >
                            Annulla
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setCompletingAction(action.id)}
                          className="text-xs bg-accent-green/10 text-accent-green px-2 py-0.5 rounded border border-accent-green/20 hover:bg-accent-green/20 transition-colors"
                        >
                          Completa
                        </button>
                      )}
                    </>
                  )}
                </div>
                {action.notes && (
                  <p className="text-sm text-txt-muted mt-0.5">{action.notes}</p>
                )}
                {action.scheduled_date && !action.completed_at && (
                  <p className="text-xs text-accent-teal mt-0.5">Pianificata: {formatDate(action.scheduled_date)}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* SEZIONE 3: RIEPILOGO */}
      <div className="sc-card p-6">
        <h2 className="text-base font-bold text-txt-primary mb-4">Riepilogo</h2>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="border border-dark-border rounded-lg p-4">
            <p className="text-xs text-txt-muted mb-1">Stato Recupero</p>
            <span className={`${STATUS_COLORS[data.recovery_status] || STATUS_COLORS.idle} sc-badge text-sm`}>
              {STATUS_LABELS[data.recovery_status] || 'Da Gestire'}
            </span>
          </div>
          <div className="border border-dark-border rounded-lg p-4">
            <p className="text-xs text-txt-muted mb-1">Prossima Azione</p>
            {data.next_action_date ? (
              <div>
                <p className="text-sm font-medium text-txt-primary">
                  {ACTION_LABELS[data.next_action_type] || data.next_action_type || '-'}
                </p>
                <p className="text-xs text-accent-teal">{formatDate(data.next_action_date)}</p>
              </div>
            ) : (
              <p className="text-sm text-txt-muted">Nessuna pianificata</p>
            )}
          </div>
          <div className="border border-dark-border rounded-lg p-4">
            <p className="text-xs text-txt-muted mb-1">Azioni Effettuate</p>
            <p className="text-xl font-bold text-txt-primary">{data.recovery_actions?.length || 0}</p>
          </div>
        </div>

        {/* Financial summary */}
        <div className="mt-4 grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="border border-accent-red/20 rounded-lg p-4 bg-accent-red/5">
            <p className="text-xs text-accent-red mb-1">Scaduto</p>
            <p className="text-lg font-bold text-accent-red">{formatCurrency(totalOverdue)}</p>
            <p className="text-xs text-accent-red/60">{overdueInvoices.length} fatture</p>
          </div>
          <div className="border border-accent-blue/20 rounded-lg p-4 bg-accent-blue/5">
            <p className="text-xs text-accent-blue mb-1">Totale Dovuto</p>
            <p className="text-lg font-bold text-accent-blue">{formatCurrency(data.invoices?.total_due || 0)}</p>
            <p className="text-xs text-accent-blue/60">{allUnpaid.length} fatture non pagate</p>
          </div>
          <div className="border border-accent-green/20 rounded-lg p-4 bg-accent-green/5">
            <p className="text-xs text-accent-green mb-1">Pagato</p>
            <p className="text-lg font-bold text-accent-green">
              {formatCurrency(
                (data.invoices?.items || [])
                  .filter(inv => inv.status === 'paid')
                  .reduce((sum, inv) => sum + inv.amount_due, 0)
              )}
            </p>
            <p className="text-xs text-accent-green/60">
              {(data.invoices?.items || []).filter(inv => inv.status === 'paid').length} fatture pagate
            </p>
          </div>
        </div>

        {/* Quick actions */}
        <div className="mt-4 pt-4 border-t border-dark-border flex flex-wrap gap-2">
          {selectedInvoices.size > 0 && (
            <>
              <button
                onClick={handleDownloadInvoicesZip}
                disabled={pdfLoading}
                className="sc-btn-primary text-sm font-medium disabled:opacity-50"
              >
                {pdfLoading ? '...' : `Scarica ${selectedInvoices.size} Fattur${selectedInvoices.size === 1 ? 'a' : 'e'}`}
              </button>
              <button
                onClick={handleDownloadPromemoria}
                disabled={promemoria}
                className="px-4 py-2 bg-accent-amber text-dark-bg rounded-lg text-sm font-medium hover:brightness-110 disabled:opacity-50"
              >
                {promemoria ? '...' : 'Promemoria'}
              </button>
              <button
                onClick={handleCopyWhatsApp}
                className={`sc-btn-secondary text-sm font-medium transition-colors ${
                  copiedWhatsApp ? 'border-accent-green text-accent-green' : ''
                }`}
              >
                {copiedWhatsApp ? 'Copiato!' : 'Copia Messaggio'}
              </button>
              {whatsappNumber && (
                <button
                  onClick={handleWhatsAppSend}
                  className="px-4 py-2 bg-accent-green text-dark-bg rounded-lg text-sm font-medium hover:brightness-110"
                >
                  WhatsApp
                </button>
              )}
            </>
          )}
          <button
            onClick={() => navigate('/customers')}
            className="sc-btn-secondary text-sm font-medium"
          >
            Torna alla Lista
          </button>
        </div>
      </div>
    </div>
  )
}
