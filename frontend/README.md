# SC Recupero Crediti - Frontend

A professional React-based dashboard for Italian debt recovery management system.

## Project Structure

```
frontend/
├── index.html                 # Vite entry HTML
├── package.json              # Dependencies and scripts
├── vite.config.js            # Vite configuration with API proxy
├── tailwind.config.js        # Tailwind CSS configuration
└── src/
    ├── main.jsx              # React entry point with Router
    ├── App.jsx               # Main layout with sidebar and navigation
    ├── index.css             # Tailwind imports and custom styles
    ├── api/
    │   └── client.js         # Axios client with interceptors
    ├── components/
    │   ├── StatsWidget.jsx   # Reusable stat card component
    │   └── SyncButton.jsx    # Data sync trigger button
    └── pages/
        ├── Dashboard.jsx     # Overview with charts and stats
        ├── Positions.jsx     # Invoice positions management
        ├── Messages.jsx      # Message queue with bulk actions
        ├── Customers.jsx     # Customer management
        └── Activity.jsx      # Activity timeline log
```

## Features

### Dashboard
- Key metrics cards: Total Credits, Open Positions, Messages, Recent Updates
- Status distribution pie chart
- Escalation level bar chart
- Recent activity timeline

### Positions
- Advanced filtering (status, escalation level, amount, search)
- Paginated table with 50 items per page
- Color-coded status badges
- Currency formatting in Italian locale

### Messages
- Three-tab interface: Drafts, Approved, Sent
- Individual and bulk actions
- Approval and sending workflow
- Escalation level display

### Customers
- Search functionality
- Exclusion toggle for each customer
- Contact information display
- Pagination support

### Activity
- Timeline view of all system actions
- Filterable by action type
- Expandable detail sections
- Timestamp display with Italian locale

## Setup

```bash
# Install dependencies
npm install

# Development server (with API proxy to localhost:8000)
npm run dev

# Production build
npm run build

# Preview production build
npm run preview
```

## API Integration

The frontend connects to a FastAPI backend at `/api`:

- `GET /api/dashboard` - Overview statistics
- `GET /api/positions` - Position list with filters
- `GET /api/messages` - Message queue
- `POST /api/messages/{id}/approve` - Approve message
- `POST /api/messages/{id}/send` - Send message
- `POST /api/messages/bulk-approve` - Bulk approve
- `POST /api/messages/bulk-send` - Bulk send
- `GET /api/customers` - Customer list
- `PUT /api/customers/{id}/exclude` - Toggle exclusion
- `POST /api/sync/full` - Full data synchronization

## Styling

- Built with Tailwind CSS
- Professional B2B financial tool design
- Blue and gray color scheme
- Responsive layouts
- Dark sidebar with light main content

## Internationalization

All labels and text are in Italian:
- Dashboard → Dashboard
- Positions → Posizioni
- Messages → Messaggi
- Customers → Clienti
- Activity → Attività

## Technologies

- **React 18.3** - UI framework
- **React Router 6** - Client-side routing
- **Axios** - HTTP client with interceptors
- **Recharts** - Data visualization
- **Vite** - Build tool with fast refresh
- **Tailwind CSS** - Utility-first CSS framework

## Development Notes

- API requests use axios client with error interceptors
- Loading states and error handling on all data pages
- Currency formatting uses Italian locale (EUR)
- Dates formatted as dd/mm/yyyy Italian format
- Status colors: open (blue), contacted (amber), promised (purple), paid (green), disputed (red), escalated (orange)
