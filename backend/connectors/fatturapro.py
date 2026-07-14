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

    def fetch_overdue_invoices(self) -> tuple[List[Dict[str, Any]], bool]:
        """Fetch all overdue invoices from FatturaPro.

        Scrapes the documenti.php?s=1 page ("Da incassare" / invoices to collect).
        First parses the initial page HTML, then paginates via xcrud AJAX.

        Il mapping delle colonne viene derivato UNA volta dall'header della
        pagina iniziale e riusato per i frammenti AJAX (che spesso non hanno
        l'header): così un'eventuale colonna in più — es. Scadenza — non
        disallinea gli importi delle pagine successive.

        Returns:
            (invoices, partial) — partial=True quando il fetch NON è
            certamente completo (chiave xcrud mancante, errore xcrud,
            eccezione a metà paginazione, batch anomalo scartato).
            Con partial=True il chiamante NON deve fare payment detection:
            una fattura assente da una lista incompleta non è pagata.

            Invoice dict keys: invoice_number, date, customer_name, total,
            balance, due_date (se la lista ha la colonna Scadenza), doc_id,
            source_platform.
        """
        if not self._authenticated:
            if not self.login():
                logger.error("Cannot fetch invoices: not authenticated")
                return [], True

        logger.info("Fetching overdue invoices from FatturaPro...")
        all_invoices = []
        partial = False
        PAGE_SIZE = 10  # xcrud default

        try:
            # Load the initial page which already contains data
            response = self.client.get(
                f"{self.base_url}/documenti.php?s=1",
                timeout=self.timeout,
            )
            response.raise_for_status()

            # Derive the column map from the initial page header, reuse it
            # for all AJAX fragments of this run.
            colmap = self._derive_column_map(response.text)

            # Parse the initial page
            initial_invoices = self._parse_invoice_table(response.text, colmap)
            all_invoices.extend(initial_invoices)
            logger.info(f"Page 1: {len(initial_invoices)} invoices")

            # Get xcrud key for pagination (from hidden input)
            soup = BeautifulSoup(response.text, "html.parser")
            key_input = soup.find("input", {"name": "key", "type": "hidden"})
            if not key_input:
                if len(initial_invoices) >= PAGE_SIZE:
                    # Ci sono quasi certamente altre pagine che non possiamo leggere
                    logger.warning("No xcrud key found with a full first page — PARTIAL fetch")
                    return all_invoices, True
                logger.info(f"All invoices fit on one page ({len(initial_invoices)})")
                return all_invoices, False

            xcrud_key = key_input.get("value")

            # Check if there are more pages
            if len(initial_invoices) < PAGE_SIZE:
                logger.info(f"All invoices fit on one page ({len(initial_invoices)})")
                return all_invoices, False

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
                    logger.warning(f"xcrud error at page {page}, stopping pagination — PARTIAL fetch")
                    partial = True
                    break

                batch = self._parse_invoice_table(ajax_resp.text, colmap)
                if not batch:
                    logger.debug(f"Page {page}: empty — pagination complete")
                    break

                # Sanity guard: un batch con tutti i saldi a zero o tutte le
                # date non parsabili indica colonne disallineate o HTML
                # inatteso → scartarlo è più sicuro che importarlo.
                if len(batch) >= 3 and (
                    all((inv.get("balance") or 0) == 0 for inv in batch)
                    or all(inv.get("date") is None for inv in batch)
                ):
                    logger.warning(
                        f"Page {page}: anomalous batch (all-zero balances or "
                        f"unparseable dates) — discarded, PARTIAL fetch"
                    )
                    partial = True
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

            logger.info(
                f"Fetched {len(all_invoices)} overdue invoices total"
                f"{' (PARTIAL)' if partial else ''}"
            )
            return all_invoices, partial

        except Exception as e:
            logger.error(f"Error fetching overdue invoices: {e}", exc_info=True)
            return all_invoices, True  # Return what we have, flagged as partial

    def _derive_column_map(self, html: str) -> Dict[str, int]:
        """Mappa nome-colonna → indice, derivata dall'header della tabella.

        Fallback al layout storico (Documento, Data, Destinatario, Totale,
        Saldo) se l'header non è riconoscibile.
        """
        default = {"documento": 0, "data": 1, "destinatario": 2, "totale": 3, "saldo": 4}
        try:
            soup = BeautifulSoup(html, "html.parser")
            header_cells = []
            for row in soup.find_all("tr"):
                ths = row.find_all("th")
                if ths:
                    header_cells = [th.get_text(strip=True).lower() for th in ths]
                    break
            if not header_cells:
                return default

            colmap = {}
            for idx, text in enumerate(header_cells):
                if "document" in text and "documento" not in colmap:
                    colmap["documento"] = idx
                elif "scadenz" in text:
                    colmap["scadenza"] = idx
                elif text.startswith("data") and "data" not in colmap:
                    colmap["data"] = idx
                elif "destinatar" in text or "cliente" in text:
                    colmap["destinatario"] = idx
                elif "total" in text:
                    colmap["totale"] = idx
                elif "saldo" in text or "residuo" in text:
                    colmap["saldo"] = idx

            required = {"documento", "data", "destinatario", "totale", "saldo"}
            if not required.issubset(colmap):
                logger.debug(f"Header incompleto {header_cells}, uso layout di default")
                return default
            if "scadenza" in colmap:
                logger.info(f"Colonna Scadenza trovata nella lista (indice {colmap['scadenza']})")
            return colmap
        except Exception as e:
            logger.warning(f"Errore derivando il mapping colonne: {e}")
            return default

    def _parse_invoice_table(
        self, html: str, colmap: Optional[Dict[str, int]] = None
    ) -> List[Dict[str, Any]]:
        """Parse invoice data from xcrud HTML table response.

        Args:
            html: HTML response from xcrud_ajax.php
            colmap: mapping colonna→indice derivato dalla pagina iniziale
                    (i frammenti AJAX spesso non hanno l'header). Default:
                    layout storico Documento, Data, Destinatario, Totale, Saldo.

        Returns:
            List of parsed invoice dictionaries
        """
        invoices = []
        if colmap is None:
            colmap = {"documento": 0, "data": 1, "destinatario": 2, "totale": 3, "saldo": 4}
        min_cells = max(colmap.values()) + 1

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
                    if len(cells) < min_cells:
                        logger.debug(f"Row has {len(cells)} cells, expected >= {min_cells}, skipping")
                        continue

                    invoice_number = cells[colmap["documento"]].get_text(strip=True)
                    date_str = cells[colmap["data"]].get_text(strip=True)
                    customer_name = cells[colmap["destinatario"]].get_text(strip=True)
                    total_str = cells[colmap["totale"]].get_text(strip=True)
                    balance_str = cells[colmap["saldo"]].get_text(strip=True)

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

                    # Scadenza reale, se la lista ha la colonna
                    if "scadenza" in colmap and len(cells) > colmap["scadenza"]:
                        due_str = cells[colmap["scadenza"]].get_text(strip=True)
                        if due_str:
                            try:
                                invoice["due_date"] = datetime.strptime(due_str, "%d/%m/%Y").date()
                            except ValueError:
                                pass

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

        Estrae dalla pagina di dettaglio (form di edit) la P.IVA del
        destinatario e — quando presente — la SCADENZA reale della fattura.

        Guardie sulla P.IVA (la corruzione di questo campo in passato ha
        convogliato fatture di clienti diversi sullo stesso profilo):
        - checksum ufficiale (piva.validate_piva): invalida = scartata;
        - blacklist COMPANY_PIVA: la P.IVA di Sake Company compare su ogni
          fattura come venditore, non è mai quella del destinatario;
        - il pattern full-text (il più fragile) è DISABILITATO se
          COMPANY_PIVA non è configurata (fail-closed).

        Returns:
            Dict with doc_id, piva (validated), piva_source
            (field/label/fulltext), due_date (date) — or None if failed
        """
        from backend.engine.piva import validate_piva

        try:
            logger.debug(f"Fetching invoice detail for doc_id: {doc_id}")

            # Construct detail page URL
            detail_url = f"{self.base_url}/documenti.php?id={doc_id}&action=edit"

            response = self.client.get(detail_url, timeout=self.timeout)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            detail = {"doc_id": doc_id}

            def _accept_piva(raw: str, source: str) -> bool:
                """Valida e registra la P.IVA se supera le guardie."""
                validated = validate_piva(raw)
                if not validated:
                    if raw and raw.strip():
                        logger.warning(
                            f"Invalid P.IVA for doc {doc_id} from {source}: '{raw}' — discarded"
                        )
                    return False
                if config.COMPANY_PIVA and validated == config.COMPANY_PIVA:
                    logger.warning(
                        f"P.IVA for doc {doc_id} from {source} is COMPANY_PIVA "
                        f"(venditore, non destinatario) — discarded"
                    )
                    return False
                detail["piva"] = validated
                detail["piva_source"] = source
                return True

            # Pattern 1: Form field with name containing "piva"/"partitaiva"
            # (con o senza underscore: xcrud usa nomi tipo "documenti.PartitaIva")
            piva_input = soup.find(["input", "textarea"], {
                "name": re.compile(r"(piva|partita_?iva|p\.iva|pivanumber)", re.IGNORECASE)
            })
            if piva_input:
                _accept_piva(piva_input.get("value", ""), "field")

            # Pattern 2: Table cell or label with P.IVA
            if not detail.get("piva"):
                piva_label = soup.find(text=re.compile(r"P\.?\s*IVA", re.IGNORECASE))
                if piva_label:
                    parent = piva_label.parent
                    # Try next sibling elements
                    for sibling in parent.find_next_siblings():
                        text = sibling.get_text(strip=True)
                        if text and not re.match(r"^P\.?\s*IVA", text, re.IGNORECASE):
                            if _accept_piva(text, "label"):
                                break
                    # Also try parent's next sibling (common in <td>P.IVA</td><td>VALUE</td>)
                    if not detail.get("piva"):
                        next_td = parent.find_next("td")
                        if next_td:
                            _accept_piva(next_td.get_text(strip=True), "label")

            # Pattern 3 (full-text): il più soggetto a catturare la P.IVA
            # sbagliata (es. quella del venditore nel footer). Attivo SOLO
            # con COMPANY_PIVA configurata, che permette di scartarla.
            if not detail.get("piva") and config.COMPANY_PIVA:
                full_text = soup.get_text()
                for piva_match in re.finditer(
                    r'P\.?\s*IVA\s*[:/]?\s*([A-Z]{0,3}\d{8,15})',
                    full_text,
                    re.IGNORECASE,
                ):
                    if _accept_piva(piva_match.group(1), "fulltext"):
                        break

            # ── Scadenza reale ──────────────────────────────────────────
            # Pattern A: campo form con nome contenente "scaden"
            due_input = soup.find(["input", "select"], {
                "name": re.compile(r"scaden", re.IGNORECASE)
            })
            if due_input and due_input.get("value"):
                detail["due_date"] = self._parse_detail_date(due_input.get("value"))

            # Pattern B: label/td "Scadenza" seguita dal valore
            if not detail.get("due_date"):
                due_label = soup.find(text=re.compile(r"Scadenza", re.IGNORECASE))
                if due_label:
                    parent = due_label.parent
                    for sibling in parent.find_next_siblings():
                        parsed = self._parse_detail_date(sibling.get_text(strip=True))
                        if parsed:
                            detail["due_date"] = parsed
                            break
                    if not detail.get("due_date"):
                        next_td = parent.find_next("td")
                        if next_td:
                            detail["due_date"] = self._parse_detail_date(
                                next_td.get_text(strip=True)
                            )

            # Pattern C: regex sul testo "Scadenza: gg/mm/aaaa"
            if not detail.get("due_date"):
                text_match = re.search(
                    r"Scadenza\s*[:\-]?\s*(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})",
                    soup.get_text(),
                    re.IGNORECASE,
                )
                if text_match:
                    detail["due_date"] = self._parse_detail_date(text_match.group(1))

            logger.debug(f"Invoice detail: {detail}")
            return detail

        except Exception as e:
            logger.error(f"Error fetching invoice detail for {doc_id}: {e}")
            return None

    @staticmethod
    def _parse_detail_date(raw: str):
        """Parse a date string from the detail page (dd/mm/yyyy or ISO)."""
        if not raw:
            return None
        raw = raw.strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    def enrich_invoices_from_detail(
        self,
        invoices: List[Dict[str, Any]],
        delay: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """Enrich invoices with P.IVA and real due date from detail pages.

        Un solo fetch di dettaglio per fattura riempie sia "customer_piva"
        che "due_date" (se non già presenti). Piccolo delay tra le richieste
        per non sovraccaricare FatturaPro.

        Guardia anti-ripetizione: se la STESSA P.IVA emerge dal pattern
        full-text per destinatari con nomi diversi nella stessa run, è
        quasi certamente un valore fisso della pagina (es. il venditore) e
        viene revocata da tutte le fatture coinvolte.
        """
        import time

        need_detail = [
            inv for inv in invoices
            if inv.get("doc_id") and (not inv.get("customer_piva") or not inv.get("due_date"))
        ]
        if not need_detail:
            logger.info("No invoices need detail enrichment")
            return invoices

        logger.info(f"Enriching {len(need_detail)} invoices from detail pages...")
        piva_count = 0
        due_count = 0
        failed_count = 0
        # P.IVA da pattern full-text → set dei nomi destinatario incontrati
        fulltext_piva_names: Dict[str, set] = {}

        for i, inv in enumerate(need_detail):
            try:
                detail = self.fetch_invoice_detail(inv["doc_id"])
                if detail:
                    if detail.get("piva") and not inv.get("customer_piva"):
                        inv["customer_piva"] = detail["piva"]
                        inv["piva_source"] = detail.get("piva_source")
                        piva_count += 1
                        if detail.get("piva_source") == "fulltext":
                            names = fulltext_piva_names.setdefault(detail["piva"], set())
                            names.add((inv.get("customer_name") or "").strip().lower())
                    if detail.get("due_date") and not inv.get("due_date"):
                        inv["due_date"] = detail["due_date"]
                        due_count += 1
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
                    f"Detail enrichment progress: {i+1}/{len(need_detail)} "
                    f"({piva_count} P.IVA, {due_count} scadenze, {failed_count} failed)"
                )

        # Guardia anti-ripetizione sul pattern full-text
        repeated = {
            piva for piva, names in fulltext_piva_names.items() if len(names) > 2
        }
        if repeated:
            revoked = 0
            for inv in need_detail:
                if inv.get("customer_piva") in repeated and inv.get("piva_source") == "fulltext":
                    inv.pop("customer_piva", None)
                    inv.pop("piva_source", None)
                    revoked += 1
            logger.warning(
                f"P.IVA full-text ripetute su destinatari diversi {sorted(repeated)}: "
                f"revocate da {revoked} fatture (probabile valore fisso della pagina)"
            )
            piva_count -= revoked

        logger.info(
            f"Detail enrichment complete: {piva_count} P.IVA, {due_count} scadenze reali, "
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
