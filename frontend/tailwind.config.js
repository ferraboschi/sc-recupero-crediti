/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,jsx}",
  ],
  theme: {
    extend: {
      colors: {
        /* SC Recupero – dark palette inspired by analisi.sakecompany.com */
        dark: {
          bg: '#0f1923',
          card: '#1a2733',
          cardHover: '#213040',
          border: '#263545',
          surface: '#15202b',
        },
        accent: {
          teal: '#2dd4bf',
          tealDark: '#14b8a6',
          cyan: '#22d3ee',
          amber: '#fbbf24',
          red: '#f87171',
          green: '#4ade80',
          purple: '#a78bfa',
          blue: '#60a5fa',
        },
        txt: {
          primary: '#e8e8e8',
          secondary: '#94a3b8',
          muted: '#64748b',
          label: '#78909c',
        },
        slate: {
          50: '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',
        },
      },
      fontFamily: {
        sans: ['-apple-system', 'system-ui', 'Segoe UI', 'Roboto', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
