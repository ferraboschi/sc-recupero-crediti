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

// Semaforo di verifica P.IVA + ragione sociale (dati dall'API, campo
// `verification`). Il verde compare SOLO quando la corrispondenza è
// davvero garantita (P.IVA uguale + ragione sociale coincidente).
const VERIFY_STYLE = {
  verified: { badge: 'bg-accent-green/15 text-accent-green', dot: '✔︎', label: 'Verificato', ring: 'border-accent-green/30 bg-accent-green/5' },
  warning: { badge: 'bg-accent-amber/15 text-accent-amber', dot: '⚠', label: 'Da controllare', ring: 'border-accent-amber/30 bg-accent-amber/5' },
  critical: { badge: 'bg-accent-red/15 text-accent-red', dot: '⛔', label: 'Discordante', ring: 'border-accent-red/30 bg-accent-red/5' },
}

// Stato "verificata a mano": la stessa convenzione muta dell'audit
// (grigio = tolto dalla lavorazione, MAI verde: il verde resta una garanzia).
const REVIEWED_STYLE = { badge: 'bg-[rgba(148,163,184,0.15)] text-txt-muted', dot: '✔︎', label: 'Verificata a mano' }

// Badge cliccabile: apre/chiude il pannello di dettaglio della verifica.
function VerifyBadge({ v, open, onToggle, reviewed }) {
  const base = reviewed ? REVIEWED_STYLE : (VERIFY_STYLE[v?.level] || VERIFY_STYLE.warning)
  // Verde da conferma UMANA (intestazione accettata): resta verde ma con
  // etichetta onesta e distinta dal verde-garanzia da checksum.
  const s = (!reviewed && v?.manual_confirmed)
    ? { ...base, label: 'Confermato a mano' }
    : base
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onToggle() }}
      className={`sc-badge ${s.badge} cursor-pointer hover:brightness-110 whitespace-nowrap`}
      title="Controllo P.IVA e ragione sociale — clicca per il dettaglio"
    >
      {s.dot} {s.label} {open ? '▴' : '▾'}
    </button>
  )
}

// Riga di confronto (valore fattura vs valore cliente) con evidenza.
function VerifyRow({ label, left, right, ok }) {
  const mark = ok === true ? '✔︎' : ok === false ? '✗' : ''
  const color = ok === true ? 'text-accent-green' : ok === false ? 'text-accent-red' : 'text-txt-muted'
  // Mobile (<sm): label sopra, poi fattura|cliente in 2 colonne. sm+: la riga
  // torna a 3 colonne (label | fattura | cliente) grazie a sm:contents sul
  // wrapper interno. Nessuna colonna fissa che sfori il layout stretto.
  return (
    <div className="py-1 text-sm sm:grid sm:grid-cols-[130px_minmax(0,1fr)_minmax(0,1fr)] sm:gap-2 sm:items-start">
      <span className="block mb-0.5 sm:mb-0 sm:pt-0.5 text-xs font-semibold text-txt-label uppercase tracking-wider">{label}</span>
      <div className="grid grid-cols-2 gap-2 sm:contents">
        <span className="min-w-0 text-txt-primary break-words">{left || <span className="text-txt-muted">—</span>}</span>
        <span className="min-w-0 text-txt-primary break-words">
          <span className={`mr-1 ${color}`}>{mark}</span>
          {right || <span className="text-txt-muted">—</span>}
        </span>
      </div>
    </div>
  )
}

