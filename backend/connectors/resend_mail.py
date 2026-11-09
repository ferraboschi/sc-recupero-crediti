"""Invio email tramite Resend (https://resend.com), usato dai promemoria
pre-scadenza. Connettore minimale: una chiamata HTTPS con allegati in base64.

Regole: la chiave API non finisce MAI nei log né nei messaggi d'errore (i log
Render persistono); l'URL base è env-overridabile (RESEND_API_URL) con il
fallback reale, come per gli altri provider.
"""
import base64
import logging
from typing import Iterable, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.resend.com"


class MailSendError(Exception):
    """Invio rifiutato o fallito: il messaggio è già pulito (niente segreti)."""


class ResendMailer:
    def __init__(
        self,
        api_key: str,
        api_url: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        if not api_key:
            raise MailSendError("RESEND_API_KEY non configurata")
        self._api_key = api_key
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self.client = httpx.Client(
            base_url=self.api_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    @staticmethod
    def _as_list(value) -> Optional[List[str]]:
        if value is None:
            return None
        if isinstance(value, str):
            items = [v.strip() for v in value.split(",") if v.strip()]
            return items or None
        items = [str(v).strip() for v in value if str(v).strip()]
        return items or None

    def send(
        self,
        *,
        sender: str,
        to,
        subject: str,
        text: str,
        html: Optional[str] = None,
        reply_to=None,
        bcc=None,
        attachments: Optional[Iterable[Tuple[str, bytes]]] = None,
    ) -> str:
        """Invia e ritorna l'id del messaggio assegnato da Resend."""
        payload = {
            "from": sender,
            "to": self._as_list(to),
            "subject": subject,
            "text": text,
        }
        if html:
            payload["html"] = html
        reply = self._as_list(reply_to)
        if reply:
            payload["reply_to"] = reply
        bcc_list = self._as_list(bcc)
        if bcc_list:
            payload["bcc"] = bcc_list
        if attachments:
            payload["attachments"] = [
                {
                    "filename": name,
                    "content": base64.b64encode(bytes(content)).decode("ascii"),
                }
                for name, content in attachments
            ]
        try:
            response = self.client.post("/emails", json=payload)
        except httpx.HTTPError as e:
            raise MailSendError(f"Resend non raggiungibile: {type(e).__name__}") from e
        if response.status_code >= 300:
            message = ""
            try:
                body = response.json()
                message = str(body.get("message") or body.get("error") or "")
            except Exception:
                message = (response.text or "")[:200]
            raise MailSendError(
                f"Resend HTTP {response.status_code}: {message[:300]}".strip()
            )
        try:
            message_id = str(response.json().get("id") or "")
        except Exception:
            message_id = ""
        logger.info(
            "Resend: email inviata (id=%s, allegati=%d)",
            message_id or "?",
            len(payload.get("attachments", [])),
        )
        return message_id

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass
