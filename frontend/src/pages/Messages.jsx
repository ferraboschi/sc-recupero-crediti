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
  const [statusFilter, setStatusFilter] = useState('')
  const limit = 50

  useEffect(() => {
    const fetchMessages = async () => {
      try {
        setLoading(true)
        const params = { skip, limit }
        if (activeTab !== 'all') {
          params.status = activeTab === 'sent' ? 'sent' : activeTab
        }
        if (statusFilter) {
          params.status = statusFilter
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
  }, [skip, activeTab, statusFilter])

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

  const statuses = [
    { value: 'draft', label: 'Bozza', color: 'gray' },
    { value: 'approved', label: 'Approvato', color: 'blue' },
    { value: 'sent', label: 'Inviato', color: 'green' },
    { value: 'delivered', label: 'Consegnato', color: 'green' },
    { value: 'read', label: 'Letto', color: 'green' },
    { value: 'replied', label: 'Risposto', color: 'purple' },
    { value: 'failed', label: 'Non Riuscito', color: 'red' },
  ]

  const getStatusColor = (status) => {
    const statusObj = statuses.find(s => s.value === status)
    return statusObj?.color || 'gray'
  }

  const getStatusLabel = (status) => {
    const statusObj = statuses.find(s => s.value === status)
    return statusObj?.label || status
  }

  const getStatusBgClass = (status) => {
    const colorMap = {
      gray: 'bg-gray-100 text-gray-800',
      blue: 'bg-blue-100 text-blue-800',
      green: 'bg-green-100 text-green-800',
      purple: 'bg-purple-100 text-purple-800',
      red: 'bg-red-100 text-red-800',
    }
    return colorMap[getStatusColor(status)] || 'bg-gray-100 text-gray-800'
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

      {/* Status Filter */}
      <div className="bg-white rounded-lg p-4 border border-slate-200">
        <label className="block text-sm font-medium text-slate-700 mb-2">Filtra per Stato</label>
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value)
            setSkip(0)
          }}
          className="w-full md:w-64 px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">Tutti gli stati</option>
          {statuses.map(status => (
            <option key={status.value} value={status.value}>
              {status.label}
            </option>
          ))}
        </select>
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
            <div key={message.id} className="bg-white rounded-lg border border-slate-200 overflow-hidden hover:shadow-md transition-shadow">
              {/* Message Header */}
              <div className="p-6 border-b border-slate-100">
                <div className="flex items-start justify-between gap-4 mb-3">
                  <div className="flex items-start gap-4 flex-1">
                    <input
                      type="checkbox"
                      checked={selectedMessages.has(message.id)}
                      onChange={() => handleSelectMessage(message.id)}
                      className="w-4 h-4 text-blue-600 rounded mt-1"
                    />
                    <div className="flex-1">
                      <div className="flex items-center gap-2 flex-wrap mb-1">
                        <h3 className="font-semibold text-slate-900">
                          {message.customer_name || `Cliente #${message.customer_id}`}
                        </h3>
                        <span className={`px-2 py-1 rounded-full text-xs font-semibold ${getStatusBgClass(message.status)}`}>
                          {getStatusLabel(message.status)}
                        </span>
                        <span className="px-2 py-1 rounded-full text-xs font-medium bg-purple-100 text-purple-800">
                          Livello {message.escalation_level}
                        </span>
                      </div>
                      <div className="flex items-center gap-3 text-xs text-slate-500">
                        <span>Fatt. {message.invoice_number || message.invoice_id}</span>
                        {message.invoice_amount != null && (
                          <span className="font-medium text-red-600">
                            {new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(message.invoice_amount)}
                          </span>
                        )}
                        {message.invoice_due_date && (
                          <span>Scad. {new Date(message.invoice_due_date).toLocaleDateString('it-IT')}</span>
                        )}
                      </div>
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

              {/* Message Body */}
              <div className="p-6 bg-slate-50">
                <p className="text-slate-700 text-sm whitespace-pre-wrap mb-3">
                  {message.body}
                </p>
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>Creato: {new Date(message.created_at).toLocaleString('it-IT')}</span>
                  {message.sent_at && (
                    <span>Inviato: {new Date(message.sent_at).toLocaleString('it-IT')}</span>
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
