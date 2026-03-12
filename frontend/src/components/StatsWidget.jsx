import React from 'react'

export default function StatsWidget({ label, value, icon: Icon, trend, trendLabel, color = 'blue' }) {
  const colorClasses = {
    blue: 'bg-blue-50 text-blue-600 border-blue-200',
    green: 'bg-green-50 text-green-600 border-green-200',
    purple: 'bg-purple-50 text-purple-600 border-purple-200',
    orange: 'bg-orange-50 text-orange-600 border-orange-200',
    red: 'bg-red-50 text-red-600 border-red-200',
  }

  const iconColorClasses = {
    blue: 'bg-blue-100 text-blue-600',
    green: 'bg-green-100 text-green-600',
    purple: 'bg-purple-100 text-purple-600',
    orange: 'bg-orange-100 text-orange-600',
    red: 'bg-red-100 text-red-600',
  }

  return (
    <div className={`${colorClasses[color]} border rounded-lg p-6 flex items-start justify-between`}>
      <div className="flex-1">
        <p className="text-sm font-medium text-slate-600 mb-2">{label}</p>
        <p className="text-3xl font-bold text-slate-900">{value}</p>
        {trend !== undefined && (
          <p className={`text-sm mt-2 ${trend >= 0 ? 'text-green-600' : 'text-red-600'}`}>
            {trend >= 0 ? '↑' : '↓'} {Math.abs(trend)}% {trendLabel && `(${trendLabel})`}
          </p>
        )}
      </div>
      {Icon && (
        <div className={`${iconColorClasses[color]} rounded-lg p-3 ml-4`}>
          <Icon size={24} />
        </div>
      )}
    </div>
  )
}
