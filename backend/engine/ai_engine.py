"""AI Engine — Claude-powered message generation and response classification.

Uses Anthropic Claude API to:
1. Generate short, professional recovery messages (WhatsApp)
2. Classify incoming replies with nuance beyond keyword matching
3. Draft contextual auto-replies
4. Detect cases that need human escalation
"""

import os
import logging
import json
from typing import Optional, Dict, Any, List
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# ── Escalation email config ─────────────────────────────────────────
ESCALATION_EMAIL = os.getenv("ESCALATION_EMAIL", "lorenzo@ef-ti.com")

# ── System prompts ──────────────────────────────────────────────────

SYSTEM_GENERATE = """Sei l'assistente automatico di recupero crediti di Sake Company.
Scrivi messaggi WhatsApp brevi (max 3 frasi), professionali ma cordiali.
Lingua: italiano. Tono: fermo ma rispettoso.
NON usare formalismi legali. NON minacciare. Sii diretto e chiaro.
Includi sempre: importo dovuto, numero fattura, e cosa fare per risolvere.
Firma sempre come "Sake Company - Ufficio Amministrativo".

Livelli di escalation:
- Livello 1: Promemoria gentile. "Ci risulta un pagamento in sospeso..."
- Livello 2: Sollecito professionale. "Non avendo ricevuto riscontro..."
- Livello 3: Avviso formale. "Siamo costretti a segnalare che..."
- Livello 4: Ultimo avviso. "In assenza di pagamento entro 7 giorni..."

Il messaggio deve essere BREVE — è un WhatsApp, non una lettera."""

SYSTEM_CLASSIFY = """Sei un classificatore di risposte per recupero crediti.
Analizza il messaggio del cliente e rispondi SOLO con un JSON valido.

Classificazioni possibili:
- "payment_confirm": Il cliente dice che ha pagato o sta pagando
- "payment_promise": Il cliente promette di pagare entro una data
- "extension": Il cliente chiede più tempo senza una data precisa
- "dispute": Il cliente contesta la fattura o l'importo
- "info_request": Il cliente chiede dettagli/chiarimenti
- "wrong_number": Non è la persona giusta
- "opt_out": Chiede di non essere contattato
- "positive": Risposta genericamente positiva/collaborativa
- "negative": Risposta ostile/rifiuto
- "unclear": Non si capisce l'intento

Rispondi SOLO con questo JSON:
{
  "intent": "...",
  "confidence": 0.0-1.0,
  "needs_human": true/false,
  "summary": "breve riassunto in italiano",
  "suggested_reply": "eventuale risposta da mandare o null",
  "payment_date": "YYYY-MM-DD se menzionata, altrimenti null"
}

needs_human = true quando:
- Il cliente è arrabbiato o offensivo
- Contesta qualcosa di specifico che richiede verifica
- La situazione è ambigua e potresti sbagliare
- Chiede di parlare con qualcuno
- Menziona un avvocato o azione legale"""

SYSTEM_REPLY = """Sei l'assistente di recupero crediti di Sake Company.
Scrivi una risposta WhatsApp BREVISSIMA (1-2 frasi max) al messaggio del cliente.
Sii professionale, cordiale, e utile. Lingua: italiano.
Se il cliente ha pagato: ringrazia e conferma.
Se promette di pagare: conferma la data e ringrazia.
Se chiede info: dai i dettagli richiesti.
Se chiede tempo: concedi con gentilezza ma ricorda l'urgenza.
Firma: "Sake Company"."""


async def _call_claude(
    system: str,
    user_message: str,
    max_tokens: int = 500,
) -> Optional[str]:
    """Call Anthropic Claude API."""
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — AI engine disabled")
        return None

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user_message}],
                },
            )

            if response.status_code != 200:
                logger.error(f"Claude API error {response.status_code}: {response.text}")
                return None

            data = response.json()
            return data["content"][0]["text"]

    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return None


# ── Public API ──────────────────────────────────────────────────────

async def generate_message(
    customer_name: str,
    invoice_number: str,
    amount_due: float,
    days_overdue: int,
    escalation_level: int,
    previous_messages: List[str] = None,
) -> Optional[str]:
    """Generate a WhatsApp recovery message using Claude.

    Args:
        customer_name: Company/person name
        invoice_number: Invoice reference number
        amount_due: Amount owed in EUR
        days_overdue: Days past due date
        escalation_level: 1-4, determines tone
        previous_messages: List of previous message bodies for context

    Returns:
        Generated message body, or None if AI unavailable
    """
    context = f"""Genera un messaggio di recupero crediti (livello {escalation_level}/4).

Cliente: {customer_name}
Fattura: {invoice_number}
Importo dovuto: €{amount_due:,.2f}
Giorni di ritardo: {days_overdue}"""

    if previous_messages:
        context += "\n\nMessaggi precedenti inviati:"
        for i, msg in enumerate(previous_messages[-3:], 1):
            context += f"\n{i}. {msg[:150]}"

    text = await _call_claude(SYSTEM_GENERATE, context, max_tokens=300)

    if not text:
        # Fallback: template statico
        return _fallback_message(customer_name, invoice_number, amount_due, days_overdue, escalation_level)

    return text.strip()


