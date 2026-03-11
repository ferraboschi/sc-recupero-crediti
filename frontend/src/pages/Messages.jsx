import React, { useState, useEffect } from 'react'
import client from '../api/client'

export default function Messages() {
  const [messages, setMessages] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [activeTab, setActiveTab] = useState('draft')
  const [selectedMessages, setSelectedMessages] = useState(new Set())
  const [actionLoading, setActionLoading] = useState(false)
  const [skip, setSkip] = useState(0)
  const [total, setTotal] = useState(0)
  const limit = 50

  useEffect(() => {
    const fetchMessages = async () => {
      try {
        setLoading(true)
        const params = { skip, limit }
        if (activeTab !== 'all') {
          params.status = activeTab === 'sent' ? 'sent' : activeTab
        }
        const response = await client.get('/messages', { params })
        setMessages(response.data.items)
        setTotal(response.data.total)
      } catch (err) {
        setError('Errore nel caricamento dei messaggi')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchMessages()
  }, [skip, activeTab])

  const handleSelectAll = () => {
    if (selectedMessages.size === messages.length) {
      setSelectedMessages(new Set())
    } else {
      setSelectedMessages(new Set(messages.map(m => m.id)))
    }
  }

  const handleSelectMessage = (id) => {
    const newSelected = new Set(selectedMessages)
    if (newSelected.has(id)) {
      newSelected.delete(id)
    } else {
      newSelected.add(id)
    }
    setSelectedMessages(newSelected)
  }

  const handleBulkApprove = async () => {
    if (selectedMessages.size === 0) return
    try {
      setActionLoading(true)
      await client.post('/messages/bulk-approve', {
        message_ids: Array.from(selectedMessages),
      })
      setSelectedMessages(new Set())
      // Refresh messages
      setSkip(0)
    } catch (err) {
      setError('Errore nell\'approvazione dei messaggi')
      console.error(err)
    } finally {
      setActionLoading(false)
    }
  }

  const handleBulkSend = async () => {
    if (selectedMessages.size === 0) return
    try {
      setActionLoading(true)
      await client.post('/messages/bulk-send', {
        message_ids: Array.from(selectedMessages),
      })
      setSelectedMessages(new Set())
      // Refresh messages
      setSkip(0)
    } catch (err) {
      setError('Errore nell\'invio dei messaggi')
      console.error(err)
    } finally {
      setActionLoading(false)
    }
  }

  const handleApproveMessage = async (messageId) => {
    try {
      await client.post(`/messages/${messageId}/approve`)
      setSkip(0)
    } catch (err) {
      console.error(err)
    }
  }

  const handleSendMessage = async (messageId) => {
    try {
      await client.post(`/messages/${messageId}/send`)
      setSkip(0)
    } catch (err) {
      console.error(err)
    }
  }

  const tabs = [
    { value: 'draft', label: 'Bozze' },
    { value: 'approved', label: 'Approvati' },
    { value: 'sent', label: 'Inviati' },
  ]

  const getStatusColor = (status) => {
    const colors = {
      draft: 'gray',
      approved: 'blue',
      sent: 'green',
      delivered: 'green',
      read: 'green',
      replied: 'purple',
    }
    return colors[status] || 'gray'
  }

  const getStatusLabel = (status) => {
    const labels = {
      draft: 'Bozza',
      approved: 'Approvato',
      sent: 'Inviato',
      delivered: 'Consegnato',
      read: 'Letto',
      replied: 'Risposto',
    }
    return labels[status] || status
  }

  return (
    <div className="space-y-6">
      {/* Tabs */}
      <div className="border-b border-slate-200">
        <div className="flex gap-8">
          {tabs.map(tab => (
            <button
              key={tab.value}
              onClick={() => {
                setActiveTab(tab.value)
                setSkip(0)
                setSelectedMessages(new Set())
              }}
              className={`px-4 py-3 font-medium border-b-2 transition-colors ${
                activeTab === tab.value
                  ? 'text-blue-600 border-blue-600'
                  : 'text-slate-600 border-transparent hover:text-slate-900'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Bulk Actions */}
      {(activeTab === 'draft' || activeTab === 'approved') && (
        <div className="bg-slate-50 rounded-lg p-4 flex items-center justify-between border border-slate-200">
          <div className="flex items-center gap-4">
            <input
              type="checkbox"
              checked={selectedMessages.size > 0 && selectedMessages.size === messages.length}
              onChange={handleSelectAll}
              className="w-4 h-4 text-blue-600 rounded"
            />
            <span className="text-sm text-slate-600">
              {selectedMessages.size > 0 ? `${selectedMessages.size} selezionati` : 'Seleziona tutto'}
            </span>
          </div>
          {selectedMessages.size > 0 && (
            <div className="flex gap-2">
              {activeTab === 'draft' && (
                <button
                  onClick={handleBulkApprove}
                  disabled={actionLoading}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
                >
                  Approva Selezionati
                </button>
              )}
              {(activeTab === 'draft' || activeTab === 'approved') && (
                <button
                  onClick={handleBulkSend}
                  disabled={actionLoading}
                  className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm font-medium hover:bg-green-700 disabled:opacity-50"
                >
                  Invia Selezionati
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* Messages */}
      <div className="space-y-4">
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
        ) : messages.length === 0 ? (
          <div className="text-center py-12 text-slate-500">
            Nessun messaggio trovato
          </div>
        ) : (
          messages.map(message => (
            <div key={message.id} className="bg-white rounded-lg border border-slate-200 p-6 hover:shadow-md transition-shadow">
              <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-4 flex-1">
                  <input
                    type="checkbox"
                    checked={selectedMessages.has(message.id)}
                    onChange={() => handleSelectMessage(message.id)}
                    className="w-4 h-4 text-blue-600 rounded mt-1"
                  />
                  <div className="flex-1">
                    <div className="flex items-center gap-3 mb-2">
                      <h3 className="font-semibold text-slate-900">Fattura: {message.id}</h3>
                      <span className={`badge-${message.status} px-3 py-1 rounded-full text-xs font-medium`}>
                        {getStatusLabel(message.status)}
                      </span>
                      <span className="px-3 py-1 rounded-full text-xs font-medium bg-purple-100 text-purple-800">
                        Livello {message.escalation_level}
                      </span>
                    </div>
                    <p className="text-slate-600 text-sm mb-2 line-clamp-2">
                      {message.body}
                    </p>
                    <p className="text-xs text-slate-500">
                      Creato: {new Date(message.created_at).toLocaleString('it-IT')}
                    </p>
                  </div>
                </div>

                {/* Action Buttons */}
                <div className="flex gap-2">
                  {message.status === 'draft' && (
                    <>
                      <button
                        onClick={() => handleApproveMessage(message.id)}
                        className="px-3 py-1 bg-blue-600 text-white rounded text-sm font-medium hover:bg-blue-700"
                      >
                        Approva
                      </button>
                      <button
                        onClick={() => handleSendMessage(message.id)}
                        className="px-3 py-1 bg-green-600 text-white rounded text-sm font-medium hover:bg-green-700"
                      >
                        Invia
                      </button>
                    </>
                  )}
                  {message.status === 'approved' && (
                    <button
                      onClick={() => handleSendMessage(message.id)}
                      className="px-3 py-1 bg-green-600 text-white rounded text-sm font-medium hover:bg-green-700"
                    >
                      Invia
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Pagination */}
      {!loading && messages.length > 0 && (
        <div className="flex items-center justify-between p-4 bg-white rounded-lg border border-slate-200">
          <p className="text-sm text-slate-600">
            Mostrando {skip + 1} a {Math.min(skip + limit, total)} di {total} messaggi
          </p>
          <div className="flex gap-2">
            <button
              onClick={() => setSkip(Math.max(0, skip - limit))}
              disabled={skip === 0}
              className="px-4 py-2 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              Precedente
            </button>
            <button
              onClick={() => setSkip(skip + limit)}
              disabled={skip + limit >= total}
              className="px-4 py-2 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              Successivo
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
