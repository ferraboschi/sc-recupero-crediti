# SC Recupero Crediti

Sistema automatizzato di recupero crediti per Sake Company con integrazione FatturaPro, Fattura24, Shopify e WhatsApp (Twilio).

**Frontend live**: [https://recupero.sakecompany.com](https://recupero.sakecompany.com)

## Architettura

Il sistema è diviso in due componenti:

- **Frontend** (React + Vite + Tailwind) — hostato su GitHub Pages con deploy automatico
- **Backend** (FastAPI + SQLite) — da deployare su VPS con Docker

### Come funziona

1. Lo scheduler giornaliero (08:30 CET) sincronizza fatture da FatturaPro/Fattura24 e clienti da Shopify
2. Il matching automatico collega fatture e clienti usando normalizzazione ragioni sociali italiane + fuzzy matching
3. Per ogni fattura scaduta, il sistema crea una posizione debitoria e invia messaggi WhatsApp in 4 livelli di escalation (7, 14, 21, 30 giorni)
4. La dashboard web mostra lo stato di tutti i crediti in tempo reale

## Quick Start — Sviluppo Locale

```bash
# Backend
cp .env.example .env
# Modifica .env con le credenziali
pip install -r requirements.txt
python -m uvicorn backend.main:app --reload

# Frontend (in un altro terminale)
cd frontend && npm install && npm run dev
```

L'app sarà su http://localhost:5173 (frontend) e http://localhost:8000 (API).

## Deploy Produzione con Docker

### Prerequisiti

- VPS con Ubuntu 22+ (minimo 1GB RAM)
- Dominio `api-recupero.sakecompany.com` puntato all'IP del VPS

### Setup rapido

```bash
# Sul VPS, come root
git clone https://github.com/ferraboschi/sc-recupero-crediti.git /opt/sc-recupero-crediti
cd /opt/sc-recupero-crediti
cp .env.example .env
nano .env  # inserisci le credenziali reali

# Avvia
docker-compose up -d --build

# Verifica
curl http://localhost:8000/api/health
```

### Setup completo con Nginx + SSL

```bash
sudo bash deploy/setup.sh
```

Lo script installa Docker, Nginx, configura SSL con Let's Encrypt e avvia il sistema.

## Configurazione (.env)

| Variabile | Descrizione |
|-----------|-------------|
| `FATTURAPRO_API_KEY` | Chiave API FatturaPro |
| `FATTURAPRO_DOMAIN` | Dominio FatturaPro (es. sakecompany.com) |
| `FATTURA24_API_KEY` | Chiave API Fattura24 (legacy) |
| `SHOPIFY_STORE_URL` | URL negozio Shopify |
| `SHOPIFY_ACCESS_TOKEN` | Token API Shopify |
| `TWILIO_ACCOUNT_SID` | Account SID Twilio |
| `TWILIO_AUTH_TOKEN` | Auth token Twilio |
| `TWILIO_WHATSAPP_NUMBER_BUSINESS` | Numero WhatsApp business |
| `TWILIO_WHATSAPP_NUMBER_RECOVERY` | Numero WhatsApp recupero crediti |
| `CORS_ORIGINS` | Origini CORS consentite (es. https://recupero.sakecompany.com) |

## API Endpoints

- `GET /api/health` — Health check e stato credenziali
- `GET /api/dashboard` — Statistiche dashboard
- `GET /api/positions` — Lista posizioni debitorie
- `GET /api/customers` — Lista clienti
- `GET /api/messages` — Lista messaggi WhatsApp
- `POST /api/sync/all` — Sincronizzazione completa (FatturaPro + Shopify + matching)
- `POST /api/webhooks/twilio` — Webhook Twilio per risposte WhatsApp

## Struttura Progetto

```
├── backend/
│   ├── api/            # Route FastAPI
│   ├── models/         # Modelli SQLAlchemy
│   ├── services/       # Logica business
│   │   ├── normalizer.py      # Normalizzazione ragioni sociali
│   │   ├── phone_validator.py # Validazione numeri per WhatsApp
│   │   ├── matching.py        # Matching fatture-clienti
│   │   └── deduplicator.py    # Deduplicazione fatture
│   ├── config.py       # Configurazione da .env
│   ├── scheduler.py    # APScheduler (job giornaliero)
│   └── main.py         # Entry point FastAPI
├── frontend/
│   ├── src/
│   │   ├── pages/      # Dashboard, Posizioni, Messaggi, Clienti
│   │   ├── api/        # Client Axios
│   │   └── config.js   # URL backend configurabile
│   └── public/
│       ├── CNAME        # Custom domain GitHub Pages
│       └── 404.html     # SPA routing
├── tests/               # 167 test (pytest)
├── deploy/
│   ├── nginx.conf       # Configurazione Nginx
│   ├── setup.sh         # Script setup server
│   └── backup.sh        # Script backup DB
├── .github/workflows/
│   ├── ci.yml           # CI: test + lint + build
│   └── pages.yml        # Deploy frontend GitHub Pages
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

- **Push su main** → CI (pytest + flake8 + npm build) + deploy frontend su GitHub Pages
- **Deploy backend** → manuale via Docker sul VPS (o automatico con secrets GitHub configurati)

## Licenza

Proprietario — Sake Company