async def classify_response(
    customer_message: str,
    context: str = "",
) -> Dict[str, Any]:
    """Classify an incoming customer reply using Claude.

    Args:
        customer_message: The customer's reply text
        context: Optional context (previous messages, invoice info)

    Returns:
        Classification dict with intent, confidence, needs_human, etc.
    """
    prompt = f"Messaggio del cliente:\n\"{customer_message}\""
    if context:
        prompt += f"\n\nContesto:\n{context}"

    text = await _call_claude(SYSTEM_CLASSIFY, prompt, max_tokens=300)

    if not text:
        # Fallback: keyword classification
        return _fallback_classify(customer_message)

    try:
        # Extract JSON from response (handle markdown code blocks)
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(clean)
    except (json.JSONDecodeError, IndexError):
        logger.warning(f"Failed to parse AI classification: {text}")
        return _fallback_classify(customer_message)


async def generate_reply(
    customer_message: str,
    intent: str,
    customer_name: str,
    invoice_number: str,
    amount_due: float,
) -> Optional[str]:
    """Generate an auto-reply to a customer message.

    Args:
        customer_message: What the customer said
        intent: Classified intent
        customer_name: Company name
        invoice_number: Invoice ref
        amount_due: Amount owed

    Returns:
        Reply message or None
    """
    prompt = f"""Il cliente {customer_name} ha risposto:
"{customer_message}"

Intent classificato: {intent}
Fattura: {invoice_number}
Importo dovuto: €{amount_due:,.2f}

Genera una risposta breve e appropriata."""

    text = await _call_claude(SYSTEM_REPLY, prompt, max_tokens=200)
    return text.strip() if text else None


# ── Fallbacks (when AI is unavailable) ──────────────────────────────

def _fallback_message(
    customer_name: str,
    invoice_number: str,
    amount_due: float,
    days_overdue: int,
    level: int,
) -> str:
    """Static template fallback."""
    templates = {
        1: (
            f"Buongiorno, ci risulta un pagamento in sospeso per la fattura "
            f"{invoice_number} di €{amount_due:,.2f}, scaduta da {days_overdue} giorni. "
            f"La preghiamo di provvedere al saldo. "
            f"Sake Company - Ufficio Amministrativo"
        ),
        2: (
            f"Gentile cliente, non avendo ricevuto riscontro, le ricordiamo che la fattura "
            f"{invoice_number} di €{amount_due:,.2f} risulta ancora in sospeso da {days_overdue} giorni. "
            f"La preghiamo di contattarci per risolvere. "
            f"Sake Company - Ufficio Amministrativo"
        ),
        3: (
            f"La informiamo che la fattura {invoice_number} di €{amount_due:,.2f} "
            f"risulta insoluta da {days_overdue} giorni. "
            f"In assenza di riscontro saremo costretti a procedere per vie legali. "
            f"Sake Company - Ufficio Amministrativo"
        ),
        4: (
            f"ULTIMO AVVISO: la fattura {invoice_number} di €{amount_due:,.2f} "
            f"è insoluta da {days_overdue} giorni. "
            f"Senza pagamento entro 7 giorni, trasmetteremo la pratica al nostro legale. "
            f"Sake Company - Ufficio Amministrativo"
        ),
    }
    return templates.get(level, templates[1])


def _fallback_classify(text: str) -> Dict[str, Any]:
    """Keyword-based fallback classification."""
    text_lower = text.lower()

    if any(w in text_lower for w in ["pagato", "pagata", "pago", "bonifico", "versato"]):
        return {"intent": "payment_confirm", "confidence": 0.6, "needs_human": False,
                "summary": "Il cliente dice di aver pagato", "suggested_reply": None, "payment_date": None}

    if any(w in text_lower for w in ["proroga", "posso pagare", "tra poco", "presto", "settimana"]):
        return {"intent": "payment_promise", "confidence": 0.5, "needs_human": False,
                "summary": "Il cliente chiede tempo", "suggested_reply": None, "payment_date": None}

    if any(w in text_lower for w in ["non devo", "contestazione", "errore", "sbagliato"]):
        return {"intent": "dispute", "confidence": 0.6, "needs_human": True,
                "summary": "Il cliente contesta", "suggested_reply": None, "payment_date": None}

    if any(w in text_lower for w in ["stop", "basta", "cancella", "non voglio"]):
        return {"intent": "opt_out", "confidence": 0.7, "needs_human": False,
                "summary": "Il cliente chiede di non essere contattato", "suggested_reply": None, "payment_date": None}

    if any(w in text_lower for w in ["quanto", "quale", "dettagli", "info"]):
        return {"intent": "info_request", "confidence": 0.5, "needs_human": False,
                "summary": "Il cliente chiede informazioni", "suggested_reply": None, "payment_date": None}

    return {"intent": "unclear", "confidence": 0.3, "needs_human": True,
            "summary": "Messaggio non chiaro", "suggested_reply": None, "payment_date": None}
