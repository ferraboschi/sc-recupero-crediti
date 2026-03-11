import React, { useState, useEffect } from 'react'
import client from '../api/client'

export default function Activity() {
  const [activities, setActivities] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [actionFilter, setActionFilter] = useState('')

  useEffect(() => {
    const fetchActivity = async () => {
      try {
        setLoading(true)
        const response = await client.get('/dashboard')
        setActivities(response.data.recent_activity || [])
      } catch (err) {
        setError('Errore nel caricamento del log attività')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchActivity()
  }, [])

  // Filter activities by action type
  const filteredActivities = actionFilter
    ? activities.filter(a => a.action === actionFilter)
    : activities

  // Get unique action types
  const actionTypes = [...new Set(activities.map(a => a.action))]

  // Get action label
  const getActionLabel = (action) => {
    const labels = {
      sync: 'Sincronizzazione',
      match: 'Abbinamento',
      escalation: 'Escalation',
      status_change: 'Cambio Stato',
      message_sent: 'Messaggio Inviato',
      customer_excluded: 'Cliente Escluso',
      customer_included: 'Cliente Incluso',
      phone_updated: 'Telefono Aggiornato',
    }
    return labels[action] || action
  }

  // Get action icon
  const getActionIcon = (action) => {
    const icons = {
      sync: '🔄',
      match: '🔗',
      escalation: '📈',
      status_change: '✏️',
      message_sent: '📤',
      customer_excluded: '🚫',
      customer_included: '✅',
      phone_updated: '📱',
    }
    return icons[action] || '📋'
  }

  // Get entity type label
  const getEntityTypeLabel = (type) => {
    const labels = {
      invoice: 'Fattura',
      customer: 'Cliente',
      message: 'Messaggio',
    }
    return labels[type] || type
  }

  return (
    <div className="space-y-6">
      {/* Filter */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <label className="block text-sm font-medium text-slate-700 mb-2">Filtra per Azione</label>
        <select
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">Tutte le azioni</option>
          {actionTypes.map(action => (
            <option key={action} value={action}>
              {getActionLabel(action)}
            </option>
          ))}
        </select>
      </div>

      {/* Timeline */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        {loading ? (
          <div className="flex items-center justify-center h-96">
            <svg className="animate-spin-slow w-8 h-8 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </div>
        ) : error ? (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-red-600">
            {error}
          </div>
        ) : filteredActivities.length === 0 ? (
          <div className="text-center py-12 text-slate-500">
            Nessuna attività trovata
          </div>
        ) : (
          <div className="space-y-6">
            {filteredActivities.map((activity, index) => (
              <div key={activity.id || index} className="flex gap-4">
                {/* Timeline line */}
                <div className="flex flex-col items-center">
                  <div className="w-10 h-10 rounded-full bg-blue-100 text-blue-600 flex items-center justify-center text-lg font-bold">
                    {getActionIcon(activity.action)}
                  </div>
                  {index < filteredActivities.length - 1 && (
                    <div className="w-0.5 h-12 bg-slate-200 mt-2"></div>
                  )}
                </div>

                {/* Content */}
                <div className="flex-1 pb-6">
                  <div className="bg-slate-50 rounded-lg p-4 border border-slate-200">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1">
                        <h3 className="font-semibold text-slate-900">
                          {getActionLabel(activity.action)}
                        </h3>
                        {activity.entity_type && (
                          <p className="text-xs text-slate-600 mt-1">
                            Entità: <span className="font-medium">{getEntityTypeLabel(activity.entity_type)}</span>
                          </p>
                        )}
                        {activity.details && (
                          <div className="mt-3 text-sm text-slate-700 bg-white rounded p-2 border border-slate-200">
                            <details className="cursor-pointer">
                              <summary className="font-medium">Dettagli</summary>
                              <pre className="mt-2 text-xs overflow-auto bg-slate-50 p-2 rounded">
                                {JSON.stringify(activity.details, null, 2)}
                              </pre>
                            </details>
                          </div>
                        )}
                      </div>
                      <div className="text-right">
                        <p className="text-xs text-slate-600 whitespace-nowrap">
                          {new Date(activity.timestamp).toLocaleString('it-IT')}
                        </p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