// Pannello di dettaglio: messaggio di garanzia/avviso + valori affiancati.
function VerifyDetail({ v }) {
  if (!v) return null
  const s = VERIFY_STYLE[v.level] || VERIFY_STYLE.warning
  return (
    <div className={`rounded-lg border ${s.ring} p-3`}>
      <p className={`text-sm font-medium break-words ${v.level === 'verified' ? 'text-accent-green' : v.level === 'critical' ? 'text-accent-red' : 'text-accent-amber'}`}>
        {v.message}
      </p>
      <div className="mt-2 pt-2 border-t border-dark-border">
        <div className="pb-1 sm:grid sm:grid-cols-[130px_minmax(0,1fr)_minmax(0,1fr)] sm:gap-2">
          <span className="hidden sm:block"></span>
          <div className="grid grid-cols-2 gap-2 sm:contents">
            <span className="text-xs font-semibold text-txt-label uppercase tracking-wider">Sulla fattura</span>
            <span className="text-xs font-semibold text-txt-label uppercase tracking-wider">Sul cliente</span>
          </div>
        </div>
        <VerifyRow
          label="P.IVA"
          left={v.invoice_piva}
          right={v.customer_piva}
          ok={v.piva_match ? true : v.piva_conflict ? false : null}
        />
        <VerifyRow
          label="Ragione soc."
          left={v.invoice_name}
          right={v.customer_name}
          ok={v.name_equivalent ? true : (v.name_score != null && v.name_score < 40) ? false : null}
        />
        {v.name_score != null && (
          <p className="text-xs text-txt-muted mt-1">Somiglianza nomi: {v.name_score}%</p>
        )}
      </div>
    </div>
  )
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
  // Selezione mista (fatture a stadi diversi): quale stadio sto scrivendo
  const [messageStage, setMessageStage] = useState(null)
  useEffect(() => { setMessageStage(null) }, [selectedInvoices])
  // Assegno in mano (Fase 3): form inline per fattura {invoiceId, expected, note}
  const [assegnoForm, setAssegnoForm] = useState(null)
  const [phoneEdit, setPhoneEdit] = useState(null)
  const [updatingInvoice, setUpdatingInvoice] = useState(null)
  const [showAllInvoices, setShowAllInvoices] = useState(false)
  const [openVerify, setOpenVerify] = useState(() => new Set())
  const [invoiceSortBy, setInvoiceSortBy] = useState('due_date')
  const [invoiceSortOrder, setInvoiceSortOrder] = useState('asc')
  const [neighbors, setNeighbors] = useState({ prev_id: null, next_id: null, position: null, total: null })
  const [completingAction, setCompletingAction] = useState(null)
  const [selectedOutcome, setSelectedOutcome] = useState('')
  const [copiedWhatsApp, setCopiedWhatsApp] = useState(false)
  const [editingDateActionId, setEditingDateActionId] = useState(null)
  const [editingDateValue, setEditingDateValue] = useState('')
  const [promemoria, setPromemoria] = useState(false)
  const [selectedWhatsAppPhone, setSelectedWhatsAppPhone] = useState(null)
  const [showDatePicker, setShowDatePicker] = useState(false)
  const [pendingActionType, setPendingActionType] = useState(null)
  const [scheduledDate, setScheduledDate] = useState('')
  // Registrazione automatica del sollecito dopo Copia/WhatsApp
  const [sollecitoToast, setSollecitoToast] = useState(null)
  const [sollecitoError, setSollecitoError] = useState(null)
  // Fatture in quarantena suggerite a questo cliente ("In attesa di conferma")
  const [suggestionActingId, setSuggestionActingId] = useState(null)
  const [suggestionError, setSuggestionError] = useState(null)
  // Audit abbinamenti del cliente aperto (azionabile): esito verify per-fattura
  const [auditData, setAuditData] = useState(null)
  const [auditLoading, setAuditLoading] = useState(true)
  const [auditError, setAuditError] = useState(null)
  const [auditActingId, setAuditActingId] = useState(null)
  const [includeReviewedAudit, setIncludeReviewedAudit] = useState(false)
  // Verifiche manuali sul semaforo per-riga (audit_reviewed_at via
  // mark-reviewed). Seminato da /customers/{id} (campo `reviewed` per
  // fattura), così lo stato sopravvive all'hard-reload.
  const [reviewedRows, setReviewedRows] = useState(() => new Set())
  const [rowReviewActingId, setRowReviewActingId] = useState(null)
  // Menu guidato "Risolvi" sulle righe discordanti dell'audit: 3 vie.
  // ① picker riassegnazione (ricerca fuzzy /customers/suggest)
  const [resolveReassignId, setResolveReassignId] = useState(null)
  const [resolveQuery, setResolveQuery] = useState('')
  const [resolveResults, setResolveResults] = useState([])
  const [resolveSearching, setResolveSearching] = useState(false)
  // ② anteprima d'impatto del rinomino (assign-name-to-customer senza confirm)
  const [renamePreview, setRenamePreview] = useState(null)
  const [renameLoadingId, setRenameLoadingId] = useState(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      const response = await client.get(`/customers/${customerId}`)
      // Scheda fusa in un'altra (duplicato deduplicato): reindirizza alla
      // sopravvissuta invece di mostrare un profilo vuoto (link/bookmark vecchi).
      if (response.data.merged_into) {
        navigate(`/customers/${response.data.merged_into}`, { replace: true })
        return
      }
      setData(response.data)
      const items = response.data.invoices?.items || []
      const overdueIds = items
        .filter(inv => inv.days_overdue > 0 && inv.status !== 'paid' && !inv.in_incasso)
        .map(inv => inv.id)
      setSelectedInvoices(new Set(overdueIds))
      // Semina le "verificate a mano" dal backend: senza, un hard-reload
      // farebbe ricomparire il ⚠ su fatture già controllate dall'operatore.
      setReviewedRows(new Set(items.filter(inv => inv.reviewed).map(inv => inv.id)))
    } catch (err) {
      setError('Errore nel caricamento del cliente')
      console.error(err)
    } finally {
      setLoading(false)
    }
  }, [customerId, navigate])

  const fetchNeighbors = useCallback(async () => {
    try {
      const response = await client.get(`/customers/${customerId}/neighbors`)
      setNeighbors(response.data)
    } catch (err) {
      console.error('Error fetching neighbors:', err)
    }
  }, [customerId])

  // Audit abbinamenti del cliente aperto: scoped alle SUE fatture (nessuna
  // scansione globale). Il client axios ritenta da solo i cold-start 502/503.
  const fetchAudit = useCallback(async () => {
    try {
      setAuditLoading(true)
      setAuditError(null)
      const res = await client.get(`/customers/${customerId}/audit`, {
        params: { include_reviewed: includeReviewedAudit },
      })
      setAuditData(res.data)
      // Semina lo stato "verificata a mano" del semaforo per-riga con ciò
      // che l'audit sa (le già verificate compaiono con include_reviewed).
      const seededIds = (res.data.items || []).filter(i => i.reviewed).map(i => i.invoice_id)
      if (seededIds.length > 0) {
        setReviewedRows(prev => {
          const next = new Set(prev)
          seededIds.forEach(id => next.add(id))
          return next
        })
      }
    } catch (err) {
      console.error('Error fetching audit:', err)
      setAuditError('Impossibile eseguire l\'audit degli abbinamenti')
    } finally {
      setAuditLoading(false)
    }
  }, [customerId, includeReviewedAudit])

  useEffect(() => {
    fetchData()
    fetchNeighbors()
  }, [fetchData, fetchNeighbors])

  // Cambiando cliente lo stato locale delle verifiche manuali riparte pulito.
  useEffect(() => {
    setReviewedRows(new Set())
    setResolveReassignId(null)
    setResolveQuery('')
    setResolveResults([])
    setRenamePreview(null)
  }, [customerId])

  // Picker "È di un altro cliente": ricerca approssimata debounced sugli
  // stessi suggerimenti fuzzy della lista Clienti (/customers/suggest).
  useEffect(() => {
    if (resolveReassignId == null) return undefined
    const q = resolveQuery.trim()
    if (q.length < 2) {
      setResolveResults([])
      return undefined
    }
    let cancelled = false
    setResolveSearching(true)
    const t = setTimeout(async () => {
      try {
        const res = await client.get('/customers/suggest', { params: { q, limit: 6 } })
        if (!cancelled) setResolveResults(res.data.items || [])
      } catch {
        if (!cancelled) setResolveResults([])
      } finally {
        if (!cancelled) setResolveSearching(false)
      }
    }, 300)
    return () => { cancelled = true; clearTimeout(t) }
  }, [resolveQuery, resolveReassignId])

  useEffect(() => {
    fetchAudit()
  }, [fetchAudit])

  // Azioni dell'audit: gli endpoint sono quelli ESISTENTI di positions.py.
  // Dopo ogni azione si ricaricano scheda + audit (unlink cambia totali).
  const runAuditAction = async (invoiceId, request) => {
    try {
      setAuditActingId(invoiceId)
      setAuditError(null)
      await request()
      await Promise.all([fetchData(), fetchAudit()])
    } catch (err) {
      console.error('Audit action error:', err)
      setAuditError(err.response?.data?.detail || 'Errore durante l\'operazione')
    } finally {
      setAuditActingId(null)
    }
  }

  const handleAuditUnlink = (item) => {
    if (!window.confirm(
      `Scollegare la fattura ${item.invoice_number} da "${data?.ragione_sociale}"?\n\n`
      + 'La fattura tornerà senza cliente e non sarà più riabbinata in automatico.'
    )) return
    runAuditAction(item.invoice_id, () => client.post(`/positions/${item.invoice_id}/unlink`))
  }

  const handleAuditAssignPiva = (item) => {
    const piva = item.verification?.invoice_piva
    if (!window.confirm(
      `Assegnare la P.IVA ${piva} (dalla fattura ${item.invoice_number}) al cliente "${data?.ragione_sociale}"?\n\n`
      + 'Serve quando la fattura ha una P.IVA valida ma il cliente non ne ha una: '
      + 'i prossimi abbinamenti diventeranno garantiti per P.IVA.'
    )) return
    runAuditAction(item.invoice_id, () => client.post(`/positions/${item.invoice_id}/assign-piva-to-customer`))
  }

  const handleAuditToggleReviewed = (item) => {
    const path = item.reviewed ? 'unmark-reviewed' : 'mark-reviewed'
    runAuditAction(item.invoice_id, async () => {
      await client.post(`/positions/${item.invoice_id}/${path}`)
      // Tiene allineato anche il semaforo per-riga della tabella fatture.
      setReviewedRows(prev => {
        const next = new Set(prev)
        if (item.reviewed) next.delete(item.invoice_id)
        else next.add(item.invoice_id)
        return next
      })
    })
  }

  // ── BONIFICA DUREVOLE dell'anagrafica ──────────────────────────────

  // TIER 1 — "Completa anagrafica" a livello CLIENTE: la bonifica PIÙ FORTE
  // (verde vero, checksum). Copia la P.IVA valida — condivisa da tutte le
  // fatture problematiche — sul cliente che ne è privo. Un click e tutte le
  // fatture con quella P.IVA, presenti e future, diventano verificate.
  const handleBonificaPiva = () => {
    const b = auditData?.bonifica_piva
    if (!b) return
    if (!window.confirm(
      `Completa anagrafica di "${data?.ragione_sociale}": assegnare la P.IVA ${b.piva}?\n\n`
      + `È presente su ${b.invoice_count} fattur${b.invoice_count === 1 ? 'a' : 'e'} ma manca sul cliente. `
      + 'Assegni l\'identità al cliente: da ora QUALSIASI fattura, anche FUTURA, con quella '
      + 'P.IVA si aggancia da sola a questo cliente e risulta verificata.\n\n'
      + 'La certezza è la somiglianza del NOME, non una verifica della P.IVA: se la P.IVA '
      + 'fosse un refuso, il verde comparirebbe comunque. Reversibile con "Rimuovi P.IVA".'
    )) return
    runAuditAction(b.invoice_id, () => client.post(`/positions/${b.invoice_id}/assign-piva-to-customer`))
  }

  // TIER 2 — "Conferma identità": intestazione accettata (durevole). Per i
  // casi senza P.IVA da assegnare (fattura intestata alla persona, grafie
  // diverse senza P.IVA). Registra la grafia della fattura come tratto
  // d'identità del cliente: vale per TUTTE le fatture con quella intestazione,
  // anche future — a differenza di "Segna verificato" (una tantum, per-riga).
  const handleConfirmIdentity = (item) => {
    const raw = item.verification?.invoice_name || item.invoice_number
    if (!window.confirm(
      `Confermare che l'intestazione "${raw}" appartiene a "${data?.ragione_sociale}"?\n\n`
      + 'Vale per tutte le fatture con questa intestazione — presenti e future — che diventeranno verificate. '
      + 'È una conferma manuale, reversibile dalla sezione "Intestazioni accettate".'
    )) return
    runAuditAction(item.invoice_id, () =>
      client.post(`/customers/${customerId}/accepted-names`, { invoice_id: item.invoice_id })
    )
  }

  // Rimozione di un'intestazione accettata (reversibilità): le fatture con
  // quella grafia tornano al loro esito naturale.
  const handleRemoveAcceptedName = async (entry) => {
    if (!window.confirm(
      `Rimuovere l'intestazione accettata "${entry.note || entry.name_normalized}"?\n\n`
      + 'Le fatture con questa intestazione torneranno "da controllare".'
    )) return
    try {
      await client.delete(`/customers/${customerId}/accepted-names/${entry.id}`)
      await Promise.all([fetchData(), fetchAudit()])
    } catch (err) {
      console.error('Errore rimozione intestazione accettata:', err)
      alert(err.response?.data?.detail || 'Errore durante la rimozione dell\'intestazione')
    }
  }

  // ── Menu guidato "Risolvi" (riga discordante) ──────────────────────

  // ① "È di un altro cliente": apre il picker inline, pre-compilato col
  // destinatario della fattura — nel caso tipico (BASARA su COLONIALE) la
  // prima ricerca è già quella giusta.
  const handleResolveOpenReassign = (item) => {
    setRenamePreview(null)
    if (resolveReassignId === item.invoice_id) {
      setResolveReassignId(null)
      setResolveQuery('')
      setResolveResults([])
      return
    }
    setResolveReassignId(item.invoice_id)
    setResolveQuery(item.verification?.invoice_name || '')
    setResolveResults([])
  }

  const handleResolveReassign = (item, target) => {
    if (!window.confirm(
      `Riassegnare la fattura ${item.invoice_number} da "${data?.ragione_sociale}" `
      + `a "${target.ragione_sociale}"?\n\n`
      + 'La fattura passa al nuovo cliente con abbinamento manuale.'
    )) return
    runAuditAction(item.invoice_id, async () => {
      await client.put(`/positions/${item.invoice_id}/reassign`, null, {
        params: { new_customer_id: target.id },
      })
      setResolveReassignId(null)
      setResolveQuery('')
      setResolveResults([])
    })
  }

  // ② "Il profilo ha il nome vecchio": prima chiamata SENZA confirm =
  // anteprima d'impatto (quante altre fatture diventerebbero discordanti),
  // il pannello con Conferma/Annulla è la conferma esplicita.
  const handleResolveRenamePreview = async (item) => {
    setResolveReassignId(null)
    setResolveQuery('')
    setResolveResults([])
    if (renamePreview?.invoiceId === item.invoice_id) {
      setRenamePreview(null)
      return
    }
    try {
      setRenameLoadingId(item.invoice_id)
      setAuditError(null)
      const res = await client.post(`/positions/${item.invoice_id}/assign-name-to-customer`)
      if (res.data.already_aligned) {
        setAuditError('Il nome del cliente è già identico a quello sulla fattura: niente da rinominare.')
        return
      }
      setRenamePreview({ invoiceId: item.invoice_id, ...res.data })
    } catch (err) {
      console.error('Rename preview error:', err)
      setAuditError(err.response?.data?.detail || 'Errore durante l\'anteprima del rinomino')
    } finally {
      setRenameLoadingId(null)
    }
  }

  const handleResolveRenameConfirm = (item) => {
    runAuditAction(item.invoice_id, async () => {
      await client.post(`/positions/${item.invoice_id}/assign-name-to-customer`, null, {
        // expected_customer_id = il cliente VISTO in anteprima: se un
        // reassign concorrente ha spostato la fattura, il backend fa 409
        // invece di rinominare (e lockare) la vittima sbagliata.
        params: { confirm: true, expected_customer_id: renamePreview?.customer_id },
      })
      setRenamePreview(null)
    })
  }

  // Sblocco amministrativo del nome (via di ritorno da un rinomino
  // sbagliato): rimosso il lock, il sync Shopify torna a governare la
  // ragione sociale dal giro successivo.
  const handleUnlockName = async () => {
    if (!window.confirm(
      `Sbloccare il nome "${data?.ragione_sociale}"?\n\n`
      + 'Il sync Shopify tornerà a governare la ragione sociale dal prossimo '
      + 'giro: serve per rimediare a un rinomino sbagliato.'
    )) return
    try {
      await client.post(`/customers/${customerId}/unlock-name`)
      await fetchData()
    } catch (err) {
      console.error('Errore sblocco nome:', err)
      alert(err.response?.data?.detail || 'Errore durante lo sblocco del nome')
    }
  }

  // "Segna verificato" dal pannello del semaforo per-riga: stesso endpoint
  // dell'audit (mark-reviewed). Dopo l'azione si ricarica l'audit, così i
  // conteggi ("N da sistemare", "già verificate") restano coerenti.
  const handleRowToggleReviewed = async (inv) => {
    const isReviewed = reviewedRows.has(inv.id)
    const path = isReviewed ? 'unmark-reviewed' : 'mark-reviewed'
    try {
      setRowReviewActingId(inv.id)
      await client.post(`/positions/${inv.id}/${path}`)
      setReviewedRows(prev => {
        const next = new Set(prev)
        if (isReviewed) next.delete(inv.id)
        else next.add(inv.id)
        return next
      })
      await fetchAudit()
    } catch (err) {
      console.error('Errore verifica manuale:', err)
      alert(err.response?.data?.detail || 'Errore durante la registrazione della verifica manuale')
    } finally {
      setRowReviewActingId(null)
    }
  }

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
      // Il backend spiega il perché (es. contatto già pianificato,
      // sollecito già registrato oggi): mostrarlo, non un errore generico.
      alert(err.response?.data?.detail || 'Errore nella creazione dell\'azione')
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

  const handleRescheduleAction = async (actionId, newDate) => {
    try {
      await client.patch(`/recovery/customers/${customerId}/actions/${actionId}/reschedule`, null, {
        params: { new_date: newDate },
      })
      setEditingDateActionId(null)
      setEditingDateValue('')
      await fetchData()
    } catch (err) {
      console.error('Error rescheduling action:', err)
      alert('Errore nell\'aggiornamento della data')
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

  const toggleVerify = (id) => {
    setOpenVerify(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAllOverdue = () => {
    if (!data?.invoices?.items) return
    const overdueIds = data.invoices.items
      .filter(inv => inv.days_overdue > 0 && inv.status !== 'paid' && !inv.in_incasso)
      .map(inv => inv.id)
    setSelectedInvoices(new Set(overdueIds))
  }

  // Il SOGGETTO è la FATTURA: il tono segue lo stadio della singola fattura
  // (sollecito_count dalla tabella di join), non il contatore della pratica.
  // La selezione si divide in due gruppi: mai sollecitate (→ 1°, cordiale) e
  // già sollecitate (→ 2°, perentorio). Se la selezione è mista si manda UN
  // messaggio per stadio; l'operatore sceglie con i chip.
  const selectedInvoiceObjs = (data?.invoices?.items || []).filter(inv => selectedInvoices.has(inv.id))
  // Stadio EFFETTIVO della fattura: i solleciti ricevuti, MENO quello di oggi
  // (un ri-copy in giornata è lo stesso sollecito), PIÙ i contatti ereditati
  // da una pratica archiviata (il tono non riparte mai cordiale).
  const inheritedContacts = data?.case?.inherited_contacts || 0
  // Contatti storici senza fatture collegate: il tono resta perentorio.
  const forceSecond = !!data?.case?.has_unlinked_contacts
  const effStage = (inv) => (inv.sollecito_count || 0) + inheritedContacts + (forceSecond ? 1 : 0)
  // Le fatture GIÀ sollecitate oggi non si rimandano: gruppo a parte, non
  // inviabile (un ri-copy in giornata non è un nuovo sollecito).
  const stageGroups = {
    today: selectedInvoiceObjs.filter(inv => inv.sollecito_today),
    first: selectedInvoiceObjs.filter(inv => !inv.sollecito_today && effStage(inv) === 0),
    second: selectedInvoiceObjs.filter(inv => !inv.sollecito_today && effStage(inv) >= 1),
  }
  const groupTotal = (st) => (stageGroups[st] || []).reduce((s, inv) => s + (inv.amount_due || 0), 0)
  const isMixed = (stageGroups.first.length > 0 && stageGroups.second.length > 0)
    || (stageGroups.today.length > 0 && (stageGroups.first.length + stageGroups.second.length) > 0)
  // Stadio attivo: la scelta dell'operatore, altrimenti il primo gruppo NON
  // vuoto (mai un gruppo vuoto: i pulsanti resterebbero muti).
  const firstNonEmpty = stageGroups.first.length > 0 ? 'first' : 'second'
  const activeStage = isMixed
    ? ((messageStage && stageGroups[messageStage]?.length > 0) ? messageStage : firstNonEmpty)
    : (stageGroups.second.length > 0 ? 'second' : 'first')
  const activeGroup = stageGroups[activeStage]

  const stagePicker = isMixed ? (
    <div
      className="inline-flex items-center gap-1.5 text-xs text-txt-muted"
      title="Fatture a stadi diversi: un messaggio per stadio, il tono segue la fattura"
    >
      <span>Messaggio per:</span>
      {stageGroups.today.length > 0 && (
        <span className="sc-badge bg-dark-surface text-txt-muted" title="Già sollecitate oggi: non si rimandano">
          già oggi · {stageGroups.today.length} fatt.
        </span>
      )}
      {['first', 'second'].map(st => (
        <button
          key={st}
          type="button"
          onClick={() => setMessageStage(st)}
          className={`sc-badge ${activeStage === st
            ? 'bg-accent-teal/20 text-accent-teal ring-1 ring-accent-teal/40'
            : 'bg-dark-surface text-txt-secondary hover:text-txt-primary'}`}
        >
          {st === 'first' ? '1° sollecito' : '2° sollecito'} · {stageGroups[st].length} fatt. · {formatCurrency(groupTotal(st))}
        </button>
      ))}
    </div>
  ) : null

  const buildWhatsAppMessage = () => {
    if (!data || activeGroup.length === 0) return ''
    const selected = activeGroup
    const totalSelected = selected.reduce((sum, inv) => sum + inv.amount_due, 0)

    // Tono dallo STADIO delle fatture del gruppo (per-fattura): dopo un saldo
    // completo, o per una fattura nuova, si riparte cordiale.
    const isSecondContact = activeStage === 'second'

    let msg = ''

    if (isSecondContact) {
      // Secondo sollecito — tono perentorio
      msg += `Spett.le ${data.ragione_sociale},\n\n`
      msg += `nonostante il nostro precedente sollecito, risultano ancora non saldate le seguenti fatture:\n\n`
    } else {
      // Primo contatto — tono cordiale
      msg += `Gentile ${data.ragione_sociale},\n\n`
      msg += `le scriviamo per ricordarle che risultano in sospeso le seguenti fatture:\n\n`
    }

    selected.forEach(inv => {
      const orderRef = inv.shopify_order_number ? ` [Ordine ${inv.shopify_order_number}]` : ''
      const importo = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(inv.amount_due)
      // Scadenza citata SOLO se reale: una scadenza stimata (emissione+30)
      // asserita al cliente come vera è già stata fonte di contestazioni.
      let dateRef = ''
      if (inv.due_date && inv.due_date_source === 'real') {
        dateRef = ` (scad. ${new Date(inv.due_date + 'T00:00:00').toLocaleDateString('it-IT')})`
      } else if (inv.issue_date) {
        dateRef = ` (del ${new Date(inv.issue_date + 'T00:00:00').toLocaleDateString('it-IT')})`
      }
      msg += `- Fatt. ${inv.invoice_number}${orderRef}: ${importo}${dateRef}\n`
    })

    msg += `\nTotale: ${new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(totalSelected)}\n\n`

    if (isSecondContact) {
      msg += `Vi informiamo che in assenza di pagamento entro 7 giorni, la pratica verrà automaticamente trasmessa al nostro studio legale per il recupero del credito, con aggravio di spese legali e interessi di mora a Vostro carico.\n\n`
      msg += `IBAN: IT44N0200801671000105175151\nIntestatario: Sake Company srl\n\n`
      msg += `Sake Company — Ufficio Amministrativo`
    } else {
      msg += `Coordinate bancarie:\nIBAN: IT44N0200801671000105175151\nIntestatario: Sake Company srl\nCausale: Saldo fatture ${data.ragione_sociale}\n\n`
      msg += `La preghiamo di provvedere al saldo o contattarci per chiarimenti.\n\nGrazie,\nSake Company`
    }

    return msg
  }

  const getWhatsAppNumber = () => {
    const raw = selectedWhatsAppPhone || data?.phone || ''
    return raw.replace(/[^+\d]/g, '')
  }

  // La sessione JWT dura 24h: se è scaduta, la registrazione del sollecito
  // fallirebbe DOPO l'invio del messaggio. Meglio bloccarsi prima di copiare.
  const isTokenExpired = () => {
    const exp = localStorage.getItem('sc_token_expires')
    if (!exp) return false
    return new Date(exp) <= new Date()
  }

  const registerSollecito = async (channel, explicitIds = null) => {
    // Cliente escluso: il copy resta possibile ma non è un sollecito
    if (data?.excluded) return
    // Si registra il sollecito SOLO per il gruppo del messaggio copiato
    // (al retry si usano le fatture del messaggio USCITO, non la selezione attuale)
    const invoiceIds = explicitIds || activeGroup.map(inv => inv.id)
    try {
      const res = await client.post(`/recovery/customers/${customerId}/solleciti`, {
        invoice_ids: invoiceIds,
        channel,
      })
      setSollecitoError(null)
      setSollecitoToast(res.data)
      if (res.data.registered) {
        await fetchData()
      }
      setTimeout(() => setSollecitoToast(current => (current === res.data ? null : current)), 15000)
    } catch (err) {
      console.error('Errore registrazione sollecito:', err)
      // Il messaggio è GIÀ partito: l'errore di registrazione non può
      // essere silenzioso, altrimenti numerazione e tono si corrompono.
      setSollecitoError({ channel, invoiceIds, detail: err.response?.data?.detail })
    }
  }

  // ── Assegno in mano (decisioni owner): stato PER-FATTURA scritto solo qui.
  const submitAssegno = async () => {
    if (!assegnoForm) return
    try {
      await client.post(`/positions/${assegnoForm.invoiceId}/assegno`, {
        expected_date: assegnoForm.expected || null,
        note: assegnoForm.note || null,
      })
      setAssegnoForm(null)
      await fetchData()
    } catch (err) {
      alert(err.response?.data?.detail || 'Errore nella registrazione dell\'assegno')
    }
  }
  const markInsoluto = async (inv) => {
    if (!window.confirm(
      `ASSEGNO INSOLUTO sulla fattura ${inv.invoice_number}?\n\n`
      + 'La fattura torna SUBITO scaduta e lavorabile, la pratica si riapre con lo storico dei solleciti '
      + 'e il recuperato viene stornato. La riga resta segnalata in rosso.'
    )) return
    try {
      await client.post(`/positions/${inv.id}/assegno/insoluto`, {})
      await fetchData()
    } catch (err) {
      alert(err.response?.data?.detail || 'Errore nella registrazione dell\'insoluto')
    }
  }
  const cancelAssegno = async (inv) => {
    if (!window.confirm(`Annullare la registrazione dell'assegno su ${inv.invoice_number}?\n\nSolo se registrata per errore: la fattura torna scaduta senza allarme.`)) return
    try {
      await client.delete(`/positions/${inv.id}/assegno`)
      await fetchData()
    } catch (err) {
      alert(err.response?.data?.detail || 'Errore nell\'annullamento')
    }
  }

  const handleUndoSollecito = async (actionId) => {
    try {
      await client.delete(`/recovery/customers/${customerId}/solleciti/${actionId}`)
      setSollecitoToast(null)
      await fetchData()
    } catch (err) {
      console.error('Errore annullamento sollecito:', err)
      alert(err.response?.data?.detail || 'Errore nell\'annullamento del sollecito')
    }
  }

  const handleWhatsAppSend = () => {
    const number = getWhatsAppNumber()
    if (!number) return
    if (isTokenExpired()) {
      alert('Sessione scaduta: effettua di nuovo il login prima di inviare (il sollecito non verrebbe registrato).')
      window.location.reload()
      return
    }
    const message = buildWhatsAppMessage()
    if (!message) return
    const url = `https://wa.me/${number}?text=${encodeURIComponent(message)}`
    window.open(url, '_blank')
    registerSollecito('whatsapp_link')
  }

  const handleCopyWhatsApp = async () => {
    const message = buildWhatsAppMessage()
    if (!message) return
    if (isTokenExpired()) {
      alert('Sessione scaduta: effettua di nuovo il login prima di copiare (il sollecito non verrebbe registrato).')
      window.location.reload()
      return
    }
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
    registerSollecito('whatsapp_copy')
  }

  // Stessa regola del backend (/positions/suggestions): fuzzy sotto 85,
  // oppure score bassissimo qualunque sia il metodo.
  const isLowConfidence = (sug) =>
    ((sug.suggested_score || 0) < 85 && sug.suggested_method === 'fuzzy')
    || (sug.suggested_score || 0) < 40

  const formatScore = (score) => {
    if (score === null || score === undefined) return ''
    return Math.round(score <= 1 ? score * 100 : score)
  }

  const handleConfirmSuggestion = async (sug) => {
    if (isLowConfidence(sug) && !window.confirm(
      `Suggerimento a BASSA CONFIDENZA (${sug.suggested_method} ${formatScore(sug.suggested_score)}): `
      + `abbinare davvero la fattura ${sug.invoice_number} a "${data?.ragione_sociale}"?`
    )) return
    try {
      setSuggestionActingId(sug.id)
      setSuggestionError(null)
      await client.post(`/positions/${sug.id}/confirm-suggestion`)
      await Promise.all([fetchData(), fetchAudit()])
    } catch (err) {
      setSuggestionError(err.response?.data?.detail || 'Errore durante la conferma del suggerimento')
      console.error(err)
    } finally {
      setSuggestionActingId(null)
    }
  }

  const handleRejectSuggestion = async (sug) => {
    if (!window.confirm('La fattura resterà senza cliente e non verrà più proposta automaticamente. Continuare?')) return
    try {
      setSuggestionActingId(sug.id)
      setSuggestionError(null)
      await client.post(`/positions/${sug.id}/reject-suggestion`)
      await Promise.all([fetchData(), fetchAudit()])
    } catch (err) {
      setSuggestionError(err.response?.data?.detail || 'Errore durante il rifiuto del suggerimento')
      console.error(err)
    } finally {
      setSuggestionActingId(null)
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

  const pendingSuggestions = data.pending_suggestions || []
  const overdueInvoices = data.invoices?.items?.filter(inv => inv.days_overdue > 0 && inv.status !== 'paid') || []
  const totalOverdue = overdueInvoices.reduce((sum, inv) => sum + inv.amount_due, 0)
  const allUnpaid = data.invoices?.items?.filter(inv => inv.status !== 'paid') || []
  const paidInvoices = data.invoices?.items?.filter(inv => inv.status === 'paid') || []
  const totalPaid = paidInvoices.reduce((sum, inv) => sum + inv.amount, 0)
  const whatsappNumber = getWhatsAppNumber() || null

  // Righe GIALLE del semaforo che l'audit NON conta come problemi (verdict
  // ok ma livello warning: garanzia impossibile, non errore di abbinamento).
  // L'header le dichiara, così "in ordine" e le ⚠ in tabella non si
  // contraddicono; escluse le già verificate a mano.
  const manualCheckCount = (data.invoices?.items || []).filter(inv =>
    inv.status !== 'paid'
    && inv.verification?.verdict === 'ok'
    && inv.verification?.level === 'warning'
    && !reviewedRows.has(inv.id)
  ).length

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
      {/* Banner errore registrazione sollecito: il messaggio è GIÀ partito,
          la registrazione va ritentata o fatta a mano — mai persa in silenzio */}
      {sollecitoError && (
        <div className="fixed bottom-6 right-6 z-50 max-w-md bg-dark-card border-2 border-accent-red rounded-lg p-4 shadow-xl">
          <p className="text-sm font-bold text-accent-red">Sollecito NON registrato</p>
          <p className="text-xs text-txt-secondary mt-1">
            Il messaggio è stato copiato/inviato ma la registrazione è fallita
            {sollecitoError.detail ? `: ${sollecitoError.detail}` : ''}.
            Senza registrazione, numerazione e tono del prossimo sollecito saranno sbagliati.
          </p>
          <div className="flex gap-2 mt-3">
            <button
              onClick={() => registerSollecito(sollecitoError.channel, sollecitoError.invoiceIds)}
              className="px-3 py-1.5 bg-accent-red text-dark-bg rounded text-xs font-bold hover:brightness-110"
            >
              Riprova registrazione
            </button>
            <button
              onClick={() => setSollecitoError(null)}
              className="px-3 py-1.5 text-xs text-txt-muted hover:text-txt-primary"
            >
              Registro a mano
            </button>
          </div>
        </div>
      )}

      {/* Toast conferma sollecito registrato (con Annulla) */}
      {sollecitoToast && !sollecitoError && (
        <div className="fixed bottom-6 right-6 z-50 max-w-md bg-dark-card border border-dark-border rounded-lg p-4 shadow-xl">
          {sollecitoToast.registered ? (
            <>
              <p className="text-sm font-bold text-accent-green">
                Sollecito n. {sollecitoToast.sollecito_n} registrato
                {sollecitoToast.already_registered_today ? ' (già registrato oggi — fatture aggiornate)' : ''}
              </p>
              {sollecitoToast.next_action?.scheduled_date && (
                <p className="text-xs text-txt-secondary mt-1">
                  Prossima azione: {ACTION_LABELS[sollecitoToast.next_action.action_type] || sollecitoToast.next_action.action_type}{' '}
                  il {formatDate(sollecitoToast.next_action.scheduled_date)}
                  <span className="text-txt-muted"> (modificabile dalla timeline)</span>
                </p>
              )}
              <div className="flex gap-2 mt-2">
                {!sollecitoToast.already_registered_today && (
                  <button
                    onClick={() => handleUndoSollecito(sollecitoToast.action_id)}
                    className="px-3 py-1 text-xs text-accent-amber hover:brightness-110 border border-accent-amber/40 rounded"
                  >
                    Annulla registrazione
                  </button>
                )}
                <button
                  onClick={() => setSollecitoToast(null)}
                  className="px-3 py-1 text-xs text-txt-muted hover:text-txt-primary"
                >
                  Chiudi
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="text-sm font-medium text-txt-primary">Messaggio copiato</p>
              <p className="text-xs text-txt-muted mt-1">
                Nessuna fattura scaduta: promemoria di cortesia, sollecito non registrato.
              </p>
            </>
          )}
        </div>
      )}

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
              {/* Nome bonificato a mano: il sync non lo sovrascrive. Lo
                  sblocco è la via di ritorno da un rinomino sbagliato. */}
              {data.ragione_sociale_locked && (
                <p className="flex items-center gap-2 flex-wrap">
                  <span
                    className="sc-badge text-xs bg-[rgba(148,163,184,0.15)] text-txt-muted"
                    title="Ragione sociale corretta a mano dalla bonifica: il sync Shopify non la sovrascrive"
                  >
                    Nome bloccato (bonifica manuale)
                  </span>
                  <button
                    onClick={handleUnlockName}
                    className="text-xs text-accent-teal hover:text-accent-cyan font-medium transition-colors"
                    title="Rimuovi il blocco: il sync Shopify tornerà a governare il nome dal prossimo giro"
                  >
                    Sblocca nome
                  </button>
                </p>
              )}
            </div>
          </div>
          <div className="flex flex-col items-end gap-3">
            <span className={`${STATUS_COLORS[data.recovery_status] || STATUS_COLORS.idle} sc-badge text-sm`}>
              {STATUS_LABELS[data.recovery_status] || 'Da Gestire'}
            </span>
            {data.case?.reopened_after_archive && (
              <span
                className="sc-badge text-xs bg-accent-red/15 text-accent-red"
                title={`Pratica precedente archiviata/passata al legale: eredita ${data.case.inherited_contacts} contatti, il tono resta perentorio`}
              >
                Riaperta dopo archiviazione
              </span>
            )}
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
            {(() => {
              const notOverdue = allUnpaid.filter(inv => (inv.days_overdue || 0) <= 0 && inv.due_date)
              if (notOverdue.length > 0) {
                const nearest = notOverdue.sort((a, b) => a.due_date.localeCompare(b.due_date))[0]
                return <p className="text-xs text-accent-teal mt-0.5">Scade {formatDate(nearest.due_date)}</p>
              }
              return null
            })()}
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

      {/* VERIFICA ABBINAMENTI: audit del cliente aperto, AZIONABILE. Chiama
          /customers/{id}/audit (scoped alle sue fatture) e mette a portata di
          click Scollega / Assegna P.IVA / Segna verificato: si sanifica il
          cliente senza passare dalla pagina Sistema. */}
      <div className="sc-card overflow-hidden">
        <div className="sc-card-header flex-wrap gap-2">
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="text-base font-bold text-txt-primary">Verifica abbinamenti</h2>
            {!auditLoading && auditData && (
              auditData.problem_count > 0 ? (
                <span className={`sc-badge ${
                  auditData.worst_verdict === 'bad'
                    ? 'bg-accent-red/15 text-accent-red'
                    : 'bg-accent-amber/15 text-accent-amber'
                }`}>
                  {auditData.problem_count} da sistemare
                </span>
              ) : (
                <span className="sc-badge bg-accent-green/15 text-accent-green">Abbinamenti in ordine ✓</span>
              )
            )}
            {/* Le ⚠ del semaforo non sono problemi di abbinamento: qui si
                dichiarano, in tono muto, per non contraddire la tabella. */}
            {!auditLoading && auditData && manualCheckCount > 0 && (
              <span
                className="text-xs text-txt-muted"
                title="Righe col semaforo giallo nella tabella fatture: non sono errori di abbinamento, chiedono solo un controllo manuale. Apri la ⚠ sulla riga e usa Segna verificato."
              >
                · {manualCheckCount} da verificare a mano
              </span>
            )}
          </div>
          <button
            onClick={fetchAudit}
            disabled={auditLoading}
            className="text-sm text-accent-teal hover:text-accent-cyan font-medium transition-colors disabled:opacity-50"
          >
            {auditLoading ? 'Analisi…' : 'Ri-analizza'}
          </button>
        </div>

        {auditError && (
          <div className="px-5 py-3 text-sm text-accent-red border-b border-dark-border flex items-center justify-between gap-3">
            <span>{auditError}</span>
            <button onClick={fetchAudit} className="sc-btn-secondary text-xs shrink-0">Riprova</button>
          </div>
        )}

        {auditLoading && !auditData ? (
          <div className="p-6 flex items-center gap-3 text-txt-muted text-sm">
            <svg className="animate-spin w-5 h-5 text-accent-teal shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            <span>Analisi degli abbinamenti in corso… (al primo avvio il server può metterci qualche secondo).</span>
          </div>
        ) : auditData ? (
          <div className="p-5 space-y-4">
            {/* Conteggi per fattura */}
            <div className="grid grid-cols-3 gap-3 text-center">
              <div>
                <p className="text-2xl font-bold text-accent-green">{auditData.counts?.ok ?? 0}</p>
                <p className="text-xs text-txt-muted mt-0.5">OK</p>
              </div>
              <div>
                <p className="text-2xl font-bold text-accent-amber">{auditData.counts?.warn ?? 0}</p>
                <p className="text-xs text-txt-muted mt-0.5">Da controllare</p>
              </div>
              <div>
                <p className="text-2xl font-bold text-accent-red">{auditData.counts?.bad ?? 0}</p>
                <p className="text-xs text-txt-muted mt-0.5">Discordanti</p>
              </div>
            </div>
            <p className="text-xs text-txt-muted text-center">
              Analizzate {auditData.total_invoices} fattur{auditData.total_invoices === 1 ? 'a' : 'e'}
              {' · '}{auditData.reviewed_count ?? 0} già verificate
              {auditData.pending_count > 0 && ` · ${auditData.pending_count} in attesa di conferma`}
            </p>

            {/* TIER 1 — "Completa anagrafica" a livello cliente: la bonifica
                PIÙ FORTE (verde vero, checksum). In testa al pannello, PREFERITA
                quando disponibile: un click sana tutte le fatture, presenti e
                future, per costruzione. */}
            {auditData.bonifica_piva && (
              <div className="rounded-xl border border-accent-teal/40 bg-accent-teal/5 p-4 space-y-2">
                <p className="text-sm font-bold text-accent-teal">Completa anagrafica del cliente</p>
                <p className="text-sm text-txt-secondary">
                  Assegna la P.IVA <span className="font-mono text-txt-primary">{auditData.bonifica_piva.piva}</span>{' '}
                  — presente su {auditData.bonifica_piva.invoice_count} fattur{auditData.bonifica_piva.invoice_count === 1 ? 'a' : 'e'},
                  assente sul cliente → tutte, presenti e future, diventano verificate.
                </p>
                <p className="text-xs text-txt-muted">
                  Assegna l&apos;identità al cliente: anche una fattura FUTURA con questa P.IVA
                  si aggancerà da sola. La certezza è la somiglianza del nome, non una verifica
                  della P.IVA. Reversibile con &laquo;Rimuovi P.IVA&raquo;.
                </p>
                <button
                  onClick={handleBonificaPiva}
                  disabled={auditActingId != null}
                  className="sc-btn-primary text-sm font-bold disabled:opacity-50"
                >
                  {auditActingId != null ? '…' : `Assegna P.IVA ${auditData.bonifica_piva.piva} al cliente`}
                </button>
              </div>
            )}

            {/* Guardrail P.IVA-diverse: fatture con P.IVA differenti = forse
                due clienti finiti sullo stesso profilo. Nessuna offerta. */}
            {auditData.bonifica_piva_conflict?.length > 0 && (
              <div className="rounded-xl border border-accent-red/40 bg-accent-red/5 p-4 space-y-1">
                <p className="text-sm font-bold text-accent-red">Qui ci sono P.IVA diverse: forse due clienti</p>
                <p className="text-sm text-txt-secondary">
                  Le fatture di questo profilo portano P.IVA differenti
                  {' '}(<span className="font-mono">{auditData.bonifica_piva_conflict.join(', ')}</span>):
                  potrebbero essere due clienti diversi sotto lo stesso profilo.
                  Controlla e riassegna le fatture prima di completare l&apos;anagrafica.
                </p>
              </div>
            )}

            {auditData.problem_count === 0 ? (
              <div className="bg-accent-green/10 border border-accent-green/20 rounded-xl p-4 text-center">
                <p className="text-accent-green font-medium">
                  {includeReviewedAudit
                    ? 'Nessun abbinamento da controllare ✓'
                    : 'Gli abbinamenti di questo cliente risultano in ordine ✓'}
                </p>
                {manualCheckCount > 0 && (
                  <p className="text-xs text-txt-muted mt-1">
                    Nella tabella fatture {manualCheckCount === 1
                      ? 'resta 1 riga gialla (⚠)'
                      : `restano ${manualCheckCount} righe gialle (⚠)`}: non {manualCheckCount === 1 ? 'è un errore' : 'sono errori'} di abbinamento,
                    {manualCheckCount === 1 ? ' chiede' : ' chiedono'} solo una verifica manuale — apri la ⚠ e usa &ldquo;Segna verificato&rdquo;.
                  </p>
                )}
                {auditData.pending_count > 0 && (
                  <p className="text-xs text-txt-muted mt-1">
                    Restano {auditData.pending_count} fattur{auditData.pending_count === 1 ? 'a' : 'e'} in attesa di conferma (sezione qui sotto).
                  </p>
                )}
              </div>
            ) : (
              <div className="space-y-3">
                {auditData.items.map(item => {
                  const busy = auditActingId === item.invoice_id
                  const isBad = item.verdict === 'bad'
                  return (
                    <div key={item.invoice_id} className={`rounded-lg border p-4 space-y-3 ${
                      isBad ? 'border-accent-red/30 bg-accent-red/5' : 'border-accent-amber/30 bg-accent-amber/5'
                    }`}>
                      <div className="flex items-center justify-between gap-3 flex-wrap">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm font-mono font-medium text-txt-primary">{item.invoice_number}</span>
                          <span className={`sc-badge ${
                            isBad ? 'bg-accent-red/15 text-accent-red' : 'bg-accent-amber/15 text-accent-amber'
                          }`}>
                            {isBad ? '⛔ Discordante' : '⚠ Da controllare'}
                          </span>
                          {item.reviewed && (
                            <span className="sc-badge bg-[rgba(148,163,184,0.15)] text-txt-muted">già verificata</span>
                          )}
                        </div>
                        <span className="text-sm font-mono text-txt-secondary whitespace-nowrap">{formatCurrency(item.amount_due)}</span>
                      </div>

                      {/* Il perché + confronto P.IVA/ragione sociale affiancato */}
                      <VerifyDetail v={item.verification} />

                      {/* Riga DISCORDANTE non ancora verificata: menu guidato
                          "Risolvi" a 3 vie — la verità è una di queste.
                          ① in testa: nel caso tipico (fattura BASARA sul
                          cliente COLONIALE) l'errore è l'abbinamento. */}
                      {isBad && !item.reviewed ? (
                        <div className="space-y-2">
                          <p className="text-xs font-semibold text-txt-label uppercase tracking-wider">
                            Risolvi — qual è la verità?
                          </p>

                          {/* ① È di un altro cliente → riassegna */}
                          <button
                            onClick={() => handleResolveOpenReassign(item)}
                            disabled={busy}
                            className={`w-full text-left px-3 py-2 rounded-lg border transition-colors ${
                              resolveReassignId === item.invoice_id
                                ? 'border-accent-teal/60 bg-accent-teal/10'
                                : 'border-dark-border bg-dark-surface hover:border-accent-teal/40'
                            } ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                          >
                            <span className="text-sm font-semibold text-txt-primary">① È di un altro cliente</span>
                            <span className="block text-xs text-txt-muted mt-0.5">
                              La fattura è attaccata al cliente sbagliato: cerca quello giusto e riassegnala.
                            </span>
                          </button>
                          {resolveReassignId === item.invoice_id && (
                            <div className="ml-4 space-y-2">
                              <input
                                type="text"
                                autoFocus
                                value={resolveQuery}
                                onChange={(e) => setResolveQuery(e.target.value)}
                                placeholder="Cerca il cliente giusto (nome anche approssimato)…"
                                className="sc-input w-full"
                              />
                              {resolveSearching && (
                                <p className="text-xs text-txt-muted">Ricerca…</p>
                              )}
                              {!resolveSearching && resolveQuery.trim().length >= 2 && resolveResults.length === 0 && (
                                <p className="text-xs text-txt-muted">
                                  Nessun cliente somigliante. Prova con meno parole.
                                </p>
                              )}
                              {resolveResults.length > 0 && (
                                <div className="flex flex-wrap gap-2">
                                  {resolveResults
                                    .filter(c => c.id !== Number(customerId))
                                    .map(c => (
                                      <button
                                        key={c.id}
                                        onClick={() => handleResolveReassign(item, c)}
                                        disabled={busy}
                                        className={`px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-teal/10 text-accent-teal border border-accent-teal/30 hover:bg-accent-teal/20 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                                        title="Clicca per riassegnare la fattura a questo cliente (con conferma)"
                                      >
                                        {c.ragione_sociale}
                                        {c.partita_iva && (
                                          <span className="text-txt-muted"> · {c.partita_iva}</span>
                                        )}
                                        {c.excluded && (
                                          <span className="text-txt-muted"> (escluso)</span>
                                        )}
                                      </button>
                                    ))}
                                </div>
                              )}
                            </div>
                          )}

                          {/* ② Stessa azienda, profilo col nome vecchio →
                              aggiorna il nome dal documento (preview→confirm) */}
                          <button
                            onClick={() => handleResolveRenamePreview(item)}
                            disabled={busy || renameLoadingId === item.invoice_id}
                            className={`w-full text-left px-3 py-2 rounded-lg border transition-colors ${
                              renamePreview?.invoiceId === item.invoice_id
                                ? 'border-accent-amber/60 bg-accent-amber/10'
                                : 'border-dark-border bg-dark-surface hover:border-accent-amber/40'
                            } ${(busy || renameLoadingId === item.invoice_id) ? 'opacity-50 cursor-not-allowed' : ''}`}
                          >
                            <span className="text-sm font-semibold text-txt-primary">
                              ② Stessa azienda, il profilo ha il nome vecchio
                              {renameLoadingId === item.invoice_id && ' …'}
                            </span>
                            <span className="block text-xs text-txt-muted mt-0.5">
                              Aggiorna il nome del cliente dal documento (prima ti mostro l&apos;impatto).
                            </span>
                          </button>
                          {renamePreview?.invoiceId === item.invoice_id && (
                            <div className="ml-4 p-3 rounded-lg border border-accent-amber/30 bg-accent-amber/5 space-y-2">
                              {/* Omonimo: il vero destinatario esiste già in
                                  anagrafica — il confirm farebbe comunque 409,
                                  quindi niente bottone Conferma: si indirizza
                                  alla via ① Riassegna. */}
                              {renamePreview.homonym ? (
                                <>
                                  <p className="text-sm font-medium text-accent-red">
                                    Esiste già il cliente &ldquo;<span className="font-semibold">{renamePreview.homonym.ragione_sociale}</span>&rdquo;
                                    {renamePreview.homonym.partita_iva && ` (P.IVA ${renamePreview.homonym.partita_iva})`}:
                                    questa fattura è probabilmente sua.
                                  </p>
                                  <p className="text-xs text-txt-muted">
                                    Rinominare questo profilo creerebbe due clienti con lo stesso nome (prossime fatture in quarantena).
                                    Usa <span className="font-semibold text-txt-secondary">① &ldquo;È di un altro cliente&rdquo;</span> per riassegnarla.
                                  </p>
                                  <button
                                    onClick={() => setRenamePreview(null)}
                                    className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-dark-surface text-txt-muted hover:text-txt-secondary transition-colors"
                                  >
                                    Chiudi
                                  </button>
                                </>
                              ) : (
                                <>
                                  <p className="text-sm text-txt-primary">
                                    Rinominare &ldquo;<span className="font-semibold">{renamePreview.old_name}</span>&rdquo; in
                                    {' '}&ldquo;<span className="font-semibold">{renamePreview.new_name}</span>&rdquo;
                                    {renamePreview.impact?.would_become_discordant > 0 ? (
                                      <>
                                        {' '}renderà discordant{renamePreview.impact.would_become_discordant === 1 ? 'e' : 'i'}{' '}
                                        <span className="font-semibold text-accent-amber">
                                          {renamePreview.impact.would_become_discordant === 1
                                            ? '1 altra fattura'
                                            : `${renamePreview.impact.would_become_discordant} altre fatture`}
                                        </span>{' '}di questo cliente — confermi?
                                      </>
                                    ) : (
                                      <> non renderà discordante nessun&apos;altra fattura aperta di questo cliente — confermi?</>
                                    )}
                                  </p>
                                  {renamePreview.impact?.invoices?.length > 0 && (
                                    <p className="text-xs text-txt-muted font-mono">
                                      {renamePreview.impact.invoices.map(i => i.invoice_number).join(' · ')}
                                    </p>
                                  )}
                                  {renamePreview.impact?.would_become_warning > 0 && (
                                    <p className="text-xs text-accent-amber">
                                      {renamePreview.impact.would_become_warning === 1
                                        ? '1 altra fattura passerà a "Da controllare"'
                                        : `${renamePreview.impact.would_become_warning} altre fatture passeranno a "Da controllare"`}
                                      {renamePreview.impact.warning_invoices?.length > 0 && (
                                        <span className="font-mono"> ({renamePreview.impact.warning_invoices.map(i => i.invoice_number).join(' · ')})</span>
                                      )}
                                    </p>
                                  )}
                                  {/* Le PAGATE non compaiono nell'audit di default:
                                      senza questa voce il caso più pericoloso
                                      (1 aperta + N pagate col vecchio nome)
                                      sembrava innocuo. */}
                                  {renamePreview.impact?.paid_would_become_discordant > 0 && (
                                    <p className="text-xs text-accent-amber">
                                      {renamePreview.impact.paid_would_become_discordant === 1
                                        ? '1 fattura PAGATA resterà intestata al vecchio nome su questo profilo'
                                        : `${renamePreview.impact.paid_would_become_discordant} fatture PAGATE resteranno intestate al vecchio nome su questo profilo`}
                                      {renamePreview.impact.paid_invoices?.length > 0 && (
                                        <span className="font-mono"> ({renamePreview.impact.paid_invoices.map(i => i.invoice_number).join(' · ')})</span>
                                      )}
                                    </p>
                                  )}
                                  {renamePreview.similarity != null && (
                                    <p className={`text-xs ${renamePreview.similarity < 40 ? 'text-accent-red' : 'text-txt-muted'}`}>
                                      Somiglianza tra i due nomi: {renamePreview.similarity}%
                                      {renamePreview.similarity < 40 && (
                                        <> — nomi molto diversi: se non sei certo che sia la stessa azienda, meglio ① Riassegna.</>
                                      )}
                                    </p>
                                  )}
                                  <div className="flex items-center gap-2">
                                    <button
                                      onClick={() => handleResolveRenameConfirm(item)}
                                      disabled={busy}
                                      className={`px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent-amber/15 text-accent-amber hover:bg-accent-amber/25 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                                    >
                                      {busy ? '…' : 'Conferma rinomina'}
                                    </button>
                                    <button
                                      onClick={() => setRenamePreview(null)}
                                      disabled={busy}
                                      className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-dark-surface text-txt-muted hover:text-txt-secondary transition-colors"
                                    >
                                      Annulla
                                    </button>
                                  </div>
                                  <p className="text-xs text-txt-muted">
                                    Il nome resterà bloccato: il sync Shopify non potrà più sovrascriverlo (Sblocca nome in testa alla scheda per tornare indietro). La fattura non viene toccata.
                                  </p>
                                </>
                              )}
                            </div>
                          )}

                          {/* ③ Stessa azienda, solo grafia diversa. L'azione
                              PRINCIPALE è la conferma d'identità DUREVOLE
                              (intestazione accettata: vale anche per le
                              future); il "segna solo questa" resta l'opzione
                              quieta una tantum. Quando c'è una P.IVA da
                              assegnare (Tier 1) quella è più forte, quindi la
                              conferma d'identità qui si mostra solo senza. */}
                          {!auditData.bonifica_piva ? (
                            <div className="w-full rounded-lg border border-dark-border bg-dark-surface p-1">
                              <button
                                onClick={() => handleConfirmIdentity(item)}
                                disabled={busy}
                                className={`w-full text-left px-3 py-2 rounded-lg hover:bg-accent-green/10 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                              >
                                <span className="text-sm font-semibold text-txt-primary">③ Stessa azienda, solo grafia diversa</span>
                                <span className="block text-xs text-txt-muted mt-0.5">
                                  Conferma che questa intestazione è del cliente: durevole, vale per tutte le fatture — anche future.
                                </span>
                              </button>
                              <button
                                onClick={() => handleAuditToggleReviewed(item)}
                                disabled={busy}
                                className={`px-3 py-1 text-xs text-txt-muted hover:text-txt-secondary transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                                title="Verifica una tantum: silenzia solo questa fattura, non vale per le future"
                              >
                                oppure segna solo questa fattura (una volta)
                              </button>
                            </div>
                          ) : (
                            <button
                              onClick={() => handleAuditToggleReviewed(item)}
                              disabled={busy}
                              className={`w-full text-left px-3 py-2 rounded-lg border border-dark-border bg-dark-surface hover:border-accent-green/40 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                            >
                              <span className="text-sm font-semibold text-txt-primary">③ Stessa azienda, solo grafia diversa</span>
                              <span className="block text-xs text-txt-muted mt-0.5">
                                L&apos;abbinamento è giusto: segna verificato e silenzia l&apos;avviso.
                              </span>
                            </button>
                          )}

                          {/* Via d'uscita fuori menu: non sai di chi è. */}
                          <div className="flex items-center gap-2 pt-1">
                            <span className="text-xs text-txt-muted">Nessuna delle tre?</span>
                            <button
                              onClick={() => handleAuditUnlink(item)}
                              disabled={busy}
                              className={`px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent-red/15 text-accent-red hover:bg-accent-red/25 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                            >
                              {busy ? '…' : 'Scollega senza riabbinare'}
                            </button>
                          </div>
                        </div>
                      ) : (
                        /* Righe warn (o bad già verificate): azioni piatte.
                           La CONFERMA D'IDENTITÀ (durevole) è l'azione
                           PRINCIPALE quando non c'è una P.IVA a livello cliente
                           da assegnare (Tier 1 batte Tier 2); il "Segna
                           verificato" (una tantum) resta l'opzione quieta. */
                        <div className="flex flex-wrap items-center gap-2">
                          {!auditData.bonifica_piva && !item.reviewed && (
                            <button
                              onClick={() => handleConfirmIdentity(item)}
                              disabled={busy}
                              className={`px-3 py-1.5 rounded-lg text-xs font-bold bg-accent-green/20 text-accent-green hover:bg-accent-green/30 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                              title="Conferma durevole: questa intestazione è di questo cliente, vale anche per le fatture future"
                            >
                              {busy ? '…' : 'Conferma identità (vale anche per le future)'}
                            </button>
                          )}
                          <button
                            onClick={() => handleAuditUnlink(item)}
                            disabled={busy}
                            className={`px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent-red/15 text-accent-red hover:bg-accent-red/25 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                          >
                            {busy ? '…' : 'Scollega'}
                          </button>
                          {item.can_assign_piva && (
                            <button
                              onClick={() => handleAuditAssignPiva(item)}
                              disabled={busy}
                              className={`px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent-teal/15 text-accent-teal hover:bg-accent-teal/25 transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''}`}
                            >
                              Assegna P.IVA al cliente
                            </button>
                          )}
                          <button
                            onClick={() => handleAuditToggleReviewed(item)}
                            disabled={busy}
                            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${busy ? 'opacity-50 cursor-not-allowed' : ''} ${
                              item.reviewed
                                ? 'bg-dark-surface text-txt-muted hover:text-txt-secondary'
                                : 'bg-dark-surface text-txt-muted hover:text-txt-secondary'
                            }`}
                            title="Verifica una tantum, solo questa fattura (non vale per le future)"
                          >
                            {item.reviewed ? 'Annulla verifica' : 'Segna solo questa (una volta)'}
                          </button>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            <label className="flex items-center justify-center gap-2 text-xs text-txt-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={includeReviewedAudit}
                onChange={(e) => setIncludeReviewedAudit(e.target.checked)}
                className="accent-accent-teal"
              />
              Mostra anche le fatture già verificate a mano
            </label>
          </div>
        ) : null}
      </div>

      {/* INTESTAZIONI ACCETTATE (bonifica durevole, Tier 2): il tratto
          d'identità del cliente. Le fatture con queste grafie sono verificate
          per costruzione, anche le future. Reversibile per riga. */}
      {data.accepted_names?.length > 0 && (
        <div className="sc-card overflow-hidden">
          <div className="sc-card-header">
            <h2 className="text-base font-bold text-txt-primary">
              Intestazioni accettate ({data.accepted_names.length})
            </h2>
          </div>
          <div className="p-5 space-y-3">
            <p className="text-xs text-txt-muted">
              Grafie che hai confermato appartenere a questo cliente: le fatture con queste
              intestazioni sono verificate — anche le future — senza dover ripetere il controllo.
            </p>
            <div className="space-y-2">
              {data.accepted_names.map(an => (
                <div
                  key={an.id}
                  className="flex items-center justify-between gap-3 rounded-lg border border-dark-border bg-dark-surface px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="text-sm text-txt-primary break-words">{an.note || an.name_normalized}</p>
                    <p className="text-xs text-txt-muted font-mono break-words">chiave: {an.name_normalized}</p>
                  </div>
                  <button
                    onClick={() => handleRemoveAcceptedName(an)}
                    className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-accent-red/10 text-accent-red hover:bg-accent-red/20 transition-colors shrink-0"
                    title="Rimuovi: le fatture con questa intestazione torneranno «da controllare»"
                  >
                    Rimuovi
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* FATTURE IN QUARANTENA SUGGERITE A QUESTO CLIENTE: senza questa
          sezione una fattura in attesa di conferma non compariva MAI sul
          profilo (caso Belfiore 655/2026) e l'operatore concludeva che il
          sistema non la conosceva. */}
      {pendingSuggestions.length > 0 && (
        <div className="sc-card overflow-hidden border-2 border-accent-amber/30">
          <div className="sc-card-header bg-accent-amber/5">
            <div>
              <h2 className="text-base font-bold text-accent-amber">
                In attesa di conferma ({pendingSuggestions.length})
              </h2>
              <p className="text-xs text-txt-muted mt-0.5">
                Fatture abbinate a questo cliente in via provvisoria: NON contano nei totali finché non le confermi.
              </p>
            </div>
          </div>

          {suggestionError && (
            <div className="mx-5 mt-4 p-3 rounded-lg text-sm bg-accent-red/10 text-accent-red border border-accent-red/20">
              {suggestionError}
            </div>
          )}

          <div className="divide-y divide-dark-border">
            {pendingSuggestions.map(sug => (
              <div key={sug.id} className="p-4 flex flex-wrap items-center gap-4">
                <div className="w-44 shrink-0">
                  <p className="text-sm font-medium text-txt-primary">{sug.invoice_number}</p>
                  <p className="text-sm text-txt-secondary">{formatCurrency(sug.amount_due)}</p>
                  <div className="flex items-center gap-1.5 mt-0.5">
                    <span className="text-xs text-txt-muted">
                      {sug.due_date ? `scad. ${formatDate(sug.due_date)}` : formatDate(sug.issue_date)}
                    </span>
                    <span className={`sc-badge ${
                      sug.source_platform === 'fatturapro' ? 'bg-accent-purple/15 text-accent-purple' : 'bg-accent-teal/15 text-accent-teal'
                    }`}>
                      {sug.source_platform === 'fatturapro' ? 'FPro' : 'F24'}
                    </span>
                  </div>
                </div>

                <div className="flex-1 min-w-[180px]">
                  <p className="text-xs font-semibold text-txt-label uppercase tracking-wider mb-1">Destinatario fattura</p>
                  <p className="text-sm text-txt-primary">{sug.customer_name_raw || '-'}</p>
                  <div className="flex items-center gap-2 mt-1 flex-wrap">
                    <span className="sc-badge bg-[rgba(148,163,184,0.15)] text-txt-secondary">
                      {sug.suggested_method} {formatScore(sug.suggested_score)}
                    </span>
                    {sug.verification && (
                      <VerifyBadge
                        v={sug.verification}
                        open={openVerify.has(`sug-${sug.id}`)}
                        onToggle={() => toggleVerify(`sug-${sug.id}`)}
                      />
                    )}
                    {isLowConfidence(sug) && (
                      <span className="sc-badge bg-[rgba(251,191,36,0.15)] text-accent-amber">bassa confidenza</span>
                    )}
                    {sug.status === 'paid' && (
                      <span className="badge-paid sc-badge">Pagata</span>
                    )}
                    {sug.status !== 'paid' && (sug.days_overdue || 0) > 0 && (
                      <span className="text-xs font-medium text-accent-red">+{sug.days_overdue}gg</span>
                    )}
                  </div>
                  {sug.verification && openVerify.has(`sug-${sug.id}`) && (
                    <div className="mt-2">
                      <VerifyDetail v={sug.verification} />
                    </div>
                  )}
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
                    onClick={() => handleRejectSuggestion(sug)}
                    disabled={suggestionActingId === sug.id}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-transparent hover:bg-accent-red/10 text-accent-red border border-accent-red/40 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Rifiuta
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

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
                <th className="px-3 py-3 text-left text-xs font-semibold text-txt-label uppercase tracking-wider">Verifica</th>
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
                <th className="px-3 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider" title="Solleciti ricevuti da QUESTA fattura (il soggetto è la fattura, non il cliente)">Solleciti (fattura)</th>
                <th className="px-3 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Stato</th>
                <th className="px-3 py-3 text-center text-xs font-semibold text-txt-label uppercase tracking-wider">Azioni</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-dark-border">
              {visibleInvoices.map(inv => (
                <React.Fragment key={inv.id}>
                <tr
                  className={`
                    ${inv.status === 'paid' ? 'bg-accent-green/5 opacity-60' : ''}
                    ${inv.days_overdue > 0 && inv.status !== 'paid' ? 'bg-accent-red/5' : ''}
                    ${inv.bounced_at && inv.status !== 'paid' ? 'bg-accent-red/15 ring-2 ring-inset ring-accent-red/60' : ''}
                    ${inv.in_incasso ? 'bg-accent-teal/5' : ''}
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
                      disabled={inv.status === 'paid' || !!inv.in_incasso}
                    />
                  </td>
                  <td className="px-3 py-3 text-sm font-medium text-txt-primary">{inv.invoice_number}</td>
                  <td className="px-3 py-3 text-sm">
                    {inv.verification ? (
                      <VerifyBadge
                        v={inv.verification}
                        open={openVerify.has(inv.id)}
                        onToggle={() => toggleVerify(inv.id)}
                        reviewed={reviewedRows.has(inv.id)}
                      />
                    ) : (
                      <span className="text-txt-muted">—</span>
                    )}
                  </td>
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
                  <td className="px-3 py-3 text-sm text-txt-secondary">
                    {formatDate(inv.due_date)}
                    {inv.due_date && inv.due_date_source !== 'real' && (
                      <span
                        className="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-dark-surface text-txt-muted align-middle"
                        title="Scadenza stimata (emissione + 30gg): non presente nel gestionale"
                      >
                        stimata
                      </span>
                    )}
                  </td>
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
                    {/* Solleciti PER-FATTURA (tabella di join, Fase 1): il
                        soggetto è la fattura — una scaduta nuova parte da 0
                        anche se il cliente è già al 2° su altre. */}
                    {inv.status === 'paid' ? (
                      <span className="text-txt-muted">—</span>
                    ) : (inv.sollecito_count || 0) === 0 ? (
                      <span className="text-txt-muted text-xs" title="Nessun sollecito su questa fattura">0</span>
                    ) : (
                      <span
                        className={`sc-badge ${(inv.sollecito_count || 0) >= 2 ? 'bg-accent-amber/15 text-accent-amber' : 'bg-accent-teal/15 text-accent-teal'}`}
                        title={inv.last_sollecito ? `Ultimo sollecito: ${formatDate(inv.last_sollecito)}` : ''}
                      >
                        {inv.sollecito_count} sollecit{inv.sollecito_count === 1 ? 'o' : 'i'}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-sm text-center">
                    <span className={`${INVOICE_STATUS_COLORS[inv.status] || 'bg-[rgba(148,163,184,0.15)] text-txt-muted'} sc-badge`}>
                      {inv.status === 'open' ? 'Aperto' : inv.status === 'paid' ? 'Pagato' : inv.status}
                    </span>
                    {/* Trasparenza incasso (sola lettura): chiude il dubbio
                        "è registrata?". Verde SOLO per la pagata (confermata);
                        muto per "da incassare" e per la conferma in corso. */}
                    {inv.status === 'paid' ? (
                      <div className="mt-1 text-[11px] text-accent-green/80">
                        Pagata{inv.paid_at ? ` · ${formatDate(inv.paid_at)}` : ''}
                      </div>
                    ) : inv.bounced_at ? (
                      <div className="mt-1">
                        <span
                          className="inline-block text-[10px] font-bold px-1.5 py-0.5 rounded bg-accent-red text-dark-bg"
                          title={`Assegno tornato insoluto il ${formatDate(inv.bounced_at)}${inv.bounced_note ? ' · ' + inv.bounced_note : ''}. La fattura è tornata scaduta e la pratica riaperta.`}
                        >
                          ⚠ ASSEGNO INSOLUTO · {formatDate(inv.bounced_at)}
                        </span>
                      </div>
                    ) : (!inv.payment_pending && inv.payment_pending_at && (inv.days_overdue || 0) > 0) ? (
                      <div className="mt-1">
                        <span
                          className="inline-block text-[10px] font-bold px-1.5 py-0.5 rounded bg-accent-amber/25 text-accent-amber"
                          title={inv.bounced_note || 'Pagata con assegno, poi riaperta su FatturaPro: verificare se l\'assegno è tornato insoluto'}
                        >
                          ⚠ RIAPERTA DOPO ASSEGNO · verificare insoluto
                        </span>
                      </div>
                    ) : inv.in_incasso ? (
                      <div className="mt-1">
                        <span
                          className={`inline-block text-[10px] px-1.5 py-0.5 rounded ${inv.pending_overdue ? 'bg-accent-amber/20 text-accent-amber' : 'bg-accent-teal/15 text-accent-teal'}`}
                          title={`Assegno registrato il ${formatDate(inv.payment_pending_at)}${inv.payment_pending_note ? ' · ' + inv.payment_pending_note : ''}. Fuori dai solleciti finché non è incassato (FatturaPro la vede ancora aperta).`}
                        >
                          In incasso · assegno{inv.payment_pending_expected ? ` · atteso ${formatDate(inv.payment_pending_expected)}` : ''}{inv.pending_overdue ? ' · OLTRE LA DATA' : ''}
                        </span>
                      </div>
                    ) : (inv.missing_streak || 0) >= 1 ? (
                      <div className="mt-1">
                        <span
                          className="inline-block text-[10px] px-1.5 py-0.5 rounded bg-[rgba(148,163,184,0.15)] text-txt-muted"
                          title="Sparita dalla lista «Da incassare» di FatturaPro: sta per essere marcata pagata. Usa «Aggiorna incassi adesso» per confermarla subito."
                        >
                          Conferma incasso in corso ({inv.missing_streak}/2)
                        </span>
                      </div>
                    ) : (
                      <div className="mt-1 text-[11px] text-txt-muted">
                        Da incassare{inv.updated_at ? ` · aggiornato ${formatDate(inv.updated_at)}` : ''}
                      </div>
                    )}
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
                    {/* Assegno in mano: azioni sulla riga della fattura */}
                    {inv.status !== 'paid' && !inv.in_incasso && ((inv.days_overdue || 0) > 0 || inv.bounced_at) && (
                      <button
                        onClick={() => setAssegnoForm(assegnoForm?.invoiceId === inv.id ? null : { invoiceId: inv.id, expected: '', note: '' })}
                        className="ml-1 px-2 py-1 bg-accent-teal/15 text-accent-teal rounded text-xs font-medium hover:bg-accent-teal/25 transition-colors"
                        title={inv.bounced_at ? 'Registra un NUOVO assegno (azzera l\'allarme insoluto)' : 'Pagata con assegno da incassare'}
                      >
                        {inv.bounced_at ? 'Nuovo assegno' : 'Assegno'}
                      </button>
                    )}
                    {!inv.in_incasso && !inv.payment_pending && inv.payment_pending_at && !inv.bounced_at && inv.status !== 'paid' && (inv.days_overdue || 0) > 0 && (
                      <button
                        onClick={() => markInsoluto(inv)}
                        className="ml-1 px-2 py-1 bg-accent-red/15 text-accent-red rounded text-xs font-bold hover:bg-accent-red/25 transition-colors"
                        title="Conferma: l'assegno è tornato insoluto"
                      >
                        Insoluto
                      </button>
                    )}
                    {inv.in_incasso && (
                      <>
                        <button
                          onClick={() => markInsoluto(inv)}
                          className="ml-1 px-2 py-1 bg-accent-red/15 text-accent-red rounded text-xs font-bold hover:bg-accent-red/25 transition-colors"
                          title="L'assegno è tornato indietro: la fattura torna scaduta SUBITO"
                        >
                          Insoluto
                        </button>
                        <button
                          onClick={() => cancelAssegno(inv)}
                          className="ml-1 px-2 py-1 bg-dark-surface text-txt-muted rounded text-xs hover:text-txt-primary transition-colors"
                          title="Annulla la registrazione (solo se fatta per errore)"
                        >
                          Annulla
                        </button>
                      </>
                    )}
                  </td>
                </tr>
                {assegnoForm?.invoiceId === inv.id && (
                  <tr key={`${inv.id}-assegno`} className="bg-dark-surface/40">
                    <td colSpan={11} className="px-3 pb-3">
                      <div className="flex items-end gap-3 flex-wrap pt-2">
                        <div className="text-xs text-txt-secondary">
                          Pagata con <strong className="text-txt-primary">assegno</strong> da incassare — Fatt. {inv.invoice_number} · {formatCurrency(inv.amount_due)}
                        </div>
                        <label className="text-xs text-txt-muted">
                          Incasso previsto
                          <input
                            type="date"
                            value={assegnoForm.expected}
                            onChange={e => setAssegnoForm({ ...assegnoForm, expected: e.target.value })}
                            className="ml-2 px-2 py-1 rounded bg-dark-bg border border-dark-border text-sm text-txt-primary"
                          />
                        </label>
                        <label className="text-xs text-txt-muted flex-1 min-w-[16rem]">
                          Nota
                          <input
                            type="text"
                            value={assegnoForm.note}
                            placeholder="es. assegno n. 123, verrà incassato il …"
                            onChange={e => setAssegnoForm({ ...assegnoForm, note: e.target.value })}
                            className="ml-2 w-full max-w-md px-2 py-1 rounded bg-dark-bg border border-dark-border text-sm text-txt-primary"
                          />
                        </label>
                        <button onClick={submitAssegno} className="sc-btn-primary text-xs">Registra assegno</button>
                        <button onClick={() => setAssegnoForm(null)} className="sc-btn-secondary text-xs">Chiudi</button>
                      </div>
                      <p className="mt-1.5 text-[11px] text-txt-muted">
                        La fattura esce dai solleciti e va in «In incasso (assegni)»; conta come recuperato dalla registrazione.
                        L'importo dovuto NON viene azzerato e FatturaPro non viene toccato: quando l'assegno è incassato la fattura diventa pagata da sola.
                      </p>
                    </td>
                  </tr>
                )}
                {openVerify.has(inv.id) && inv.verification && (
                  <tr key={`${inv.id}-verify`} className="bg-dark-surface/40">
                    <td colSpan={11} className="px-3 pb-3">
                      <VerifyDetail v={inv.verification} />
                      {/* Via d'uscita dal giallo: l'operatore che ha
                          controllato a mano lo registra qui (stesso
                          endpoint mark-reviewed dell'audit). */}
                      {inv.verification.level !== 'verified' && (
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          <button
                            onClick={() => handleRowToggleReviewed(inv)}
                            disabled={rowReviewActingId === inv.id}
                            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors ${
                              rowReviewActingId === inv.id ? 'opacity-50 cursor-not-allowed' : ''
                            } ${
                              reviewedRows.has(inv.id)
                                ? 'bg-dark-surface text-txt-muted hover:text-txt-secondary'
                                : 'bg-accent-green/15 text-accent-green hover:bg-accent-green/25'
                            }`}
                          >
                            {rowReviewActingId === inv.id
                              ? '…'
                              : reviewedRows.has(inv.id) ? 'Annulla verifica' : 'Segna verificato'}
                          </button>
                          <span className="text-xs text-txt-muted">
                            {reviewedRows.has(inv.id)
                              ? 'Verificata a mano: l\'avviso è silenziato.'
                              : 'Hai controllato a mano che la fattura è del cliente giusto? Segnala verificata.'}
                          </span>
                        </div>
                      )}
                    </td>
                  </tr>
                )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>

        {/* Action bar for selected invoices */}
        {selectedInvoices.size > 0 && (
          <div className="px-6 py-4 bg-accent-green/5 border-t border-accent-green/20">
            {/* Selettore di stadio su RIGA PROPRIA con slot riservato: appare
                solo con selezione mista, senza spostare i pulsanti. */}
            <div className="min-h-[1.75rem] mb-1 flex items-center">{stagePicker}</div>
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
                  disabled={activeGroup.length === 0}
                  title={activeGroup.length === 0 ? "Nessuna fattura da sollecitare: quelle selezionate sono già state sollecitate oggi" : ""}
                  className={`sc-btn-secondary text-sm font-bold transition-colors ${
                    copiedWhatsApp ? 'border-accent-green text-accent-green' : ''
                  }`}
                >
                  {copiedWhatsApp ? 'Copiato!' : 'Copia Messaggio'}
                </button>
                {whatsappNumber ? (
                  <button
                    onClick={handleWhatsAppSend}
                    disabled={activeGroup.length === 0}
                    title={activeGroup.length === 0 ? "Nessuna fattura da sollecitare: quelle selezionate sono già state sollecitate oggi" : ""}
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
              <div key={action.id} className={`relative ${action.cancelled ? 'opacity-50' : ''}`}>
                <div className={`absolute -left-[21px] top-1 w-3 h-3 rounded-full border-2 border-dark-card ${
                  action.cancelled ? 'bg-dark-border' : action.completed_at ? 'bg-accent-green' : 'bg-txt-muted'
                }`}></div>
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`text-sm font-medium text-txt-primary ${action.cancelled ? 'line-through' : ''}`}>
                    {ACTION_LABELS[action.action_type] || action.action_type}
                  </span>
                  {/* Data di ESECUZIONE per le azioni fatte, di pianificazione
                      per i todo: la data di creazione mostrata in passato
                      faceva sembrare i solleciti molto più vecchi del reale */}
                  <span className="text-xs text-txt-muted">
                    {action.completed_at
                      ? `eseguita il ${formatDate(action.completed_at)}`
                      : action.scheduled_date
                        ? `pianificata per ${formatDate(action.scheduled_date)}`
                        : formatDate(action.created_at)}
                  </span>
                  {(action.channel === 'whatsapp_copy' || action.channel === 'whatsapp_link') && (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-accent-green/15 text-accent-green">WhatsApp</span>
                  )}
                  {action.cancelled && (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-dark-surface text-txt-muted">annullata</span>
                  )}
                  {action.completed_at && action.outcome && (
                    <span className={`text-xs px-1.5 py-0.5 rounded ${OUTCOME_COLORS[action.outcome] || 'bg-accent-green/15 text-accent-green'}`}>
                      {OUTCOME_LABELS[action.outcome] || action.outcome}
                    </span>
                  )}
                  {action.completed_at && !action.outcome && (
                    <span className="text-xs bg-accent-green/15 text-accent-green px-1.5 py-0.5 rounded">completata</span>
                  )}
                  {!action.completed_at && !action.cancelled && (
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
                {action.scheduled_date && action.action_type !== 'note' && (
                  editingDateActionId === action.id ? (
                    <div className="flex items-center gap-2 mt-1">
                      <input
                        type="date"
                        value={editingDateValue}
                        onChange={(e) => setEditingDateValue(e.target.value)}
                        className="text-xs bg-dark-surface border border-dark-border rounded px-2 py-1 text-txt-primary"
                        autoFocus
                      />
                      <button
                        onClick={() => handleRescheduleAction(action.id, editingDateValue)}
                        disabled={!editingDateValue}
                        className="text-xs bg-accent-teal/20 text-accent-teal px-2 py-0.5 rounded hover:bg-accent-teal/30 disabled:opacity-40"
                      >
                        Salva
                      </button>
                      <button
                        onClick={() => { setEditingDateActionId(null); setEditingDateValue('') }}
                        className="text-xs text-txt-muted hover:text-txt-primary"
                      >
                        Annulla
                      </button>
                    </div>
                  ) : (
                    <p
                      className="text-xs text-accent-teal mt-0.5 cursor-pointer hover:underline"
                      onClick={() => {
                        setEditingDateActionId(action.id)
                        setEditingDateValue(action.scheduled_date?.split('T')[0] || '')
                      }}
                      title="Clicca per modificare la data"
                    >
                      Pianificata: {formatDate(action.scheduled_date)}
                    </p>
                  )
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
            <p className="text-xs text-accent-blue/60">{allUnpaid.length} fattur{allUnpaid.length === 1 ? 'a non pagata' : 'e non pagate'}</p>
            {(() => {
              const notOverdue = allUnpaid.filter(inv => (inv.days_overdue || 0) <= 0 && inv.due_date)
              if (notOverdue.length > 0) {
                const nearest = notOverdue.sort((a, b) => a.due_date.localeCompare(b.due_date))[0]
                return (
                  <p className="text-xs text-accent-teal mt-1">
                    Prossima scadenza: <span className="font-medium">{formatDate(nearest.due_date)}</span>
                    <span className="text-accent-teal/60"> (tra {Math.abs(nearest.days_overdue)}gg)</span>
                  </p>
                )
              }
              return null
            })()}
          </div>
          <div className="border border-accent-green/20 rounded-lg p-4 bg-accent-green/5">
            <p className="text-xs text-accent-green mb-1">Pagato</p>
            <p className="text-lg font-bold text-accent-green">{formatCurrency(totalPaid)}</p>
            <p className="text-xs text-accent-green/60">
              {paidInvoices.length} fattur{paidInvoices.length === 1 ? 'a pagata' : 'e pagate'}
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
                disabled={activeGroup.length === 0}
                title={activeGroup.length === 0 ? "Nessuna fattura da sollecitare: quelle selezionate sono già state sollecitate oggi" : ""}
                className={`sc-btn-secondary text-sm font-medium transition-colors ${
                  copiedWhatsApp ? 'border-accent-green text-accent-green' : ''
                }`}
              >
                {copiedWhatsApp ? 'Copiato!' : 'Copia Messaggio'}
              </button>
              {whatsappNumber && (
                <button
                  onClick={handleWhatsAppSend}
                  disabled={activeGroup.length === 0}
                  title={activeGroup.length === 0 ? "Nessuna fattura da sollecitare: quelle selezionate sono già state sollecitate oggi" : ""}
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
