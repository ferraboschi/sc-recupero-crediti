"""Twilio WhatsApp connector for sending/receiving messages."""

import logging
import base64
import json
from typing import Optional, Dict, Any
from datetime import datetime
from backend.connectors.base import BaseConnector
from backend.config import config

logger = logging.getLogger(__name__)


class TwilioWhatsAppConnector(BaseConnector):
    """Connector for Twilio WhatsApp messaging API."""

    def __init__(self):
        """Initialize Twilio WhatsApp connector."""
        # Twilio API base URL with account SID
        account_sid = config.TWILIO_ACCOUNT_SID
        base_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"

        super().__init__(
            base_url=base_url,
            timeout=30,
            max_retries=3
        )

        self.account_sid = account_sid
        self.auth_token = config.TWILIO_AUTH_TOKEN
        self.whatsapp_number_business = config.TWILIO_WHATSAPP_NUMBER_BUSINESS
        self.whatsapp_number_recovery = config.TWILIO_WHATSAPP_NUMBER_RECOVERY

        if not all([self.account_sid, self.auth_token]):
            logger.warning("Twilio credentials not fully configured")

    def _get_auth_header(self) -> Dict[str, str]:
        """Generate HTTP Basic auth header."""
        credentials = f"{self.account_sid}:{self.auth_token}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    def send_whatsapp(
        self,
        to_number: str,
        body: str,
        from_number: Optional[str] = None
    ) -> Optional[str]:
        """
        Send a WhatsApp message.

        Args:
            to_number: Recipient phone number (format: +39...)
            body: Message body
            from_number: Sender WhatsApp number (default: recovery number)

        Returns:
            Message SID if successful, None otherwise
        """
        try:
            if not from_number:
                from_number = self.whatsapp_number_recovery

            # Format numbers as whatsapp:+39...
            to_formatted = self.format_whatsapp_number(to_number)
            from_formatted = self.format_whatsapp_number(from_number)

            headers = self._get_auth_header()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

            data = {
                "From": from_formatted,
                "To": to_formatted,
                "Body": body,
            }

            response = self.post(
                "Messages.json",
                headers=headers,
                data=data
            )

            if isinstance(response, dict) and "sid" in response:
                message_sid = response["sid"]
                logger.info(f"WhatsApp message sent to {to_number}: {message_sid}")
                return message_sid
            else:
                logger.error(f"Unexpected response format: {response}")
                return None

        except Exception as e:
            logger.error(f"Error sending WhatsApp message to {to_number}: {e}")
            return None

    def get_message_status(self, message_sid: str) -> Optional[str]:
        """
        Get delivery status of a message.

        Args:
            message_sid: Message SID from send_whatsapp

        Returns:
            Status string (queued, sent, delivered, failed, etc.)
        """
        try:
            headers = self._get_auth_header()

            response = self.get(
                f"Messages/{message_sid}.json",
                headers=headers
            )

            if isinstance(response, dict) and "status" in response:
                status = response["status"]
                logger.debug(f"Message {message_sid} status: {status}")
                return status

            logger.warning(f"Could not determine status for {message_sid}")
            return None

        except Exception as e:
            logger.error(f"Error getting message status for {message_sid}: {e}")
            return None

    def parse_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse incoming Twilio webhook data.

        Args:
            payload: Webhook payload from Twilio

        Returns:
            Dictionary with extracted fields and detected intent
        """
        try:
            from_number = payload.get("From", "").replace("whatsapp:", "")
            body = payload.get("Body", "").strip()
            message_sid = payload.get("MessageSid", "")
            status = payload.get("MessageStatus", "")

            # Detect intent from reply
            intent = self._detect_intent(body)

            return {
                "from_number": from_number,
                "body": body,
                "message_sid": message_sid,
                "status": status,
                "intent": intent,
                "timestamp": datetime.utcnow().isoformat(),
            }

        except Exception as e:
            logger.error(f"Error parsing webhook payload: {e}")
            return {
                "intent": "unknown",
                "error": str(e),
            }

    def _detect_intent(self, body: str) -> str:
        """
        Detect intent from message body using keyword matching.

        Args:
            body: Message body

        Returns:
            Intent string
        """
        body_lower = body.lower().strip()

        # Payment confirmation keywords
        payment_keywords = ["pagato", "pagata", "pago", "trasferito", "versato", "bonifico", "sì"]
        if any(kw in body_lower for kw in payment_keywords):
            return "payment_confirm"

        # Extension request keywords
        extension_keywords = ["proroga", "rimandare", "posso pagare", "posso pagare più tardi", "aspetta", "presto", "tra poco"]
        if any(kw in body_lower for kw in extension_keywords):
            return "extension"

        # Dispute keywords
        dispute_keywords = ["non devo", "non sono", "sbagliato", "errore", "controversia", "ricorso", "contestazione"]
        if any(kw in body_lower for kw in dispute_keywords):
            return "dispute"

        # Info request keywords
        info_keywords = ["quanto", "quanto devo", "qual è", "quale", "come", "informazioni", "dettagli", "fattura"]
        if any(kw in body_lower for kw in info_keywords):
            return "info_request"

        # Wrong number keywords
        wrong_keywords = ["sbagliato", "numero sbagliato", "non conosco", "chi sei", "chi è", "non mi riguarda"]
        if any(kw in body_lower for kw in wrong_keywords):
            return "wrong_number"

        # Opt-out keywords
        optout_keywords = ["cancella", "non voglio", "basta", "smetti", "stop", "no più", "unsubscribe"]
        if any(kw in body_lower for kw in optout_keywords):
            return "opt_out"

        return "unknown"

    def format_whatsapp_number(self, phone: str) -> str:
        """
        Format phone number to WhatsApp format.

        Args:
            phone: Phone number (various formats)

        Returns:
            Formatted phone in whatsapp:+39... format
        """
        # Remove common formatting characters
        cleaned = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

        # Add +39 if not present and looks like Italian number
        if not cleaned.startswith("+"):
            if cleaned.startswith("39"):
                cleaned = "+" + cleaned
            elif cleaned.startswith("0"):
                # Remove leading 0 and add +39
                cleaned = "+39" + cleaned[1:]
            else:
                # Assume it's missing country code
                cleaned = "+39" + cleaned

        # Ensure whatsapp: prefix
        if not cleaned.startswith("whatsapp:"):
            cleaned = f"whatsapp:{cleaned}"

        return cleaned
