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
            # Verify session is still valid
            if self._check_session_valid():
                return True
            self._authenticated = False

        logger.info("Attempting FatturaPro authentication...")

        # Check if we already have a valid session (e.g., from cookies)
        if self._check_session_valid():
            self._authenticated = True
            logger.info("Existing session is valid")
            return True

        # Try form login with username/password
        if self._try_form_login():
            self._authenticated = True
            logger.info("Successfully authenticated via form login")
            return True

        # Try /ws/ endpoint as fallback
        if self._try_ws_auth():
            self._authenticated = True
            logger.info("Successfully authenticated via /ws/ endpoint")
            return True

        logger.error("Failed to authenticate with FatturaPro")
        return False

    def _try_form_login(self) -> bool:
        """Try authentication via web login form with username/password.

        First fetches the login page to extract any hidden tokens (CSRF),
        then submits the form with all required fields.

        Returns:
            True if successful, False otherwise
        """
        username = config.FATTURAPRO_USERNAME
        password = config.FATTURAPRO_PASSWORD

        if not username or not password:
            logger.warning("FATTURAPRO_USERNAME or FATTURAPRO_PASSWORD not configured")
            return False

        try:
            logger.info(f"Attempting form login with user {username}...")

            # First, GET the login page to capture cookies and any hidden form fields
            login_page = self.client.get(
                f"{self.base_url}/signin.php",
                timeout=self.timeout,
            )
            logger.info(f"Login page status: {login_page.status_code}, URL: {login_page.url}")

            # Parse login form for hidden fields
            form_data = {
                "username": username,
                "password": password,
                "remember": "on",
            }

            soup = BeautifulSoup(login_page.text, "html.parser")
            login_form = soup.find("form")
            if login_form:
                for hidden in login_form.find_all("input", {"type": "hidden"}):
                    name = hidden.get("name")
                    value = hidden.get("value", "")
                    if name and name not in form_data:
                        form_data[name] = value
                        logger.info(f"Found hidden form field: {name}={value[:20]}...")

            # Submit login form
            response = self.client.post(
                f"{self.base_url}/signin.php",
                data=form_data,
                timeout=self.timeout,
            )
            logger.info(f"Login POST status: {response.status_code}, URL: {response.url}")

            # After successful login, check if we can access a protected page
            check = self.client.get(
                f"{self.base_url}/documenti.php",
                timeout=self.timeout,
            )
            logger.info(f"Post-login check status: {check.status_code}, URL: {check.url}")

            # If we're still on signin.php, login failed
            final_url = str(check.url)
            if "signin.php" in final_url:
                logger.warning("Form login failed: redirected back to signin")
                # Log a snippet of the page to debug
                logger.info(f"Login page snippet: {check.text[:500]}")
                return False

            # Check if the page contains actual content (not login form)
            if "documenti" in check.text.lower() or "xcrud" in check.text.lower():
                logger.info("Form login successful — authenticated!")
                return True

            logger.warning(f"Form login uncertain: final URL={final_url}, content length={len(check.text)}")
            return False

        except Exception as e:
            logger.error(f"Form login error: {e}", exc_info=True)
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
                # Check for valid response - returnCode 0 means success
                if "<returnCode>0</returnCode>" in response.text:
                    logger.debug(f"WS auth response: {response.text[:100]}")
                    return True
                else:
                    logger.debug(f"WS auth rejected: {response.text[:200]}")

            return False
        except Exception as e:
            logger.debug(f"WS authentication failed: {e}")
            return False

    def _check_session_valid(self) -> bool:
        """Check if current session can access protected pages.

        Returns:
            True if session is valid, False otherwise
        """
        try:
            response = self.client.get(
                f"{self.base_url}/documenti.php",
                timeout=self.timeout
            )

            final_url = str(response.url)
            # If redirected to signin, session is invalid
            if "signin.php" in final_url:
                return False

            # If we got 200 and page has actual content
            if response.status_code == 200 and "xcrud" in response.text.lower():
                return True

            return False
        except Exception as e:
            logger.debug(f"Session check failed: {e}")
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

        Scrapes the documenti.php?s=1 page ("Da incassare" / invoices to collect).
        First parses the initial page HTML, then paginates via xcrud AJAX.

        xcrud pagination uses jQuery-style nested POST params:
            xcrud[key]=..., xcrud[start]=10, xcrud[task]=list, etc.

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
        PAGE_SIZE = 10  # xcrud default

        try:
            # Load the initial page which already contains data
            response = self.client.get(
                f"{self.base_url}/documenti.php?s=1",
                timeout=self.timeout,
            )
            response.raise_for_status()

            # Parse the initial page
            initial_invoices = self._parse_invoice_table(response.text)
            all_invoices.extend(initial_invoices)
            logger.info(f"Page 1: {len(initial_invoices)} invoices")

            # Get xcrud key for pagination (from hidden input)
            soup = BeautifulSoup(response.text, "html.parser")
            key_input = soup.find("input", {"name": "key", "type": "hidden"})
            if not key_input:
                logger.warning("No xcrud key found, returning page 1 only")
                return all_invoices

            xcrud_key = key_input.get("value")

            # Check if there are more pages
            if len(initial_invoices) < PAGE_SIZE:
                logger.info(f"All invoices fit on one page ({len(initial_invoices)})")
                return all_invoices

            # Paginate via xcrud AJAX with jQuery-style nested params
            start = PAGE_SIZE
            page = 2
            max_pages = 100  # Safety limit

            while page <= max_pages:
                ajax_resp = self.client.post(
                    f"{self.base_url}/xcrud/xcrud_ajax.php",
                    data={
                        "xcrud[key]": xcrud_key,
                        "xcrud[orderby]": "documenti.Data",
                        "xcrud[order]": "desc",
                        "xcrud[start]": str(start),
                        "xcrud[limit]": str(PAGE_SIZE),
                        "xcrud[instance]": "documenti",
                        "xcrud[task]": "list",
                    },
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": f"{self.base_url}/documenti.php?s=1",
                    },
                    timeout=self.timeout,
                )
                ajax_resp.raise_for_status()

                # Check for xcrud error
                if "xcrud-error" in ajax_resp.text:
                    logger.warning(f"xcrud error at page {page}, stopping pagination")
                    break

                batch = self._parse_invoice_table(ajax_resp.text)
                if not batch:
                    logger.debug(f"Page {page}: empty — pagination complete")
                    break

                all_invoices.extend(batch)

                if page % 10 == 0:
                    logger.info(f"Page {page}: {len(batch)} invoices (running total: {len(all_invoices)})")

                if len(batch) < PAGE_SIZE:
                    logger.debug(f"Page {page}: {len(batch)} invoices (last page)")
                    break

                # Update xcrud key if the response issues a new one
                batch_soup = BeautifulSoup(ajax_resp.text, "html.parser")
                new_key = batch_soup.find("input", {"name": "key", "type": "hidden"})
                if new_key:
                    xcrud_key = new_key.get("value")

                start += PAGE_SIZE
                page += 1

            logger.info(f"Successfully fetched {len(all_invoices)} overdue invoices total")
            return all_invoices

        except Exception as e:
            logger.error(f"Error fetching overdue invoices: {e}", exc_info=True)
            return all_invoices  # Return what we have so far

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

                    # Skip summary/total rows (no invoice number or no date)
                    if not invoice_number or not date_str:
                        logger.debug("Skipping row without invoice number or date (likely summary row)")
                        continue

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

            # Pattern 2: Table cell or label with P.IVA
            if "piva" not in detail or not detail.get("piva"):
                piva_label = soup.find(text=re.compile(r"P\.?\s*IVA", re.IGNORECASE))
                if piva_label:
                    parent = piva_label.parent
                    # Try next sibling elements
                    for sibling in parent.find_next_siblings():
                        text = sibling.get_text(strip=True)
                        if text and not re.match(r"^P\.?\s*IVA", text, re.IGNORECASE):
                            detail["piva"] = text
                            break
                    # Also try parent's next sibling (common in <td>P.IVA</td><td>VALUE</td>)
                    if "piva" not in detail or not detail.get("piva"):
                        next_td = parent.find_next("td")
                        if next_td:
                            text = next_td.get_text(strip=True)
                            if text:
                                detail["piva"] = text

            # Pattern 3: Look for Codice Fiscale / P.IVA in any text content
            if "piva" not in detail or not detail.get("piva"):
                # Search all text nodes for patterns like "P.IVA: 12345678901"
                full_text = soup.get_text()
                piva_match = re.search(
                    r'P\.?\s*IVA\s*[:/]?\s*([A-Z]{0,3}\d{8,15})',
                    full_text,
                    re.IGNORECASE
                )
                if piva_match:
                    detail["piva"] = piva_match.group(1).strip()

            # Validate P.IVA format (Italian: 11 digits, or foreign: country prefix + digits)
            if detail.get("piva"):
                piva_clean = detail["piva"].strip().upper()
                # Accept Italian (11 digits), EU (2 letter prefix + digits), Swiss (CHE + digits)
                if re.match(r'^([A-Z]{2,3})?\d{8,15}$', piva_clean):
                    detail["piva"] = piva_clean
                else:
                    logger.warning(f"Invalid P.IVA format for doc {doc_id}: '{piva_clean}', discarding")
                    detail.pop("piva", None)

            logger.debug(f"Invoice detail: {detail}")
            return detail

        except Exception as e:
            logger.error(f"Error fetching invoice detail for {doc_id}: {e}")
            return None

    def enrich_invoices_with_piva(
        self,
        invoices: List[Dict[str, Any]],
        delay: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """Enrich a list of invoices with P.IVA from their detail pages.

        Only fetches detail for invoices that have a doc_id and don't already
        have a P.IVA. Adds a small delay between requests to avoid overloading
        the FatturaPro server.

        Args:
            invoices: List of invoice dicts from fetch_overdue_invoices()
            delay: Seconds to wait between detail requests (default 0.5s)

        Returns:
            The same list with "customer_piva" key added where found
        """
        import time

        need_detail = [inv for inv in invoices if inv.get("doc_id") and not inv.get("customer_piva")]
        if not need_detail:
            logger.info("No invoices need P.IVA enrichment")
            return invoices

        logger.info(f"Enriching {len(need_detail)} invoices with P.IVA from detail pages...")
        enriched_count = 0
        failed_count = 0

        for i, inv in enumerate(need_detail):
            try:
                detail = self.fetch_invoice_detail(inv["doc_id"])
                if detail and detail.get("piva"):
                    inv["customer_piva"] = detail["piva"]
                    enriched_count += 1
                    logger.debug(
                        f"[{i+1}/{len(need_detail)}] Invoice {inv['invoice_number']}: "
                        f"P.IVA = {detail['piva']}"
                    )
                else:
                    logger.debug(
                        f"[{i+1}/{len(need_detail)}] Invoice {inv['invoice_number']}: "
                        f"no P.IVA found on detail page"
                    )
            except Exception as e:
                failed_count += 1
                logger.warning(
                    f"[{i+1}/{len(need_detail)}] Failed to fetch detail for "
                    f"{inv['invoice_number']}: {e}"
                )

            # Throttle requests
            if i < len(need_detail) - 1:
                time.sleep(delay)

            # Progress log every 25 invoices
            if (i + 1) % 25 == 0:
                logger.info(
                    f"P.IVA enrichment progress: {i+1}/{len(need_detail)} "
                    f"({enriched_count} found, {failed_count} failed)"
                )

        logger.info(
            f"P.IVA enrichment complete: {enriched_count}/{len(need_detail)} enriched, "
            f"{failed_count} failed"
        )
        return invoices

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
