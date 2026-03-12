import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'

export default function Activity() {
  const navigate = useNavigate()
  const [report, setReport] = useState(null)
  const [activities, setActivities] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [activeTab, setActiveTab] = useState('attivi')

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        const [reportRes, activityRes] = await Promise.all([
          client.get('/recovery/report'),
          client.get('/dashboard'),
        ])
        setReport(reportRes.data)
        setActivities(activityRes.data.recent_activity || [])
      } catch (err) {
        setError('Errore nel caricamento dei dati')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])

  const tabs = [
    { id: 'attivi', label: 'Recuperi Attivi', icon: '🔥' },
    { id: 'saldato', label: 'Saldato', icon: '✅' },
    { id: 'attesa', label: 'In Attesa', icon: '⏳' },
    { id: 'avvocato', label: 'Avvocato', icon: '⚖️' },
    { id: 'scadenze', label: 'Prossime Scadenze', icon: '📅' },
    { id: 'log', label: 'Log Attività', icon: '📜' },
  ]

  const getActionLabel = (action) => {
    const labels = {
      first_contact: 'I Contatto',
      second_contact: 'II Contatto',
      lawyer: 'Avvocato',
      archive: 'Archivia',
      wait: 'Attendi',
      note: 'Nota',
      sync: 'Sincronizzazione',
      match: 'Abbinamento',
      escalation: 'Escalation',
      status_change: 'Cambio Stato',
      customer_excluded: 'Cliente Escluso',
      customer_included: 'Cliente Incluso',
      phone_updated: 'Telefono Aggiornato',
      csv_import: 'Import CSV',
    }
    return labels[action] || action
  }

  const getStatusBadge = (status) => {
    const styles = {
      first_contact: 'bg-yellow-100 text-yellow-800',
      second_contact: 'bg-orange-100 text-orange-800',
      lawyer: 'bg-red-100 text-red-800',
      waiting: 'bg-blue-100 text-blue-800',
      idle: 'bg-slate-100 text-slate-600',
      archived: 'bg-slate-200 text-slate-500',
    }
    const labels = {
      first_contact: 'I Contatto',
      second_contact: 'II Contatto',
      lawyer: 'Avvocato',
      waiting: 'In Attesa',
      idle: 'Da Avviare',
      archived: 'Archiviato',
    }
    return (
      <span className={`px-2 py-1 rounded-full text-xs font-medium ${styles[status] || 'bg-slate-100 text-slate-600'}`}>
        {labels[status] || status}
      </span>
    )
  }

  const formatCurrency = (val) => {
    return new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(val || 0)
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

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-red-600">
        {error}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Summary Cards */}
      {report?.summary && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('attivi')}>
            <div className="text-2xl font-bold text-orange-600">{report.summary.active_count}</div>
            <div className="text-xs text-slate-500 mt-1">Recuperi Attivi</div>
            <div className="text-sm font-medium text-slate-700 mt-1">{formatCurrency(report.summary.active_total_due)}</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('saldato')}>
            <div className="text-2xl font-bold text-green-600">{report.summary.paid_count}</div>
            <div className="text-xs text-slate-500 mt-1">Saldato</div>
            <div className="text-sm font-medium text-slate-700 mt-1">{formatCurrency(report.summary.paid_total)}</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('attesa')}>
            <div className="text-2xl font-bold text-blue-600">{report.summary.waiting_count}</div>
            <div className="text-xs text-slate-500 mt-1">In Attesa</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('avvocato')}>
            <div className="text-2xl font-bold text-red-600">{report.summary.lawyer_count}</div>
            <div className="text-xs text-slate-500 mt-1">Avvocato</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('scadenze')}>
            <div className="text-2xl font-bold text-purple-600">{report.summary.upcoming_actions_count}</div>
            <div className="text-xs text-slate-500 mt-1">Prossime Scadenze</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-slate-200 cursor-pointer hover:shadow-md transition-shadow" onClick={() => setActiveTab('log')}>
            <div className="text-2xl font-bold text-slate-600">{activities.length}</div>
            <div className="text-xs text-slate-500 mt-1">Log Recenti</div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="bg-white rounded-lg border border-slate-200">
        <div className="flex border-b border-slate-200 overflow-x-auto">
          {tabs.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-3 text-sm font-medium whitespace-nowrap border-b-2 transition-colors ${
                activeTab === tab.id
                  ? 'border-blue-600 text-blue-600'
                  : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
              }`}
            >
              <span>{tab.icon}</span>
              {tab.label}
            </button>
          ))}
        </div>

        <div className="p-6">
          {/* Recuperi Attivi */}
          {activeTab === 'attivi' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Recuperi Attivi ({report?.recuperi_attivi?.length || 0})</h3>
              {report?.recuperi_attivi?.length === 0 ? (
                <div className="bg-blue-50 border border-blue-200 rounded-lg p-6 text-center">
                  <div className="text-3xl mb-3">🚀</div>
                  <h4 className="font-semibold text-blue-800 mb-2">Nessun recupero attivo</h4>
                  <p className="text-blue-700 text-sm mb-3">Per avviare il flusso di recupero, vai nella pagina <strong>Clienti</strong> e seleziona un cliente con fatture scadute.</p>
                  <button
                    onClick={() => navigate('/customers')}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
                  >
                    Vai ai Clienti
                  </button>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600">P.IVA</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Stato</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-right">Dovuto</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-center">Fatture</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Prossima Azione</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.recuperi_attivi.map(c => (
                        <tr
                          key={c.id}
                          className="hover:bg-slate-50 cursor-pointer"
                          onClick={() => navigate(`/customers/${c.id}`)}
                        >
                          <td className="px-4 py-3 font-medium text-slate-900">{c.ragione_sociale}</td>
                          <td className="px-4 py-3 text-slate-600">{c.partita_iva || '-'}</td>
                          <td className="px-4 py-3">{getStatusBadge(c.recovery_status)}</td>
                          <td className="px-4 py-3 text-right font-medium text-red-600">{formatCurrency(c.total_due)}</td>
                          <td className="px-4 py-3 text-center">{c.invoice_count}</td>
                          <td className="px-4 py-3 text-slate-600">
                            {c.next_action_date ? new Date(c.next_action_date).toLocaleDateString('it-IT') : '-'}
                            {c.next_action_type && <span className="ml-2 text-xs text-slate-400">({getActionLabel(c.next_action_type)})</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Saldato */}
          {activeTab === 'saldato' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Fatture Saldate ({report?.saldato?.length || 0})</h3>
              {report?.saldato?.length === 0 ? (
                <p className="text-slate-500">Nessuna fattura saldata</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">N. Fattura</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-right">Importo</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Piattaforma</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.saldato.map(inv => (
                        <tr key={inv.id} className="hover:bg-slate-50 cursor-pointer" onClick={() => inv.customer_id && navigate(`/customers/${inv.customer_id}`)}>
                          <td className="px-4 py-3 font-medium text-slate-900">{inv.invoice_number}</td>
                          <td className="px-4 py-3 text-slate-600">{inv.customer_name || '-'}</td>
                          <td className="px-4 py-3 text-right font-medium text-green-600">{formatCurrency(inv.amount)}</td>
                          <td className="px-4 py-3">
                            <span className={`px-2 py-1 rounded-full text-xs font-medium ${inv.source_platform === 'fatturapro' ? 'bg-blue-100 text-blue-700' : 'bg-purple-100 text-purple-700'}`}>
                              {inv.source_platform === 'fatturapro' ? 'FatturaPro' : 'Fattura24'}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* In Attesa */}
          {activeTab === 'attesa' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">In Attesa ({report?.in_attesa?.length || 0})</h3>
              {report?.in_attesa?.length === 0 ? (
                <p className="text-slate-500">Nessun cliente in attesa</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600">P.IVA</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-right">Dovuto</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-center">Fatture</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Riprendi Il</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.in_attesa.map(c => (
                        <tr key={c.id} className="hover:bg-slate-50 cursor-pointer" onClick={() => navigate(`/customers/${c.id}`)}>
                          <td className="px-4 py-3 font-medium text-slate-900">{c.ragione_sociale}</td>
                          <td className="px-4 py-3 text-slate-600">{c.partita_iva || '-'}</td>
                          <td className="px-4 py-3 text-right font-medium text-orange-600">{formatCurrency(c.total_due)}</td>
                          <td className="px-4 py-3 text-center">{c.invoice_count}</td>
                          <td className="px-4 py-3 text-slate-600">{c.next_action_date ? new Date(c.next_action_date).toLocaleDateString('it-IT') : '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Avvocato */}
          {activeTab === 'avvocato' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Passati ad Avvocato ({report?.avvocato?.length || 0})</h3>
              {report?.avvocato?.length === 0 ? (
                <p className="text-slate-500">Nessun caso in mano all'avvocato</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600">P.IVA</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-right">Dovuto</th>
                        <th className="px-4 py-3 font-medium text-slate-600 text-center">Fatture</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Follow-up</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.avvocato.map(c => (
                        <tr key={c.id} className="hover:bg-slate-50 cursor-pointer" onClick={() => navigate(`/customers/${c.id}`)}>
                          <td className="px-4 py-3 font-medium text-slate-900">{c.ragione_sociale}</td>
                          <td className="px-4 py-3 text-slate-600">{c.partita_iva || '-'}</td>
                          <td className="px-4 py-3 text-right font-medium text-red-600">{formatCurrency(c.total_due)}</td>
                          <td className="px-4 py-3 text-center">{c.invoice_count}</td>
                          <td className="px-4 py-3 text-slate-600">{c.next_action_date ? new Date(c.next_action_date).toLocaleDateString('it-IT') : '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Prossime Scadenze */}
          {activeTab === 'scadenze' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Prossime Scadenze ({report?.prossime_scadenze?.length || 0})</h3>
              {report?.prossime_scadenze?.length === 0 ? (
                <p className="text-slate-500">Nessuna scadenza nei prossimi 30 giorni</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-slate-50 text-left">
                        <th className="px-4 py-3 font-medium text-slate-600">Data</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Cliente</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Tipo Azione</th>
                        <th className="px-4 py-3 font-medium text-slate-600">Note</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {report.prossime_scadenze.map((a, idx) => (
                        <tr key={a.id || idx} className="hover:bg-slate-50 cursor-pointer" onClick={() => navigate(`/customers/${a.customer_id}`)}>
                          <td className="px-4 py-3 font-medium text-slate-900">
                            {new Date(a.scheduled_date).toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' })}
                          </td>
                          <td className="px-4 py-3 text-slate-600">{a.customer_name}</td>
                          <td className="px-4 py-3">{getStatusBadge(a.action_type)}</td>
                          <td className="px-4 py-3 text-slate-500 text-xs max-w-xs truncate">{a.notes || '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Log Attività */}
          {activeTab === 'log' && (
            <div>
              <h3 className="text-lg font-semibold text-slate-800 mb-4">Log Attività Recenti</h3>
              {activities.length === 0 ? (
                <p className="text-slate-500">Nessuna attività recente</p>
              ) : (
                <div className="space-y-3">
                  {activities.map((activity, index) => (
                    <div key={activity.id || index} className="flex items-start gap-3 bg-slate-50 rounded-lg p-3 border border-slate-200">
                      <div className="text-lg">
                        {{
                          sync: '🔄', match: '🔗', escalation: '📈', status_change: '✏️',
                          message_sent: '📤', customer_excluded: '🚫', customer_included: '✅',
                          phone_updated: '📱', csv_import: '📥',
                        }[activity.action] || '📋'}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium text-sm text-slate-800">{getActionLabel(activity.action)}</span>
                          <span className="text-xs text-slate-500 whitespace-nowrap">
                            {new Date(activity.timestamp).toLocaleString('it-IT')}
                          </span>
                        </div>
                        {activity.details && (
                          <details className="mt-1 cursor-pointer">
                            <summary className="text-xs text-slate-500">Dettagli</summary>
                            <pre className="mt-1 text-xs overflow-auto bg-white p-2 rounded border border-slate-200 max-h-32">
                              {JSON.stringify(activity.details, null, 2)}
                            </pre>
                          </details>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
