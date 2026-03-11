# SC Recupero Crediti

Sistema di gestione per il recupero crediti con integrazione Shopify, FatturaPro e Twilio WhatsApp.

Un'applicazione web completa per la gestione delle posizioni debitorie, comunicazione automatica ai clienti tramite WhatsApp e sincronizzazione con piattaforme di fatturazione e e-commerce.

## Caratteristiche

- **Gestione Posizioni**: Monitora e gestisci tutte le posizioni debitorie
- **Integrazione FatturaPro**: Sincronizzazione automatica delle fatture
- **Integrazione Shopify**: Estrazione dati clienti e ordini
- **WhatsApp Automation**: Invio messaggi automatici tramite Twilio
- **Scheduler**: Automazione dei processi ricorrenti
- **Dashboard**: Visualizzazione in tempo reale dello stato dei crediti

## Quick Start con Docker

### Prerequisiti

- Docker e Docker Compose installati
- File `.env` configurato con le credenziali API

### Setup

1. **Clona il repository e naviga nella cartella**
   ```bash
   cd sc-recupero-crediti
   ```

2. **Configura le variabili di ambiente**
   ```bash
   cp .env.example .env
   # Modifica .env con le tue credenziali
   ```

3. **Costruisci e avvia i container**
   ```bash
   docker-compose up --build
   ```

4. **Accedi all'applicazione**
   - Frontend: http://localhost:8000
   - API: http://localhost:8000/api
   - Health Check: http://localhost:8000/api/health

### Comandi Docker Utili

```bash
# Avvia i container
docker-compose up

# Avvia in background
docker-compose up -d

# Ferma i container
docker-compose down

# Visualizza i log
docker-compose logs -f app

# Ricostruisci l'immagine
docker-compose up --build
```

## Setup Manuale

### Prerequisiti

- Python 3.12+
- Node.js 20+
- SQLite3

### Backend Setup

1. **Crea un virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # su Windows: venv\Scripts\activate
   ```

2. **Installa le dipendenze Python**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configura le variabili di ambiente**
   ```bash
   cp .env.example .env
   # Modifica .env con le tue credenziali
   ```

4. **Avvia il backend**
   ```bash
   python -m uvicorn backend.main:app --reload
   ```

### Frontend Setup

1. **Naviga nella cartella frontend**
   ```bash
   cd frontend
   ```

2. **Installa le dipendenze Node.js**
   ```bash
   npm install
   ```

3. **Avvia il dev server**
   ```bash
   npm run dev
   ```

4. **Accedi all'applicazione**
   - Aprire http://localhost:5173

### Build di Produzione

```bash
# Frontend
cd frontend
npm run build

# Backend (usa il normale uvicorn con opzioni di produzione)
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## Configurazione

### Variabili di Ambiente (.env)

Vedi `.env.example` per il template completo. Le variabili principali sono:

#### FatturaPro
```
FATTURAPRO_API_URL=https://cloud.fatturapro.click
FATTURAPRO_API_KEY=tua_api_key
FATTURAPRO_DOMAIN=tuodominio.com
```

#### Shopify
```
SHOPIFY_STORE_URL=https://negozio.myshopify.com
SHOPIFY_ACCESS_TOKEN=token_shopify
SHOPIFY_API_VERSION=2026-01
SHOPIFY_PIVA_FIELD=address2
```

#### Twilio WhatsApp
```
TWILIO_ACCOUNT_SID=sid_twilio
TWILIO_AUTH_TOKEN=token_twilio
TWILIO_WHATSAPP_NUMBER_BUSINESS=whatsapp:+1234567890
TWILIO_WHATSAPP_NUMBER_RECOVERY=whatsapp:+0987654321
TWILIO_WEBHOOK_URL=https://tuoapp.com/api/webhooks/twilio
```

#### Applicazione
```
DATABASE_PATH=data/sc_recupero.db
TIMEZONE=Europe/Rome
SCHEDULER_HOUR=8
SCHEDULER_MINUTE=30
```

## API Endpoints

### Dashboard
- `GET /api/dashboard/stats` - Statistiche generali
- `GET /api/dashboard/overview` - Panoramica posizioni

### Posizioni
- `GET /api/positions` - Lista posizioni
- `POST /api/positions` - Crea posizione
- `GET /api/positions/{id}` - Dettagli posizione
- `PATCH /api/positions/{id}` - Aggiorna posizione

### Messaggi
- `GET /api/messages` - Lista messaggi
- `POST /api/messages` - Invia messaggio
- `GET /api/messages/{id}` - Dettagli messaggio

### Clienti
- `GET /api/customers` - Lista clienti
- `GET /api/customers/{id}` - Dettagli cliente

### Sincronizzazione
- `POST /api/sync/fatturapro` - Sincronizza da FatturaPro
- `POST /api/sync/shopify` - Sincronizza da Shopify
- `GET /api/sync/status` - Stato sincronizzazione

### Webhooks
- `POST /api/webhooks/twilio` - Webhook Twilio WhatsApp

### Health Check
- `GET /api/health` - Status applicazione e credenziali

## Architettura

```
sc-recupero-crediti/
├── backend/                    # Backend FastAPI
│   ├── api/                    # Route API
│   │   ├── dashboard.py
│   │   ├── positions.py
│   │   ├── messages.py
│   │   ├── customers.py
│   │   ├── sync.py
│   │   └── webhooks.py
│   ├── models/                 # Database models
│   ├── services/               # Logica business
│   ├── config.py              # Configurazione
│   ├── database.py            # Database setup
│   ├── scheduler.py           # Job scheduler
│   └── main.py               # Entry point FastAPI
├── frontend/                   # Frontend React
│   ├── src/
│   ├── package.json
│   └── vite.config.ts
├── Dockerfile                 # Multi-stage build
├── docker-compose.yml        # Orchestrazione
├── requirements.txt          # Python dependencies
├── .env.example             # Template variabili
└── README.md                # Questo file
```

## Workflow di Sviluppo

### 1. Aggiunta di una nuova feature

```bash
# Crea un branch per la feature
git checkout -b feature/nome-feature

# Sviluppa il codice
# ... modifiche ...

# Test locale con Docker
docker-compose up --build

# Commit e push
git add .
git commit -m "feat: descrizione feature"
git push origin feature/nome-feature
```

### 2. Aggiornamento dipendenze

```bash
# Backend
pip install --upgrade -r requirements.txt
pip freeze > requirements.txt

# Frontend
cd frontend
npm update
```

### 3. Build per produzione

```bash
# Docker build
docker build -t sc-recupero-crediti:latest .

# Tag per registry
docker tag sc-recupero-crediti:latest your-registry/sc-recupero-crediti:latest

# Push a registry
docker push your-registry/sc-recupero-crediti:latest
```

## Troubleshooting

### Il container non parte
```bash
# Visualizza i log
docker-compose logs -f app

# Verifica le variabili .env
cat .env
```

### Errore di connessione database
```bash
# Ricrea il container con volume pulito
docker-compose down -v
docker-compose up --build
```

### API non risponde
```bash
# Controlla health check
curl http://localhost:8000/api/health

# Riavvia il container
docker-compose restart app
```

## Logging e Monitoring

I log dell'applicazione sono salvati in `data/logs/` e possono essere consultati anche tramite Docker:

```bash
# Log in tempo reale
docker-compose logs -f app

# Log degli ultimi N righe
docker-compose logs --tail=100 app
```

## License

Proprietario - SAKE Company

## Support

Per domande o problemi, contatta il team di sviluppo.
