"""Fattura24 connector for legacy billing platform."""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
import xml.etree.ElementTree as ET
from backend.connectors.base import BaseConnector
from backend.config import config

logger = logging.getLogger(__name__)


class Fattura24SubscriptionError(Exception):
    """Raised when Fattura24 subscription is expired or API access is denied."""
    pass


class Fattura24Connector(BaseConnector):
    """Connector for Fattura24 legacy billing API (XML-based).

    API docs: https://www.fattura24.com/api/introduzione/
    Base URL: https://www.app.fattura24.com/api/v0.3
    All requests must use POST with Content-Type: application/x-www-form-urlencoded
    """

    def __init__(self):
        """Initialize Fattura24 connector."""
        super().__init__(
            base_url=config.FATTURA24_API_URL,
            timeout=30,
            max_retries=3
        )
        self.api_key = config.FATTURA24_API_KEY
        if not self.api_key:
            logger.warning("FATTURA24_API_KEY not configured")

    def _request(
        self,
        method: str,
        endpoint: str,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
        data: Optional[Any] = None,
    ) -> Dict:
        """Override to handle XML responses and authorization errors."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        last_error = None

        # Fattura24 API requires this Content-Type
        if headers is None:
            headers = {}
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    data=data,
                )

                # Rate limit handling
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 2))
                    logger.warning(f"Rate limited on {url}. Waiting {retry_after}s")
                    import time
                    time.sleep(retry_after)
                    continue

                # 404 = endpoint doesn't exist
                if response.status_code == 404:
                    raise Exception(
                        f"Endpoint {endpoint} non trovato (404). "
                        f"L'API Fattura24 v0.3 potrebbe non supportare questo endpoint."
                    )

                response.raise_for_status()

                # Try to parse as XML first
                try:
                    root = ET.fromstring(response.text)
                    parsed = self._xml_to_dict(root)

                    # Check for authorization errors (subscription expired)
                    if isinstance(parsed, dict) and "root" in parsed:
                        root_data = parsed["root"]
                        if isinstance(root_data, dict):
                            return_code = root_data.get("returnCode", "")
                            description = root_data.get("description", "")
                            # returnCode -1 or -10 = not authorized
                            if return_code in ("-1", "-10"):
                                raise Fattura24SubscriptionError(
                                    f"Fattura24 API non autorizzata (codice {return_code}): {description}. "
                                    f"L'abbonamento Fattura24 potrebbe essere scaduto. "
                                    f"Riattivare l'abbonamento su fattura24.com."
                                )

                    return parsed
                except Fattura24SubscriptionError:
                    raise
                except ET.ParseError:
                    # Fallback to raw text if XML parsing fails
                    return {"raw": response.text}

            except Fattura24SubscriptionError:
                raise
            except Exception as e:
                last_error = e
                logger.error(f"Request error on {url} (attempt {attempt}): {e}")
                if attempt < self.max_retries:
                    # Don't retry subscription errors or 404s
                    if isinstance(e, Fattura24SubscriptionError) or "404" in str(e):
                        raise
                    import time
                    time.sleep(2 ** attempt)
                    continue
                raise

        raise last_error or Exception(f"Failed after {self.max_retries} retries")

    def test_connection(self) -> Dict:
        """Test API key and connection. Returns status info."""
        try:
            response = self.post(
                "TestKey",
                data={"apiKey": self.api_key}
            )
            return {"success": True, "response": response}
        except Fattura24SubscriptionError as e:
            return {"success": False, "error": str(e), "subscription_expired": True}
        except Exception as e:
            return {"success": False, "error": str(e), "subscription_expired": False}

    def _xml_to_dict(self, element: ET.Element) -> Dict:
        """Convert XML element to dictionary."""
        result = {}

        # Get tag name without namespace
        tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag

        # Get text content
        text = (element.text or "").strip()

        # Collect children
        children = {}
        for child in element:
            child_dict = self._xml_to_dict(child)
            child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag

            if child_tag in children:
                # Convert to list if multiple children with same tag
                if not isinstance(children[child_tag], list):
                    children[child_tag] = [children[child_tag]]
                children[child_tag].append(child_dict)
            else:
                children[child_tag] = child_dict

        # Build result
        if children:
            result = children
            if text:
                result['_text'] = text
        else:
            result = text

        return {tag: result} if len(element) > 0 or not text else {tag: text}

    def fetch_invoices(self, date_from: str, date_to: str) -> List[Dict]:
        """
        Fetch invoices in date range.

        Note: Fattura24 API v0.3 does NOT have a native endpoint to list invoices.
        The API only supports creating documents and retrieving individual files.
        We use GetCustomer to get the customer list, then cross-reference.

        If a 'GetDc' endpoint is available (some accounts may have it), we try it.
        Otherwise we raise a clear error.

        Args:
            date_from: Start date (YYYY-MM-DD)
            date_to: End date (YYYY-MM-DD)

        Returns:
            List of invoice dictionaries

        Raises:
            Fattura24SubscriptionError: If subscription is expired
        """
        # First test connection to detect subscription issues early
        test = self.test_connection()
        if not test["success"]:
            if test.get("subscription_expired"):
                raise Fattura24SubscriptionError(test["error"])
            raise Exception(test["error"])

        # Try GetDc endpoint (may exist for some account types)
        try:
            response = self.post(
                "GetDc",
                data={
                    "apiKey": self.api_key,
                    "dataInizio": date_from,
                    "dataFine": date_to,
                }
            )

            invoices = []

            # Parse response - handle nested structure
            if isinstance(response, dict) and "GetDc" in response:
                docs = response["GetDc"]
                if isinstance(docs, dict) and "Document" in docs:
                    docs_list = docs["Document"]
                    if not isinstance(docs_list, list):
                        docs_list = [docs_list]

                    for doc in docs_list:
                        invoice = self._parse_invoice(doc)
                        if invoice:
                            invoices.append(invoice)

            logger.info(f"Fetched {len(invoices)} invoices from Fattura24 (GetDc)")
            return invoices

        except Fattura24SubscriptionError:
            raise
        except Exception as e:
            logger.warning(
                f"GetDc endpoint non disponibile: {e}. "
                f"L'API Fattura24 v0.3 potrebbe non supportare l'elenco fatture. "
                f"Utilizzare l'importazione CSV manuale."
            )
            raise Exception(
                "L'API Fattura24 non supporta l'elenco fatture (endpoint GetDc non disponibile). "
                "Per importare le fatture da Fattura24, esportarle in CSV dal pannello Fattura24 "
                "e caricarle nella sezione Importa CSV della piattaforma."
            )

    def fetch_overdue_invoices(self) -> List[Dict]:
        """
        Fetch overdue invoices.

        Returns:
            List of overdue invoice dictionaries
        """
        try:
            from datetime import datetime, timedelta

            # Fetch last 90 days to find overdue ones
            date_to = datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

            all_invoices = self.fetch_invoices(date_from, date_to)

            # Filter for overdue (status indicates unpaid)
            overdue = [
                inv for inv in all_invoices
                if self._is_overdue(inv)
            ]

            logger.info(f"Found {len(overdue)} overdue invoices")
            return overdue

        except Exception as e:
            logger.error(f"Error fetching overdue invoices: {e}")
            return []

    def fetch_customers(self) -> List[Dict]:
        """
        Fetch all customers.

        Returns:
            List of customer dictionaries
        """
        try:
            response = self.post(
                "GetCustomer",
                data={"apiKey": self.api_key}
            )

            customers = []

            # Parse response
            if isinstance(response, dict) and "GetCustomer" in response:
                custs = response["GetCustomer"]
                if isinstance(custs, dict) and "Customer" in custs:
                    cust_list = custs["Customer"]
                    if not isinstance(cust_list, list):
                        cust_list = [cust_list]

                    for cust in cust_list:
                        customer = self._parse_customer(cust)
                        if customer:
                            customers.append(customer)

            logger.info(f"Fetched {len(customers)} customers from Fattura24")
            return customers

        except Exception as e:
            logger.error(f"Error fetching customers from Fattura24: {e}")
            return []

    def _parse_invoice(self, doc: Dict) -> Optional[Dict]:
        """Parse invoice from API response."""
        try:
            # Extract fields - handle various XML structure variations
            def get_val(d, key, default=""):
                if isinstance(d, dict):
                    if key in d:
                        val = d[key]
                        if isinstance(val, dict) and '_text' in val:
                            return val['_text']
                        return str(val) if val else default
                return default

            invoice_number = get_val(doc, "Number")
            if not invoice_number:
                return None

            amount_str = get_val(doc, "Total", "0")
            amount_due_str = get_val(doc, "Paid", "0")

            try:
                amount = float(amount_str) if amount_str else 0.0
                amount_paid = float(amount_due_str) if amount_due_str else 0.0
                amount_due = amount - amount_paid
            except (ValueError, TypeError):
                amount = 0.0
                amount_due = 0.0

            issue_date_str = get_val(doc, "DocDate")
            due_date_str = get_val(doc, "DueDate")

            customer_name = get_val(doc, "CustomerName")
            customer_piva = get_val(doc, "CustomerVatNumber")

            return {
                "invoice_number": invoice_number,
                "amount": amount,
                "amount_due": amount_due,
                "issue_date": issue_date_str,
                "due_date": due_date_str,
                "customer_name": customer_name,
                "customer_piva": customer_piva,
                "source_platform": "fatture24",
                "source_id": get_val(doc, "Id"),
            }
        except Exception as e:
            logger.warning(f"Error parsing invoice: {e}")
            return None

    def _parse_customer(self, cust: Dict) -> Optional[Dict]:
        """Parse customer from API response."""
        try:
            def get_val(d, key, default=""):
                if isinstance(d, dict):
                    if key in d:
                        val = d[key]
                        if isinstance(val, dict) and '_text' in val:
                            return val['_text']
                        return str(val) if val else default
                return default

            name = get_val(cust, "Name")
            if not name:
                return None

            return {
                "ragione_sociale": name,
                "partita_iva": get_val(cust, "VatNumber"),
                "email": get_val(cust, "Email"),
                "phone": get_val(cust, "Phone"),
                "source_id": get_val(cust, "Id"),
            }
        except Exception as e:
            logger.warning(f"Error parsing customer: {e}")
            return None

    def _is_overdue(self, invoice: Dict) -> bool:
        """Check if invoice is overdue."""
        try:
            if not invoice.get("due_date"):
                return False

            due_date = datetime.strptime(invoice["due_date"], "%Y-%m-%d").date()
            from datetime import date

            return due_date < date.today() and invoice.get("amount_due", 0) > 0
        except Exception:
            return False
