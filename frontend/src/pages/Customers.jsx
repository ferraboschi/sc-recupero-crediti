import React, { useState, useEffect } from 'react'
import client from '../api/client'

export default function Customers() {
  const [customers, setCustomers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [skip, setSkip] = useState(0)
  const [total, setTotal] = useState(0)
  const [limit] = useState(50)
  const [search, setSearch] = useState('')
  const [excludedToggle, setExcludedToggle] = useState({})

  useEffect(() => {
    const fetchCustomers = async () => {
      try {
        setLoading(true)
        const params = { skip, limit }
        if (search) params.search = search

        const response = await client.get('/customers', { params })
        setCustomers(response.data.items)
        setTotal(response.data.total)

        // Initialize excluded toggle state
        const toggleState = {}
        response.data.items.forEach(c => {
          toggleState[c.id] = c.excluded
        })
        setExcludedToggle(toggleState)
      } catch (err) {
        setError('Errore nel caricamento dei clienti')
        console.error(err)
      } finally {
        setLoading(false)
      }
    }
    fetchCustomers()
  }, [skip, limit, search])

  const handleToggleExcluded = async (customerId, newValue) => {
    try {
      await client.put(`/customers/${customerId}/exclude`, null, {
        params: { exclude: newValue },
      })
      setExcludedToggle({
        ...excludedToggle,
        [customerId]: newValue,
      })
    } catch (err) {
      console.error(err)
    }
  }

  return (
    <div className="space-y-6">
      {/* Search Bar */}
      <div className="bg-white rounded-lg p-6 border border-slate-200">
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-2">Ricerca Clienti</label>
          <input
            type="text"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setSkip(0)
            }}
            placeholder="Ragione Sociale, P.IVA, Email..."
            className="w-full px-4 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-96">
            <svg className="animate-spin-slow w-8 h-8 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </div>
        ) : error ? (
          <div className="p-6 text-red-600">{error}</div>
        ) : customers.length === 0 ? (
          <div className="p-6 text-center text-slate-500">Nessun cliente trovato</div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-slate-50 border-b border-slate-200">
                  <tr>
                    <th className="px-6 py-3 text-left text-sm font-semibold text-slate-900">Ragione Sociale</th>
                    <th className="px-6 py-3 text-left text-sm font-semibold text-slate-900">P.IVA</th>
                    <th className="px-6 py-3 text-left text-sm font-semibold text-slate-900">Telefono</th>
                    <th className="px-6 py-3 text-left text-sm font-semibold text-slate-900">Email</th>
                    <th className="px-6 py-3 text-center text-sm font-semibold text-slate-900">Escluso</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {customers.map(customer => (
                    <tr key={customer.id} className="hover:bg-slate-50">
                      <td className="px-6 py-3 text-sm font-medium text-slate-900 cursor-pointer hover:text-blue-600">
                        {customer.ragione_sociale}
                      </td>
                      <td className="px-6 py-3 text-sm text-slate-600">
                        {customer.partita_iva}
                      </td>
                      <td className="px-6 py-3 text-sm text-slate-600">
                        {customer.phone || '-'}
                      </td>
                      <td className="px-6 py-3 text-sm text-slate-600">
                        {customer.email || '-'}
                      </td>
                      <td className="px-6 py-3 text-center">
                        <button
                          onClick={() => handleToggleExcluded(customer.id, !excludedToggle[customer.id])}
                          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                            excludedToggle[customer.id]
                              ? 'bg-red-600'
                              : 'bg-green-600'
                          }`}
                        >
                          <span
                            className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                              excludedToggle[customer.id]
                                ? 'translate-x-6'
                                : 'translate-x-1'
                            }`}
                          />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            <div className="px-6 py-4 border-t border-slate-200 flex items-center justify-between">
              <p className="text-sm text-slate-600">
                Mostrando {skip + 1} a {Math.min(skip + limit, total)} di {total} clienti
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => setSkip(Math.max(0, skip - limit))}
                  disabled={skip === 0}
                  className="px-4 py-2 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Precedente
                </button>
                <button
                  onClick={() => setSkip(skip + limit)}
                  disabled={skip + limit >= total}
                  className="px-4 py-2 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed"
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
