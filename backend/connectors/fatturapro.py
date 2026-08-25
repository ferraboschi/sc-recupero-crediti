"""FatturaPro connector for fetching overdue invoices via web scraping.

FatturaPro has no public documented API, so we use authenticated sessions
with BeautifulSoup4 for parsing HTML responses from the xcrud AJAX framework.
"""

import logging
import re
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from backend.config import config

logger = logging.getLogger(__name__)


def doc_key(raw: str) -> str:
    """Chiave canonica YEAR-AWARE per il JOIN fattura↔scadenzario.

    Lo scadenzario elenca "1170/SAK del 24/06/2026" (anno nella data 'del' =
    emissione), la lista fatture "2026/00001170/SAK - Fattura" (anno in testa):
    entrambe collassano su "2026/1170/SAK".

    L'ANNO È PARTE DELLA CHIAVE. La numerazione riparte ogni anno, quindi
    "2025/00001438/SAK" e "2026/00001438/SAK" sono fatture DIVERSE. Scartando
    l'anno collidevano e — con lo scadenzario che tiene la scadenza aperta più
    VECCHIA — la fattura nuova (2026) ereditava la scadenza della vecchia
    omonima (2025), risultando scaduta da un anno. Pericoloso: faceva partire
    solleciti su fatture non ancora scadute.

    Se l'anno non è ricavabile da nessuna delle due forme, si ricade sulla
    chiave senza anno (retrocompatibile): un eventuale mancato match degrada a
    scadenza 'assumed', mai a una data di un altro anno.
    """
    s = str(raw or "")
    # Anno dalla data 'del GG/MM/AAAA' (forma scadenzario), se presente.
    year = None
    split = re.split(r"\s+del\s+", s, flags=re.IGNORECASE)
    if len(split) > 1:
        m = re.search(r"\b\d{1,2}/\d{1,2}/(\d{4})\b", split[1])
        if m:
            year = m.group(1)
    head = split[0]
    head = re.sub(r"\s*[-–]\s*(Fattura|Nota.*|Ricevuta).*", "", head, flags=re.IGNORECASE).strip()
    parts = [p.strip() for p in head.split("/") if p.strip()]
    numeric = [p for p in parts if p.isdigit()]
    suffix = next((p.upper() for p in parts if re.search(r"[A-Za-z]", p)), "")

    def _is_year(p: str) -> bool:
        # Anno "nudo" a 4 cifre (1900-2099); il progressivo zero-paddato
        # ("00001093", 8 char) non viene scambiato per un anno.
        return len(p) == 4 and 1900 <= int(p) <= 2099

    # Anno dal gruppo-anno in testa (forma lista fatture), se non già trovato.
    if year is None:
        year = next((p for p in numeric if _is_year(p)), None)

    candidates = [p for p in numeric if not _is_year(p)] or numeric
    if not candidates:
        base = head.upper()
        return f"{year}/{base}" if year else base
    # Il progressivo è il gruppo (rimasto) con la stringa più lunga
    prog = max(candidates, key=len)
    num = str(int(prog))
    core = f"{num}/{suffix}" if suffix else num
    return f"{year}/{core}" if year else core


