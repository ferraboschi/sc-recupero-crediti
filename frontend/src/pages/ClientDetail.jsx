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
  const [selectedInvoices, setSelectedInvoices] = useState(new Set())
  const [phoneEdit, setPhoneEdit] = useState(null)
  const [updatingInvoice, setUpdatingInvoice] = useState(null)
  const [showAllInvoices, setShowAllInvoices] = useState(false)

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

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const formatCurrency = (value) =>
    new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(value)

  const formatDate = (dateStr) => {
    if (!dateStr) return '-'
    return new Date(dateStr).toLocaleDateString('it-IT')
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

  const handleDownloadPdf = async () => {
    setPdfLoading(true)
    try {
      const response = await client.get(`/recovery/customers/${customerId}/pdf-riepilogativo`, {
        responseType: 'blob',
        params: { overdue_only: true },
      })
      const url = window.URL.createObjectURL(new Blob([response.data]))
      const link = document.createElement('a')
      link.href = url
      link.setAttribute('download', `riepilogativo_${data?.ragione_sociale?.replace(/\s/g, '_')}.pdf`)
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      console.error('Error downloading PDF:', err)
      alert('Errore nella generazione del PDF')
    } finally {
      setPdfLoading(false)
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
      const dueDate = inv.due_date ? new Date(inv.due_date).toLocaleDateString('it-IT') : 'N/D'
      msg += `- Fatt. ${inv.invoice_number}: ${new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(inv.amount_due)} (scad. ${dueDate})\n`
    })

    msg += `\nTotale: ${new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(totalSelected)}\n\n`
    msg += `Coordinate bancarie:\nIBAN: IT60F0306909606100000194066\nIntestatario: Wagyu Company S.R.L.\nCausale: Saldo fatture ${data.ragione_sociale}\n\n`
    msg += `La preghiamo di provvedere al saldo o contattarci per chiarimenti.\n\nGrazie,\nSake Company`

    return msg
  }

  const handleWhatsAppSend = () => {
    if (!data?.phone) return
    const number = data.phone.replace(/[^+\d]/g, '')
    const message = buildWhatsAppMessage()
    const url = `https://wa.me/${number}?text=${encodeURIComponent(message)}`
    window.open(url, '_blank')
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
  const whatsappNumber = data.phone ? data.phone.replace(/[^+\d]/g, '') : null

  // By default show only overdue, unless user toggles
  const visibleInvoices = showAllInvoices
    ? (data.invoices?.items || [])
    : overdueInvoices

  const selectedTotal = (data.invoices?.items || [])
    .filter(inv => selectedInvoices.has(inv.id))
    .reduce((sum, inv) => sum + inv.amount_due, 0)

  return (
    <div className="space-y-6">
      {/* Back button */}
      <button
        onClick={() => navigate('/customers')}
        className="flex items-center gap-2 text-sm text-slate-600 hover:text-blue-600"
      >
        &larr; Torna ai Clienti
      </button>

      {/* Customer Header */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-bold text-slate-900">{data.ragione_sociale}</h1>
            <div className="mt-2 space-y-1 text-sm text-slate-600">
              {data.partita_iva && <p>P.IVA: <span className="font-mono">{data.partita_iva}</span></p>}
              {data.codice_fiscale && <p>C.F.: <span className="font-mono">{data.codice_fiscale}</span></p>}
              {data.email && <p>Email: {data.email}</p>}
              <div className="flex items-center gap-2">
                <span>Tel:</span>
                {phoneEdit !== null ? (
                  <div className="flex items-center gap-2">
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
                  <div className="flex items-center gap-2">
                    <span className="font-mono">{data.phone || 'Non disponibile'}</span>
                    <button
                      onClick={() => setPhoneEdit(data.phone || '')}
                      className="text-blue-600 text-xs underline"
                    >
                      Modifica
                    </button>
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

      {/* WhatsApp Send Section — prominent when invoices are selected */}
      {whatsappNumber && selectedInvoices.size > 0 && (
        <div className="bg-green-50 rounded-lg p-4 border border-green-200">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-green-900">
                {selectedInvoices.size} fattur{selectedInvoices.size === 1 ? 'a' : 'e'} selezionat{selectedInvoices.size === 1 ? 'a' : 'e'} — {formatCurrency(selectedTotal)}
              </p>
              <p className="text-xs text-green-700 mt-1">
                Invia un messaggio WhatsApp con il riepilogo delle fatture selezionate
              </p>
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={handleDownloadPdf}
                disabled={pdfLoading}
                className="px-4 py-2 bg-amber-600 text-white rounded-lg text-sm font-medium hover:bg-amber-700 disabled:opacity-50"
              >
                {pdfLoading ? '...' : 'PDF'}
              </button>
              <button
                onClick={handleWhatsAppSend}
                className="px-6 py-2 bg-green-600 text-white rounded-lg text-sm font-bold hover:bg-green-700 flex items-center gap-2"
              >
                <span>Invia WhatsApp</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* No phone warning */}
      {!whatsappNumber && overdueInvoices.length > 0 && (
        <div className="bg-yellow-50 rounded-lg p-4 border border-yellow-200 flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-yellow-900">
              Telefono mancante — impossibile inviare WhatsApp
            </p>
            <p className="text-xs text-yellow-700 mt-1">
              Aggiungi il numero di telefono per poter inviare il riepilogo fatture
            </p>
          </div>
          <button
            onClick={() => setPhoneEdit(data.phone || '')}
            className="px-4 py-2 bg-yellow-600 text-white rounded-lg text-sm font-medium hover:bg-yellow-700"
          >
            Aggiungi Telefono
          </button>
        </div>
      )}

      {/* Recovery Actions */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <h2 className="text-lg font-bold text-slate-900 mb-4">Azioni di Recupero</h2>

        {/* Action buttons */}
        <div className="flex flex-wrap gap-3 mb-4">
          {Object.entries(ACTION_COLORS).map(([type, colorClass]) => (
            <button
              key={type}
              onClick={() => handleAction(type)}
              disabled={actionLoading}
              className={`px-4 py-2 text-white rounded-lg text-sm font-medium disabled:opacity-50 ${colorClass}`}
            >
              {actionLoading ? '...' : ACTION_LABELS[type]}
            </button>
          ))}
          <button
            onClick={() => setShowNoteInput(!showNoteInput)}
            className="px-4 py-2 bg-slate-100 text-slate-700 rounded-lg text-sm font-medium hover:bg-slate-200"
          >
            + Nota
          </button>
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
                <div className="absolute -left-[21px] top-1 w-3 h-3 rounded-full bg-slate-400 border-2 border-white"></div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-slate-900">
                    {ACTION_LABELS[action.action_type] || action.action_type}
                  </span>
                  <span className="text-xs text-slate-400">{formatDate(action.created_at)}</span>
                  {action.completed_at && (
                    <span className="text-xs bg-green-100 text-green-600 px-1.5 py-0.5 rounded">completata</span>
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

      {/* Invoices Table */}
      <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between">
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
                <th className="px-3 py-3 text-left text-sm font-semibold text-slate-900">Fattura</th>
                <th className="px-3 py-3 text-left text-sm font-semibold text-slate-900">Fonte</th>
                <th className="px-3 py-3 text-right text-sm font-semibold text-slate-900">Dovuto</th>
                <th className="px-3 py-3 text-left text-sm font-semibold text-slate-900">Scadenza</th>
                <th className="px-3 py-3 text-right text-sm font-semibold text-slate-900">GG</th>
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
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                      inv.source_platform === 'fatturapro' ? 'bg-indigo-100 text-indigo-700' : 'bg-teal-100 text-teal-700'
                    }`}>
                      {inv.source_platform === 'fatturapro' ? 'FPro' : 'F24'}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-sm text-right font-medium">{formatCurrency(inv.amount_due)}</td>
                  <td className="px-3 py-3 text-sm text-slate-600">{formatDate(inv.due_date)}</td>
                  <td className="px-3 py-3 text-sm text-right">
                    <span className={inv.days_overdue > 30 ? 'text-red-600 font-medium' : 'text-slate-600'}>
                      {inv.days_overdue || 0}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-sm text-center">
                    <span className={`${INVOICE_STATUS_COLORS[inv.status] || 'bg-slate-100 text-slate-600'} px-2 py-1 rounded-full text-xs font-medium`}>
                      {inv.status === 'open' ? 'Aperto' : inv.status === 'paid' ? 'Pagato' : inv.status}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-sm text-center">
                    {inv.status !== 'paid' ? (
                      <button
                        onClick={() => handleMarkPaid(inv.id)}
                        disabled={updatingInvoice === inv.id}
                        className="px-2 py-1 bg-green-600 text-white rounded text-xs font-medium hover:bg-green-700 disabled:opacity-50"
                      >
                        {updatingInvoice === inv.id ? '...' : 'Pagato'}
                      </button>
                    ) : (
                      <button
                        onClick={() => handleReopenInvoice(inv.id)}
                        disabled={updatingInvoice === inv.id}
                        className="px-2 py-1 bg-slate-200 text-slate-600 rounded text-xs font-medium hover:bg-slate-300 disabled:opacity-50"
                      >
                        {updatingInvoice === inv.id ? '...' : 'Riapri'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
