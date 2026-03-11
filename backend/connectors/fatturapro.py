"""FatturaPro connector for fetching overdue invoices via web scraping.

FatturaPro has no public documented API, so we use authenticated sessions
with BeautifulSoup4 for parsing HTML responses from the xcrud AJAX framework.
"""

import logging
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from backend.config import config

logger = logging.getLogger(__name__)


class FatturaProConnector:
    """Connector for FatturaPro platform using web scraping."""

    def __init__(self, timeout: int = 30, max_retries: int = 3):
        """Initialize FatturaPro connector.

        Args:
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
        """
        self.base_url = config.FATTURAPRO_API_URL.rstrip("/")
        self.api_key = config.FATTURAPRO_API_KEY
        self.domain = config.FATTURAPRO_DOMAIN
        self.timeout = timeout
        self.max_retries = max_retries

        # Initialize session with cookie persistence
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )
        self._xcrud_key: Optional[str] = None
        self._authenticated = False

    def set_xcrud_key(self, key: str):
        """Manually set the xcrud key (e.g., from browser session).

        Args:
            key: The xcrud key value
        """
        self._xcrud_key = key
        self._authenticated = True
        logger.info(f"xcrud key set manually: {key[:8]}...")

    def set_cookies(self, cookies: dict):
        """Set cookies for the HTTP client (e.g., from browser session).

        Args:
            cookies: Dictionary of cookie name-value pairs
        """
        for name, value in cookies.items():
            self.client.cookies.set(name, value, domain="cloud.fatturapro.click")
        logger.info(f"Set {len(cookies)} cookies for FatturaPro session")

    def login(self) -> bool:
        """Authenticate with FatturaPro platform.

        Attempts authentication in this order:
        1. Try /ws/ endpoint with API key
        2. Fall back to web form authentication

        Returns:
            True if authentication successful, False otherwise
        """
        if self._authenticated:
            return True

        logger.info("Attempting FatturaPro authentication...")

        # Try /ws/ endpoint first
        if self._try_ws_auth():
            self._authenticated = True
            logger.info("Successfully authenticated via /ws/ endpoint")
            return True

        # Fall back to web form authentication
        if self._try_web_auth():
            self._authenticated = True
            logger.info("Successfully authenticated via web form")
            return True

        logger.error("Failed to authenticate with FatturaPro")
        return False

    def _try_ws_auth(self) -> bool:
        """Try authentication via /ws/ endpoint with API key.

        Returns:
            True if successful, False otherwise
        """
        try:
            endpoint = f"{self.base_url}/ws/"
            response = self.client.post(
                endpoint,
                data={"apiKey": self.api_key},
                timeout=self.timeout
            )

            if response.status_code == 200:
                # Check if response indicates success (varies by endpoint)
                if "error" not in response.text.lower():
                    logger.debug(f"WS auth response: {response.text[:100]}")
                    return True

            return False
        except Exception as e:
            logger.debug(f"WS authentication failed: {e}")
            return False

    def _try_web_auth(self) -> bool:
        """Try authentication via web login form.

        This is a fallback if /ws/ endpoint doesn't work.
        Requires credentials or session token setup.

        Returns:
            True if session is valid, False otherwise
        """
        try:
            # Try accessing the main page to see if we have valid cookies
            response = self.client.get(
                f"{self.base_url}/documenti.php",
                timeout=self.timeout
            )

            # If we get a redirect to login or 403, authentication failed
            if response.status_code in [401, 403]:
                logger.debug("Web authentication: Access denied")
                return False

            if response.status_code == 200:
                # Successfully accessed the page
                logger.debug("Web form access successful")
                return True

            return False
        except Exception as e:
            logger.debug(f"Web authentication attempt failed: {e}")
            return False

    def _get_xcrud_key(self) -> Optional[str]:
        """Parse the xcrud key from documenti.php page HTML.

        The xcrud key is required for subsequent AJAX requests.
        It's typically found in a hidden input or as a data attribute.

        Returns:
            The xcrud key if found, None otherwise
        """
        if self._xcrud_key:
            return self._xcrud_key

        try:
            response = self.client.get(
                f"{self.base_url}/documenti.php?s=1",
                timeout=self.timeout
            )
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Look for xcrud key in common locations
            # Pattern 1: In a data attribute
            xcrud_div = soup.find(attrs={"data-xcrud-key": True})
            if xcrud_div:
                self._xcrud_key = xcrud_div.get("data-xcrud-key")
                logger.debug(f"Found xcrud key in data attribute: {self._xcrud_key}")
                return self._xcrud_key

            # Pattern 2: In a script tag as variable
            scripts = soup.find_all("script")
            for script in scripts:
                if script.string:
                    match = re.search(r'xcrud_key\s*=\s*["\']([a-f0-9]+)["\']', script.string)
                    if match:
                        self._xcrud_key = match.group(1)
                        logger.debug(f"Found xcrud key in script: {self._xcrud_key}")
                        return self._xcrud_key

            # Pattern 3: In a hidden input field named "key" (xcrud standard)
            key_input = soup.find("input", {"name": "key", "type": "hidden"})
            if key_input:
                self._xcrud_key = key_input.get("value")
                logger.debug(f"Found xcrud key in hidden input: {self._xcrud_key}")
                return self._xcrud_key

            # Pattern 4: Legacy name
            key_input = soup.find("input", {"name": "xcrud_key"})
            if key_input:
                self._xcrud_key = key_input.get("value")
                logger.debug(f"Found xcrud key in xcrud_key input: {self._xcrud_key}")
                return self._xcrud_key

            logger.warning("Could not find xcrud key in page HTML")
            return None

        except Exception as e:
            logger.error(f"Error retrieving xcrud key: {e}")
            return None

    def fetch_overdue_invoices(self) -> List[Dict[str, Any]]:
        """Fetch all overdue invoices from FatturaPro.

        Scrapes the documenti.php?s=1 page ("Da incassare" / invoices to collect)
        and paginates through all results using AJAX requests.

        Returns:
            List of invoice dictionaries with keys:
            - invoice_number
            - date
            - customer_name
            - total
            - balance (saldo)
            - doc_id
            - source_platform: "fatturapro"
        """
        if not self._authenticated:
            if not self.login():
                logger.error("Cannot fetch invoices: not authenticated")
                return []

        logger.info("Fetching overdue invoices from FatturaPro...")
        all_invoices = []

        try:
            xcrud_key = self._get_xcrud_key()
            if not xcrud_key:
                logger.error("Cannot proceed without xcrud key")
                return []

            # Paginate through all results
            offset = 0
            limit = 10

            while True:
                logger.debug(f"Fetching invoices at offset {offset}...")

                response = self.client.post(
                    f"{self.base_url}/xcrud/xcrud_ajax.php",
                    data={
                        "key": xcrud_key,
                        "instance": "documenti",
                        "task": "list",
                        "start": offset,
                        "limit": limit,
                        "orderby": "documenti.Data",
                        "order": "desc"
                    },
                    timeout=self.timeout
                )
                response.raise_for_status()

                # Parse the HTML response
                invoices_batch = self._parse_invoice_table(response.text)

                if not invoices_batch:
                    logger.debug("No more invoices found, stopping pagination")
                    break

                all_invoices.extend(invoices_batch)

                # If we got fewer results than the limit, we've reached the end
                if len(invoices_batch) < limit:
                    logger.debug(f"Received {len(invoices_batch)} results (< {limit}), reached end")
                    break

                offset += limit

            logger.info(f"Successfully fetched {len(all_invoices)} overdue invoices")
            return all_invoices

        except Exception as e:
            logger.error(f"Error fetching overdue invoices: {e}", exc_info=True)
            return []

    def _parse_invoice_table(self, html: str) -> List[Dict[str, Any]]:
        """Parse invoice data from xcrud HTML table response.

        Expected table columns: Documento, Data, Destinatario, Totale, Saldo
        Each row has a data-doc_id attribute for the invoice ID.

        Args:
            html: HTML response from xcrud_ajax.php

        Returns:
            List of parsed invoice dictionaries
        """
        invoices = []

        try:
            soup = BeautifulSoup(html, "html.parser")

            # Find the table rows
            rows = soup.find_all("tr")

            for row in rows:
                try:
                    # Skip header rows
                    if row.find("th"):
                        continue

                    # Get the doc_id from action links within the row
                    action_link = row.find("a", {"data-doc_id": True})
                    doc_id = action_link.get("data-doc_id") if action_link else None

                    # Extract cells
                    cells = row.find_all("td")
                    if len(cells) < 5:
                        logger.debug(f"Row has {len(cells)} cells, expected >= 5, skipping")
                        continue

                    # Parse cells: Documento, Data, Destinatario, Totale, Saldo
                    invoice_number = cells[0].get_text(strip=True)
                    date_str = cells[1].get_text(strip=True)
                    customer_name = cells[2].get_text(strip=True)
                    total_str = cells[3].get_text(strip=True)
                    balance_str = cells[4].get_text(strip=True)

                    # Parse numeric values
                    total = self._parse_currency(total_str)
                    balance = self._parse_currency(balance_str)

                    # Parse date (format typically: DD/MM/YYYY)
                    try:
                        invoice_date = datetime.strptime(date_str, "%d/%m/%Y").date()
                    except ValueError:
                        logger.warning(f"Could not parse date: {date_str}")
                        invoice_date = None

                    invoice = {
                        "invoice_number": invoice_number,
                        "date": invoice_date,
                        "customer_name": customer_name,
                        "total": total,
                        "balance": balance,
                        "doc_id": doc_id,
                        "source_platform": "fatturapro"
                    }

                    invoices.append(invoice)
                    logger.debug(f"Parsed invoice: {invoice_number} - {customer_name} - {balance}")

                except Exception as e:
                    logger.warning(f"Error parsing invoice row: {e}")
                    continue

            return invoices

        except Exception as e:
            logger.error(f"Error parsing invoice table: {e}")
            return []

    def _parse_currency(self, value_str: str) -> float:
        """Parse currency string to float.

        Handles various formats like "1.234,56" (IT format) or "1,234.56" (EN format).

        Args:
            value_str: Currency string to parse

        Returns:
            Parsed float value, or 0.0 if parsing fails
        """
        try:
            # Remove whitespace
            value_str = value_str.strip()

            # Remove currency symbols and common prefixes
            value_str = re.sub(r'[€$\s]', '', value_str)

            # Italian format: 1.234,56 -> use comma as decimal
            # English format: 1,234.56 -> use period as decimal
            # Heuristic: if there's both comma and period, the last one is the decimal
            if ',' in value_str and '.' in value_str:
                if value_str.rindex(',') > value_str.rindex('.'):
                    # Italian format
                    value_str = value_str.replace('.', '').replace(',', '.')
                else:
                    # English format
                    value_str = value_str.replace(',', '')
            elif ',' in value_str:
                # Only comma - could be either format
                # Check if there are digits after comma
                parts = value_str.split(',')
                if len(parts[1]) == 2:
                    # Likely Italian format (cents)
                    value_str = value_str.replace('.', '').replace(',', '.')
                else:
                    # Likely English format (thousands)
                    value_str = value_str.replace(',', '')

            return float(value_str)
        except Exception:
            logger.warning(f"Could not parse currency value: {value_str}")
            return 0.0

    def fetch_invoice_detail(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Fetch detailed information for a single invoice.

        Retrieves the detail page for an invoice to extract P.IVA and other info.

        Args:
            doc_id: The document ID from the invoice list

        Returns:
            Dictionary with invoice details including P.IVA, or None if failed
        """
        try:
            logger.debug(f"Fetching invoice detail for doc_id: {doc_id}")

            # Construct detail page URL
            detail_url = f"{self.base_url}/documenti.php?id={doc_id}&action=edit"

            response = self.client.get(detail_url, timeout=self.timeout)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            detail = {"doc_id": doc_id}

            # Try to extract P.IVA from various common field locations
            # Pattern 1: Form field with name containing "piva" or "partita_iva"
            piva_input = soup.find(["input", "textarea"], {
                "name": re.compile(r"(piva|partita_iva|p\.iva|pivanumber)", re.IGNORECASE)
            })
            if piva_input:
                detail["piva"] = piva_input.get("value", "").strip()

            # Pattern 2: Table cell with P.IVA label
            piva_label = soup.find(text=re.compile(r"P\.?IVA", re.IGNORECASE))
            if piva_label:
                # Try to find the value in the next sibling
                parent = piva_label.parent
                for sibling in parent.find_next_siblings():
                    text = sibling.get_text(strip=True)
                    if text and not re.match(r"^P\.?IVA", text, re.IGNORECASE):
                        detail["piva"] = text
                        break

            logger.debug(f"Invoice detail: {detail}")
            return detail

        except Exception as e:
            logger.error(f"Error fetching invoice detail for {doc_id}: {e}")
            return None

    def request_xml_export(self, date_from: str, date_to: str) -> Optional[bytes]:
        """Request XML export of invoices for a date range.

        Uses the bulk XML export feature at esportazioni.php to export
        "Documenti Emessi in formato XML" (issued documents in FatturaPA XML format).

        Args:
            date_from: Start date in YYYY-MM-DD format
            date_to: End date in YYYY-MM-DD format

        Returns:
            XML content as bytes, or None if export failed
        """
        try:
            logger.info(f"Requesting XML export from {date_from} to {date_to}...")

            # Convert dates to Italian format if needed
            if len(date_from) == 10:  # YYYY-MM-DD format
                date_from_it = date_from.replace("-", "/")[-2:] + "/" + date_from.replace("-", "/")[-5:-3] + "/" + date_from.replace("-", "/")[:4]
                date_to_it = date_to.replace("-", "/")[-2:] + "/" + date_to.replace("-", "/")[-5:-3] + "/" + date_to.replace("-", "/")[:4]
            else:
                date_from_it = date_from
                date_to_it = date_to

            # Request XML export with parameters
            response = self.client.post(
                f"{self.base_url}/esportazioni.php",
                data={
                    "export_type": "xml",
                    "format": "fatturaPA",
                    "date_from": date_from_it,
                    "date_to": date_to_it,
                },
                timeout=self.timeout
            )

            response.raise_for_status()

            # Check if response is XML
            if response.headers.get("content-type", "").startswith("text/xml"):
                logger.info(f"XML export successful, received {len(response.content)} bytes")
                return response.content
            else:
                # Response might be HTML with a download link
                soup = BeautifulSoup(response.text, "html.parser")
                download_link = soup.find("a", href=re.compile(r"\.xml$"))

                if download_link:
                    xml_url = download_link.get("href")
                    if not xml_url.startswith("http"):
                        xml_url = urljoin(self.base_url, xml_url)

                    logger.debug(f"Following download link: {xml_url}")
                    xml_response = self.client.get(xml_url, timeout=self.timeout)
                    xml_response.raise_for_status()

                    logger.info(f"XML export downloaded, received {len(xml_response.content)} bytes")
                    return xml_response.content

            logger.warning("Could not obtain XML export file")
            return None

        except Exception as e:
            logger.error(f"Error requesting XML export: {e}", exc_info=True)
            return None

    def close(self):
        """Close the HTTP client connection."""
        try:
            self.client.close()
            logger.debug("FatturaPro connector closed")
        except Exception as e:
            logger.warning(f"Error closing connector: {e}")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