def _it_date_to_date(s: str) -> Optional[date]:
    """'gg/mm/aaaa' → date, o None."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(s or ""))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


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
        # Contatore righe-fattura scartate dall'ultimo _parse_invoice_table
        self._last_parse_drops = 0

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

    def _fetch_list_page(
        self, xcrud_key: str, colmap: Dict[str, int], start: int, limit: int,
        orderby: str = "documenti.NumeroSezionale",
    ) -> Tuple[List[Dict[str, Any]], Optional[str], int, Optional[str]]:
        """Una pagina della lista fatture via xcrud AJAX.

        `orderby` DEFAULT `documenti.NumeroSezionale`, e non è un dettaglio: è
        il numero fattura progressivo, l'UNICA colonna univoca della lista
        documenti (le altre — Data, Destinatario, ImportoTotaleDocumento, Saldo
        — hanno duplicati). Ordinare su una chiave univoca e totale è ciò che
        permette alla finestra offset di piastrellare senza buchi né
        ripetizioni ANCHE quando il server tronca (clampa) il limit: una pagina
        più corta del richiesto DIMOSTRA allora la fine della lista.
          NON lasciare `orderby` vuoto: la tesi "orderby vuoto = PK univoca" è
        FALSA per la lista documenti. Verificato sul FatturaPro reale, la
        pagina `documenti.php?s=1` ha come ordinamento di default `↓ Data`
        (`documenti.Data`), che NON è univoca: i pari-data possono uscire in
        ordine diverso a ogni query, la finestra scivola, e sotto il clamp del
        server la completezza non è più dimostrabile → partial per sempre (era
        il freeze del rilevamento pagamenti in produzione). Si forza dunque
        `documenti.NumeroSezionale`, la colonna univoca reale.

        Returns (batch, new_key, drops, failure). `failure` è None solo se la
        risposta è un frammento xcrud valido; altrimenti è il motivo. Chi
        chiama NON deve mai leggere un fallimento come fine della lista: una
        sessione scaduta a metà restituisce la pagina di login con status 200
        e zero righe, che è indistinguibile da "finito" se non si guarda.
        """
        data = {
            "xcrud[key]": xcrud_key,
            "xcrud[orderby]": orderby,
            "xcrud[start]": str(start),
            "xcrud[limit]": str(limit),
            "xcrud[instance]": "documenti",
            "xcrud[task]": "list",
        }
        # `order` ha senso solo con un orderby esplicito. Col default
        # `documenti.NumeroSezionale` viene inviato `desc`: la direzione è
        # indifferente, qualsiasi ordine TOTALE su una chiave univoca
        # piastrella la finestra offset. (Se un chiamante passa orderby vuoto
        # non lo invia — ma la lista fatture non lo fa più.)
        if orderby:
            data["xcrud[order]"] = "desc"
        resp = self.client.post(
            f"{self.base_url}/xcrud/xcrud_ajax.php",
            data=data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/documenti.php?s=1",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()

        if "xcrud-error" in resp.text:
            return [], None, 0, "xcrud-error"

        batch = self._parse_invoice_table(resp.text, colmap)
        drops = self._last_parse_drops
        if not batch and self._looks_like_auth_page(resp):
            return [], None, drops, "session-expired"

        soup = BeautifulSoup(resp.text, "html.parser")
        new_key = soup.find("input", {"name": "key", "type": "hidden"})
        return batch, (new_key.get("value") if new_key else None), drops, None

    @staticmethod
    def _looks_anomalous(batch: List[Dict[str, Any]]) -> bool:
        """Batch con tutti i saldi a zero o tutte le date non parsabili:
        indica colonne disallineate o HTML inatteso → scartarlo è più sicuro
        che importarlo."""
        return len(batch) >= 3 and (
            all((inv.get("balance") or 0) == 0 for inv in batch)
            or all(inv.get("date") is None for inv in batch)
        )

    def fetch_overdue_invoices(self) -> tuple[List[Dict[str, Any]], bool]:
        """Fetch all overdue invoices from FatturaPro.

        Scrapes the documenti.php?s=1 page ("Da incassare" / invoices to collect).
        La pagina iniziale serve per il column map e la chiave xcrud; le righe
        arrivano da UNA sola query AJAX.

        Il mapping delle colonne viene derivato UNA volta dall'header della
        pagina iniziale e riusato per i frammenti AJAX (che spesso non hanno
        l'header): così un'eventuale colonna in più — es. Scadenza — non
        disallinea gli importi.

        ORDINAMENTO STABILE E UNIVOCO: `documenti.NumeroSezionale`. La
        paginazione xcrud passa `xcrud[orderby]=documenti.NumeroSezionale` — il
        numero fattura progressivo, l'UNICA colonna univoca della lista. Su una
        chiave univoca e totale la finestra offset piastrella la lista senza
        buchi né ripetizioni ANCHE quando il server tronca (clampa) il limit
        (come per le 1251 righe di clienti.php che `_paginate_xcrud_list`
        scarica per intero a pagine da 100), e una pagina più corta del
        richiesto DIMOSTRA la fine — a prova di clamp.
          Attenzione: NON basta lasciare `orderby` vuoto. La tesi "orderby
        vuoto = PK univoca" (PR #14) è FALSA per la lista documenti — verificato
        sul FatturaPro reale: il default della pagina è `↓ Data`
        (`documenti.Data`), NON univoca. Su un ordinamento non totale il DB è
        libero di restituire i pari-data in ordine diverso a ogni query: con
        l'offset la finestra scivola, RIPETE righe su una pagina e ne SALTA
        altre. In produzione il server clampa il limit (una pagina non basta
        mai), quindi si cadeva SEMPRE nella paginazione a offset, la finestra
        scivolava a ogni giro e la completezza non era mai dimostrabile →
        `partial=True` a ogni sync → la payment detection non partiva mai → lo
        scaduto solo cresceva. È il difetto che questo fix chiude forzando
        `documenti.NumeroSezionale`: caso Belfiore 655/2026 e "551 agg …
        (PARZIALE)" perenne.
          RETE DI SICUREZZA (non rimossa). Anche con l'orderby stabile la
        deduplica per invoice_number resta accesa nel ripiego a offset: se —
        contro l'atteso — `documenti.NumeroSezionale` NON fosse univoco, una
        finestra che scivola RIPETE una riga (e per pigeonhole, su una pagina
        finale corta, un salto ne FORZA la ripetizione). Il duplicato viene
        rilevato → `partial=True`. Quindi il caso peggiore degrada allo stallo
        onesto di oggi, MAI a una fattura marcata 'paid' per errore.

        Returns:
            (invoices, partial) — partial=True quando il fetch NON è
            certamente completo (chiave xcrud mancante, errore xcrud,
            eccezione a metà, batch anomalo scartato, completezza non
            dimostrabile, finestra scivolata nel ripiego).
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
        seen_numbers = set()
        partial = False
        dropped_rows = 0
        # Righe che il gestionale rende da solo nella pagina HTML (paginazione
        # sua, non nostra): serve SOLO a capire se quella pagina basta quando
        # la chiave xcrud manca e non possiamo chiedere di più.
        RENDERED_PAGE_SIZE = 10
        # Limite della pagina unica. La lista "Da incassare" vale ~674k EUR e
        # sta in ~1.300 righe: 5.000 lascia ~4x di margine di crescita senza
        # chiedere al server una tabella smisurata. Non è una soglia di
        # sicurezza — se un giorno non bastasse, la sonda qui sotto se ne
        # accorge e si ripiega: sbagliarlo costa una richiesta in più, non un
        # credito perso.
        FETCH_LIMIT = 5000
        PROBE_LIMIT = 10

        def _add_batch(batch):
            """Accumula deduplicando per invoice_number.

            Sul percorso normale (pagina unica) non ci sono confini di pagina
            e quindi nemmeno duplicati: la deduplica non costa nulla e resta
            come rete. Nel ripiego a offset il ritorno `added` è invece la
            spia dello scivolamento — vedi sotto.
            """
            added = 0
            for inv in batch:
                num = inv.get("invoice_number")
                if num in seen_numbers:
                    continue
                seen_numbers.add(num)
                all_invoices.append(inv)
                added += 1
            return added

        try:
            # Load the initial page: serve per il column map e la chiave
            # xcrud. Le RIGHE renderizzate non vengono usate quando la
            # paginazione AJAX è disponibile: la pagina renderizzata usa
            # l'ordinamento di default del sito, le pagine AJAX quello
            # imposto dal codice — un mismatch faceva saltare
            # deterministicamente una finestra di fatture a ogni sync.
            response = self.client.get(
                f"{self.base_url}/documenti.php?s=1",
                timeout=self.timeout,
            )
            response.raise_for_status()

            # Derive the column map from the initial page header, reuse it
            # for all AJAX fragments of this run.
            colmap = self._derive_column_map(response.text)

            # Get xcrud key for pagination (from hidden input)
            soup = BeautifulSoup(response.text, "html.parser")
            key_input = soup.find("input", {"name": "key", "type": "hidden"})
            if not key_input:
                # Fallback: senza chiave si può leggere solo la pagina
                # renderizzata, con la paginazione del gestionale.
                initial_invoices = self._parse_invoice_table(response.text, colmap)
                dropped_rows += self._last_parse_drops
                _add_batch(initial_invoices)
                if len(initial_invoices) >= RENDERED_PAGE_SIZE:
                    # Ci sono quasi certamente altre pagine che non possiamo leggere
                    logger.warning("No xcrud key found with a full first page — PARTIAL fetch")
                    return all_invoices, True
                logger.info(f"All invoices fit on one page ({len(initial_invoices)})")
                return all_invoices, dropped_rows > 0

            xcrud_key = key_input.get("value")

            # ── La lista in una pagina sola ──
            # Una query, start=0, limite ampio: se il server lo onora, tutta la
            # lista arriva senza offset. Se lo clampa (produzione), la sonda qui
            # sotto se ne accorge e si ripiega sulla paginazione — che ora è
            # stabile perché `_fetch_list_page` ordina su `documenti.NumeroSezionale`
            # (colonna univoca reale), non su `documenti.Data`.
            batch, new_key, drops, failure = self._fetch_list_page(
                xcrud_key, colmap, 0, FETCH_LIMIT
            )
            dropped_rows += drops
            if failure:
                logger.warning(f"Invoice list unavailable ({failure}) — PARTIAL fetch")
                return all_invoices, True
            if self._looks_anomalous(batch):
                logger.warning(
                    "Anomalous invoice list (all-zero balances or unparseable "
                    "dates) — discarded, PARTIAL fetch"
                )
                return all_invoices, True
            if new_key:
                xcrud_key = new_key

            # ── La sonda: è DAVVERO tutta la lista? ──
            # Una pagina più corta del limite chiesto NON dimostra la fine
            # della lista: il server potrebbe aver troncato il limit in
            # silenzio, e dichiarare "completa" una lista mozza è esattamente
            # ciò che marca pagate le fatture non lette. Si chiede la riga
            # successiva oltre l'ultima letta: se la sonda non porta NULLA DI
            # NUOVO, la lista è finita — dimostrato, non supposto. Costa una
            # richiesta.
            #   Attenzione a cosa conta come "nulla di nuovo": sul FatturaPro
            # reale (verificato 2026-08-18 con richieste dirette) il server NON
            # clampa il limit — lo ONORA, e le 549 righe arrivano in questa
            # unica pagina. Ma IGNORA uno `start` oltre la fine della lista:
            # invece della pagina vuota che proverebbe la fine, rispedisce le
            # PRIME righe (duplicati, probe_overlap 10/10 — mai numeri nuovi).
            # Perciò "sonda vuota" e "sonda di soli duplicati" provano ENTRAMBE
            # la fine; solo un numero fattura NUOVO nella sonda dimostra che
            # oltre `batch` c'è dell'altro. Confondere i due era il difetto:
            # ripiego a offset ad ogni sync → slittamento → partial=True perenne
            # → payment detection mai eseguita (Speranzina 952 mai uscita da
            # "Da incassare").
            probe, _, _, probe_failure = self._fetch_list_page(
                xcrud_key, colmap, len(batch), PROBE_LIMIT
            )
            if probe_failure:
                # Le righe lette restano buone (vanno create), ma la
                # completezza non è dimostrabile: niente chiusure, niente paid.
                logger.warning(f"Completeness probe failed ({probe_failure}) — PARTIAL fetch")
                _add_batch(batch)
                return all_invoices, True

            # La sonda porta almeno un numero fattura NON già in `batch`?
            # Se no (vuota, o soli duplicati perché il server ha ignorato lo
            # `start` oltre la fine), la pagina unica È la lista completa.
            batch_numbers = {inv.get("invoice_number") for inv in batch}
            probe_has_new = any(
                inv.get("invoice_number") not in batch_numbers for inv in probe
            )
            if not probe_has_new:
                _add_batch(batch)
                if probe:
                    logger.info(
                        f"Fetched {len(all_invoices)} overdue invoices in a "
                        f"single page (probe returned {len(probe)} already-seen "
                        f"rows — the server ignored the offset past end-of-list, "
                        f"list is complete)"
                    )
                else:
                    logger.info(f"Fetched {len(all_invoices)} overdue invoices in a single page")
                return all_invoices, dropped_rows > 0

            # ── Ripiego: la pagina unica non è bastata ──
            # O il server ha troncato il limit, o la lista supera FETCH_LIMIT.
            # Per la sicurezza le due cose sono la stessa (la lista non è
            # intera); per i dati no: le fatture vanno CREATE comunque, o
            # tornano invisibili — e `partial` blocca solo chiusure e paid,
            # non la creazione. Quindi si legge il resto nell'unico modo
            # possibile, la paginazione a offset, con il rilevatore acceso.
            # La pagina è quella che il server ci ha concesso: se ha troncato
            # a 100, si va di 100 in 100.
            page_size = len(batch) or RENDERED_PAGE_SIZE
            logger.warning(
                f"Single page returned {len(batch)} rows for a requested limit "
                f"of {FETCH_LIMIT}, but more rows exist — falling back to "
                f"offset pagination with page_size={page_size}"
            )
            _add_batch(batch)
            start = len(batch)
            page = 1
            max_pages = 200  # Safety limit

            while True:
                if page > max_pages:
                    logger.warning(
                        f"Reached max_pages={max_pages} without a natural "
                        f"end — PARTIAL fetch"
                    )
                    partial = True
                    break

                batch, new_key, drops, failure = self._fetch_list_page(
                    xcrud_key, colmap, start, page_size
                )
                dropped_rows += drops
                if failure:
                    logger.warning(
                        f"Page {page}: pagination stopped ({failure}) — PARTIAL fetch"
                    )
                    partial = True
                    break
                if not batch:
                    logger.debug(f"Page {page}: empty — pagination complete")
                    break
                if self._looks_anomalous(batch):
                    logger.warning(
                        f"Page {page}: anomalous batch (all-zero balances or "
                        f"unparseable dates) — discarded, PARTIAL fetch"
                    )
                    partial = True
                    break

                added = _add_batch(batch)
                if new_key:
                    xcrud_key = new_key

                # Il rilevatore di scivolamento. Una riga già vista che
                # ricompare a un offset più avanti significa che la lista si è
                # mossa sotto di noi — e una finestra che ripete una riga ne
                # sta saltando un'altra, in egual numero. Un duplicato non può
                # essere legittimo: il numero documento porta anno,
                # progressivo e serie ("2026/00001170/SAK - Fattura"), e la
                # lista ha una riga per documento (le rate stanno nello
                # scadenzario). Quindi il duplicato è una prova, non un
                # sospetto. Si continua a raccogliere — le righe lette servono
                # comunque — ma il fetch è parziale.
                if added < len(batch):
                    logger.warning(
                        f"Page {page}: {len(batch) - added} rows already seen "
                        f"at start={start} — the list shifted under us, so it "
                        f"is also skipping rows — PARTIAL fetch"
                    )
                    partial = True

                if page % 10 == 0:
                    logger.info(f"Page {page}: {len(batch)} invoices (running total: {len(all_invoices)})")

                if len(batch) < page_size:
                    logger.debug(f"Page {page}: {len(batch)} invoices (last page)")
                    break

                start += page_size
                page += 1

            if dropped_rows:
                # Righe che sembravano fatture ma non sono state parsate:
                # una lista con buchi non deve mai passare per completa
                # (la payment detection marcherebbe pagate le fatture perse).
                logger.warning(
                    f"{dropped_rows} invoice-like rows dropped during "
                    f"parsing — PARTIAL fetch"
                )
                partial = True

            logger.info(
                f"Fetched {len(all_invoices)} overdue invoices total"
                f"{' (PARTIAL)' if partial else ''}"
            )
            return all_invoices, partial

        except Exception as e:
            logger.error(f"Error fetching overdue invoices: {e}", exc_info=True)
            return all_invoices, True  # Return what we have, flagged as partial

    # ── Liste ausiliarie: scadenzario (scadenze reali) + anagrafica (P.IVA/contatti) ──
    # Sostituiscono lo scraping del form di dettaglio (nomi campo Base64,
    # richiesta xcrud stateful non replicabile): due liste xcrud semplici,
    # paginate come la lista fatture. Approccio provato in produzione da
    # SC-order-app.

    @staticmethod
    def _parse_xcrud_rows(html: str) -> List[List[str]]:
        """Righe di una tabella xcrud → liste di celle (testo)."""
        out = []
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.find_all("tr"):
            if row.find("th"):
                continue
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if cells:
                out.append(cells)
        return out

    @staticmethod
    def _xcrud_tokens(html: str) -> Tuple[Optional[str], Optional[str]]:
        """Estrae (key, instance) dai campi hidden di una pagina xcrud.

        Per clienti.php e scadenzario.php SIA key SIA instance sono hash
        di sessione per-pagina (NON il nome letterale della tabella): vanno
        letti dai hidden e riportati alla richiesta AJAX successiva.
        """
        soup = BeautifulSoup(html, "html.parser")
        key_el = soup.find("input", {"name": "key"})
        inst_el = soup.find("input", {"name": "instance"})
        key = key_el.get("value") if key_el else None
        instance = inst_el.get("value") if inst_el else None
        return key, instance

    def _paginate_xcrud_list(
        self, path: str, page_size: int = 100, max_pages: int = 100,
    ) -> Tuple[List[List[str]], bool]:
        """Scarica TUTTE le righe di una lista xcrud (es. scadenzario.php,
        clienti.php), paginando via key+instance letti dai hidden.

        Returns (rows, complete): complete=False se il fetch si è interrotto
        (chiave mancante, errore xcrud, login scaduto a metà).
        """
        rows: List[List[str]] = []
        first = self.client.get(f"{self.base_url}/{path}", timeout=self.timeout)
        first.raise_for_status()
        if self._looks_like_auth_page(first):
            logger.warning(f"{path}: session expired — cannot fetch")
            return rows, False
        rows.extend(self._parse_xcrud_rows(first.text))
        key, instance = self._xcrud_tokens(first.text)
        if not key or not instance:
            # Nessuna paginazione possibile: se la prima pagina è piena,
            # mancano righe.
            complete = len(rows) < page_size
            return rows, complete

        start = 0
        for _ in range(max_pages):
            resp = self.client.post(
                f"{self.base_url}/xcrud/xcrud_ajax.php",
                data={
                    "xcrud[key]": key,
                    "xcrud[instance]": instance,
                    "xcrud[orderby]": "",
                    "xcrud[start]": str(start),
                    "xcrud[limit]": str(page_size),
                    "xcrud[task]": "list",
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{self.base_url}/{path}",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            if "xcrud-error" in resp.text:
                logger.warning(f"{path}: xcrud error during pagination — PARTIAL")
                return rows, False
            batch = self._parse_xcrud_rows(resp.text)
            if not batch:
                if self._looks_like_auth_page(resp):
                    logger.warning(f"{path}: session expired mid-pagination — PARTIAL")
                    return rows, False
                break
            rows.extend(batch)
            nk, ni = self._xcrud_tokens(resp.text)
            if nk:
                key = nk
            if ni:
                instance = ni
            if len(batch) < page_size:
                break
            start += page_size
        else:
            logger.warning(f"{path}: reached max_pages — PARTIAL")
            return rows, False
        return rows, True

    def fetch_scadenze_map(
        self, target_keys: Optional[set] = None,
        max_pages: int = 400, patience: int = 20,
    ) -> Tuple[Dict[str, date], bool]:
        """Scadenze reali dallo scadenzario → {doc_key: due_date}.

        Colonne: Scadenza · Proroga · Documento · Cliente · Modalità ·
        Banca · Iban · Importo · Sospeso. Una riga per rata.
        - la Proroga (col 1) sovrascrive la Scadenza (col 0);
        - le rate con Sospeso == 0 (saldate) vengono ignorate;
        - per ogni fattura si tiene la scadenza aperta più VECCHIA.

        Lo scadenzario storico è ENORME (>40k righe: una per rata dal 2022,
        saldate incluse) e non ha un filtro "solo aperte". Ma servono solo le
        scadenze delle fatture attualmente da incassare (target_keys, ~600):
        si pagina ordinando per data DESC (le non pagate sono recenti → in
        testa) e ci si ferma quando TUTTE le target sono coperte, o dopo
        `patience` pagine senza un nuovo match target. Così si leggono
        centinaia di righe invece di decine di migliaia, e il fetch risulta
        COMPLETO (le rate più vecchie sono tutte saldate, quindi irrilevanti).
        Senza target_keys si scarre l'intero scadenzario (fallback).

        Returns ({doc_key: date}, complete).
        """
        if not self._authenticated and not self.login():
            return {}, False

        result: Dict[str, date] = {}
        targets = set(target_keys) if target_keys else None
        covered: set = set()
        pages_without_new = 0
        total_rows = 0
        complete = False

        def _ingest(cells_list) -> int:
            """Aggiunge le rate aperte; ritorna quante NUOVE target ha coperto."""
            nonlocal total_rows
            new_targets = 0
            for cells in cells_list:
                total_rows += 1
                if len(cells) < 3:
                    continue
                due = _it_date_to_date(cells[1]) or _it_date_to_date(cells[0])
                if not due:
                    continue
                sospeso = (cells[8] if len(cells) > 8 else "").strip()
                if re.match(r"^0([.,]0+)?$", sospeso):
                    continue  # rata saldata
                k = doc_key(cells[2])
                if not k:
                    continue
                if targets is not None and k not in targets:
                    continue
                if targets is not None and k not in covered:
                    covered.add(k)
                    new_targets += 1
                prev = result.get(k)
                if prev is None or due < prev:
                    result[k] = due
            return new_targets

        try:
            first = self.client.get(f"{self.base_url}/scadenzario.php", timeout=self.timeout)
            first.raise_for_status()
            if self._looks_like_auth_page(first):
                logger.warning("scadenzario.php: session expired — cannot fetch")
                return {}, False
            _ingest(self._parse_xcrud_rows(first.text))
            key, instance = self._xcrud_tokens(first.text)
            if not key or not instance:
                complete = total_rows < 100
                return result, complete

            PAGE = 100
            # Si parte da start=0 così TUTTE le pagine condividono
            # l'ordinamento imposto qui sotto (DESC per data scadenza).
            # Partendo da PAGE, le prime 100 righe di QUELL'ordinamento — le
            # scadenze più lontane, cioè le fatture aperte più recenti — non
            # venivano mai richieste: restavano senza scadenza reale e
            # 'assumed' (emissione+30) le dava per scadute in anticipo.
            # Stesso motivo per cui fetch_overdue_invoices e
            # _paginate_xcrud_list partono da 0.
            # Il re-ingest delle righe già lette dalla pagina renderizzata è
            # innocuo: `result` è un dict per doc_key con merge via min(),
            # `covered` è un set.
            start = 0
            for _ in range(max_pages):
                if targets is not None and covered >= targets:
                    complete = True
                    break
                resp = self.client.post(
                    f"{self.base_url}/xcrud/xcrud_ajax.php",
                    data={
                        "xcrud[key]": key,
                        "xcrud[instance]": instance,
                        "xcrud[orderby]": "scadenze.DataScadenzaPagamento",
                        "xcrud[order]": "desc",
                        "xcrud[start]": str(start),
                        "xcrud[limit]": str(PAGE),
                        "xcrud[task]": "list",
                    },
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": f"{self.base_url}/scadenzario.php",
                    },
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                if "xcrud-error" in resp.text:
                    logger.warning("scadenzario.php: xcrud error during pagination — PARTIAL")
                    break
                batch = self._parse_xcrud_rows(resp.text)
                if not batch:
                    complete = not self._looks_like_auth_page(resp)
                    break
                new_targets = _ingest(batch)
                nk, ni = self._xcrud_tokens(resp.text)
                if nk:
                    key = nk
                if ni:
                    instance = ni
                if len(batch) < PAGE:
                    complete = True
                    break
                # Convergenza: se abbiamo un set target e non troviamo nuove
                # scadenze aperte da `patience` pagine, il resto sono rate
                # saldate → fetch di fatto completo per i nostri scopi.
                if targets is not None:
                    pages_without_new = 0 if new_targets else pages_without_new + 1
                    if pages_without_new >= patience:
                        complete = True
                        break
                start += PAGE
            else:
                logger.warning("scadenzario.php: reached max_pages — PARTIAL")
        except Exception as e:
            logger.error(f"Scadenzario fetch error: {e}")
            return result, False

        cov = f", {len(covered)}/{len(targets)} target covered" if targets is not None else ""
        logger.info(
            f"Scadenzario: {len(result)} invoices with a real due date "
            f"from {total_rows} ledger rows{cov}{'' if complete else ' (PARTIAL)'}"
        )
        return result, complete

    def fetch_clienti_map(self) -> Tuple[Dict[str, Dict[str, Any]], bool]:
        """Anagrafica clienti → {nome_lower: {piva, phone, email}}.

        Colonne: Denominazione · Partita IVA · Codice Fiscale · Indirizzo ·
        Numero Civico · Cap · Comune · Provincia · Telefono · Email.
        La lista fatture porta solo il NOME del destinatario: qui si
        recuperano P.IVA, telefono ed email da agganciare per nome.

        Returns ({nome_lower: {...}}, complete).
        """
        if not self._authenticated and not self.login():
            return {}, False
        rows, complete = self._paginate_xcrud_list("clienti.php")
        result: Dict[str, Dict[str, Any]] = {}
        # Nomi ambigui: stessa Denominazione su entità con P.IVA diverse.
        # La P.IVA qui è AUTOREVOLE (guida il matching): assegnare quella
        # sbagliata dell'omonimo creerebbe abbinamenti errati. Meglio non
        # fornire nulla per un nome ambiguo che una P.IVA di un'altra azienda.
        ambiguous: set = set()
        piva_fmt = re.compile(r"^([A-Z]{2,3})?\d{8,15}$")
        email_re = re.compile(r"[^\s@<>\"']+@[^\s@<>\"']+\.[^\s@<>\"']+")
        for cells in rows:
            if not cells:
                continue
            name = (cells[0] or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in ambiguous:
                continue
            piva_raw = (cells[1] if len(cells) > 1 else "").strip()
            # I formati esteri portano spesso un suffisso descrittivo che
            # non fa parte del numero (svizzero: 'CHE-123.456.789 IVA' /
            # 'MWST' / 'TVA'): va tolto PRIMA del check di formato, o la
            # P.IVA valida viene scartata per colpa del suffisso.
            piva_raw = re.sub(r"\s+(IVA|MWST|TVA|VAT)\.?$", "", piva_raw, flags=re.IGNORECASE)
            piva = re.sub(r"[^0-9A-Za-z]", "", piva_raw).upper() or None
            # Scarta valori non conformi al formato P.IVA (testo, note, CF
            # di persona): non devono sovrascrivere una P.IVA valida.
            if piva and not piva_fmt.match(piva):
                piva = None
            phone = (cells[8] if len(cells) > 8 else "").strip() or None
            email = None
            for c in cells:
                m = email_re.search(c or "")
                if m:
                    email = m.group(0).lower()
                    break
            prev = result.get(key)
            if prev is not None:
                # Omonimo: se le P.IVA divergono O una sola delle due righe
                # ce l'ha, il nome non identifica l'entità → ambiguo, si
                # rimuove l'entry. (Il caso 'presente solo su una' prima
                # veniva mergiato in silenzio: la P.IVA di un'entità finiva
                # servita anche per le fatture dell'omonima.)
                if (prev.get("piva") or piva) and prev.get("piva") != piva:
                    ambiguous.add(key)
                    result.pop(key, None)
                    continue
                # Stessa P.IVA (o assente su entrambe): completa i campi vuoti
                if not prev.get("phone") and phone:
                    prev["phone"] = phone
                if not prev.get("email") and email:
                    prev["email"] = email
                continue
            # Registra SEMPRE la riga (anche senza dati): il check di
            # ambiguità degli omonimi deve vedere anche le righe vuote,
            # altrimenti l'esito dipende dall'ordine (riga vuota prima
            # della gemella con P.IVA → P.IVA servita per il nome condiviso).
            result[key] = {"piva": piva, "phone": phone, "email": email}
        # Le entry senza alcun dato utile escono solo ORA, ad ambiguità
        # già calcolata.
        result = {
            k: v for k, v in result.items()
            if v["piva"] or v["phone"] or v["email"]
        }
        logger.info(
            f"Anagrafica: {len(result)} customers with P.IVA/contacts "
            f"from {len(rows)} rows ({len(ambiguous)} ambiguous names skipped)"
            f"{'' if complete else ' (PARTIAL)'}"
        )
        return result, complete

    @staticmethod
    def _looks_like_auth_page(response) -> bool:
        """True se la risposta è la pagina di login (sessione scaduta)."""
        try:
            if "signin" in str(response.url):
                return True
            low = response.text.lower()
            return 'type="password"' in low or "accesso alla piattaforma" in low
        except Exception:
            return False

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
        # Righe che sembravano dati-fattura ma non sono state parsate:
        # il chiamante le legge per decidere se il fetch è parziale.
        self._last_parse_drops = 0
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
                        if doc_id:
                            # Una riga con doc_id è una fattura vera: se non
                            # ha le celle attese è un problema di layout, non
                            # una riga di riepilogo.
                            self._last_parse_drops += 1
                        logger.debug(f"Row has {len(cells)} cells, expected >= {min_cells}, skipping")
                        continue

                    invoice_number = cells[colmap["documento"]].get_text(strip=True)
                    date_str = cells[colmap["data"]].get_text(strip=True)
                    customer_name = cells[colmap["destinatario"]].get_text(strip=True)
                    total_str = cells[colmap["totale"]].get_text(strip=True)
                    balance_str = cells[colmap["saldo"]].get_text(strip=True)

                    # Skip summary/total rows (no invoice number or no date)
                    if not invoice_number or not date_str:
                        if doc_id:
                            # Riga con doc_id = fattura vera: se numero o
                            # data sono vuoti è un problema di parsing, non
                            # una riga di riepilogo — conta come drop.
                            self._last_parse_drops += 1
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
                    self._last_parse_drops += 1
                    logger.warning(f"Error parsing invoice row: {e}")
                    continue

            return invoices

        except Exception as e:
            self._last_parse_drops += 1
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
