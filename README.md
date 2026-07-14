# SC Recupero Crediti

Sistema di recupero crediti per Sake Company con integrazione FatturaPro e Shopify.
I solleciti sono MANUALI: l'operatore usa "Copia Messaggio" dal dettaglio cliente e invia su
WhatsApp; il sistema registra automaticamente il sollecito sulla pratica del cliente.

**Frontend live**: [https://recupero.sakecompany.com](https://recupero.sakecompany.com)

## Architettura

Il sistema è diviso in due componenti:

- **Frontend** (React + Vite + Tailwind) — hostato su GitHub Pages con deploy automatico
- **Backend** (FastAPI + Supabase PostgreSQL) — deployato su Render (free tier)

### Come funziona

1. Lo scheduler giornaliero (08:30 CET) sincronizza fatture da FatturaPro (con P.IVA e
   scadenze reali dalle pagine di dettaglio) e clienti da Shopify
2. Il matching abbina automaticamente solo i casi SICURI (P.IVA univoca validata con checksum,
   nome normalizzato esatto e univoco); i casi dubbi (fuzzy, P.IVA ambigue) finiscono nella coda
   "Da confermare" della pagina Fatture, dove l'operatore conferma o rifiuta
3. Le fatture scadute di un cliente aprono una PRATICA (recovery case): i solleciti manuali via
   "Copia Messaggio" vengono registrati sulla pratica (numerazione e tono per ciclo di debito);
   a saldo completo la pratica si chiude e il ciclo successivo riparte da zero
4. La dashboard web mostra todo, calendario e stato di tutti i crediti in tempo reale

### Tabelle legacy

Le tabelle `messages` e `conversations` (pipeline di invio automatico Twilio, mai attivata in
produzione e rimossa a luglio 2026) restano nel database Supabase come archivio non mappato dal
codice: possono essere eliminate manualmente quando non servono più.

## Quick Start — Sviluppo Locale

```bash
# Backend (con SQLite locale)
cp .env.example .env
# Modifica .env con le credenziali
pip install -r requirements.txt
python -m uvicorn backend.main:app --reload

# Frontend (in un altro terminale)
cd frontend && npm install && npm run dev
```

L'app sarà su http://localhost:5173 (frontend) e http://localhost:8000 (API).

Per sviluppo locale il sistema usa SQLite automaticamente (nessun setup database necessario).

## Deploy Produzione — Supabase + Render

### 1. Supabase (Database PostgreSQL)

1. Crea un progetto su [supabase.com](https://supabase.com)
2. Vai su **Settings → Database → Connection string → URI**
3. Copia la connection string (formato: `postgresql://postgres.[ref]:[password]@...pooler.supabase.com:6543/postgres`)
4. Le tabelle vengono create automaticamente al primo avvio dell'app

### 2. Render (Backend API)

1. Vai su [render.com](https://render.com) e connetti il repo GitHub
2. Render rileva `render.yaml` e configura il servizio automaticamente
3. Aggiungi le variabili d'ambiente nel dashboard Render:
   - `DATABASE_URL` → connection string Supabase
   - `FATTURAPRO_API_KEY`, `FATTURAPRO_DOMAIN`
   - `SHOPIFY_STORE_URL`, `SHOPIFY_ACCESS_TOKEN`
   - `JWT_SECRET`, `AUTH_PASSWORD`, `COMPANY_PIVA`
   - `CORS_ORIGINS` → `https://recupero.sakecompany.com`
4. Il deploy è automatico ad ogni push su `main`

### 3. Frontend (GitHub Pages)

Il frontend si deploya automaticamente su push al branch `main` via GitHub Actions.
Imposta la variabile `VITE_API_BASE_URL` nei Settings → Variables del repo GitHub con l'URL del backend Render.

### Deploy alternativo con Docker

Per chi preferisce un VPS:

```bash
# Sul VPS, come root
git clone https://github.com/ferraboschi/sc-recupero-crediti.git /opt/sc-recupero-crediti
cd /opt/sc-recupero-crediti
cp .env.example .env
nano .env  # inserisci DATABASE_URL Supabase + altre credenziali
docker-compose up -d --build
```

Per setup completo con Nginx + SSL: `sudo bash deploy/setup.sh`

## Configurazione (.env)

| Variabile | Descrizione |
|-----------|-------------|
| `DATABASE_URL` | Connection string database (PostgreSQL Supabase o SQLite) |
| `FATTURAPRO_API_KEY` | Chiave API FatturaPro |
| `FATTURAPRO_DOMAIN` | Dominio FatturaPro (es. sakecompany.com) |
| `FATTURA24_API_KEY` | Chiave API Fattura24 (legacy) |
| `SHOPIFY_STORE_URL` | URL negozio Shopify |
| `SHOPIFY_ACCESS_TOKEN` | Token API Shopify |
| `COMPANY_PIVA` | P.IVA di Sake Company (blacklist scraping: senza, l'enrichment full-text resta disabilitato) |
| `JWT_SECRET` | Segreto JWT (obbligatoria) |
| `AUTH_PASSWORD` | Password di accesso (obbligatoria) |
| `CORS_ORIGINS` | Origini CORS consentite |

## API Endpoints

- `GET /api/health` — Health check e stato credenziali
- `GET /api/dashboard` — Statistiche dashboard
- `GET /api/positions` — Lista posizioni debitorie
- `GET /api/customers` — Lista clienti
- `GET /api/positions/suggestions` — Abbinamenti in quarantena da confermare
- `POST /api/recovery/customers/{id}/solleciti` — Registra un sollecito WhatsApp sulla pratica
- `GET /api/system/match-audit` — Audit abbinamenti fatture→clienti (warn/bad)
- `POST /api/sync/full` — Sincronizzazione completa (fatture → clienti → matching → auto-create → ordini → pratiche)

## Struttura Progetto

```
├── backend/
│   ├── api/            # Route FastAPI
│   ├── connectors/     # Client API (FatturaPro, Shopify)
│   ├── engine/         # Logica business
│   │   ├── normalizer.py      # Normalizzazione ragioni sociali
│   │   ├── piva.py            # Validazione P.IVA (checksum)
│   │   ├── matching.py        # Matching fatture-clienti + quarantena
│   │   └── cases.py           # Pratiche di recupero (lifecycle + backfill)
│   ├── config.py       # Configurazione da .env
│   ├── database.py     # SQLAlchemy ORM (PostgreSQL/SQLite)
│   ├── scheduler.py    # APScheduler (job giornaliero)
│   └── main.py         # Entry point FastAPI
├── frontend/
│   ├── src/
│   │   ├── pages/      # Dashboard, Attività, Fatture, Clienti, Sistema
│   │   ├── api/        # Client Axios
│   │   └── config.js   # URL backend configurabile
│   └── public/
│       ├── CNAME        # Custom domain GitHub Pages
│       └── 404.html     # SPA routing
├── tests/               # 167 test (pytest)
├── deploy/
│   ├── nginx.conf       # Configurazione Nginx
│   ├── setup.sh         # Script setup server (VPS alternativo)
│   └── backup.sh        # Script backup DB
├── .github/workflows/
│   ├── ci.yml           # CI: test + lint + build
│   └── pages.yml        # Deploy frontend GitHub Pages
├── render.yaml          # Deploy backend su Render
├── Dockerfile           # Backend Docker image
├── docker-compose.yml   # Orchestrazione Docker
└── .env.example         # Template variabili ambiente
```

## Test

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

167 test che coprono normalizer, phone_validator, matching, deduplicator e API.

## CI/CD

- **Push su main** → CI (pytest + flake8 + npm build) + deploy frontend su GitHub Pages + deploy backend su Render
- **Database** → Supabase PostgreSQL (gestito esternamente, nessun deploy necessario)

## Licenza

Proprietario — Sake Company
