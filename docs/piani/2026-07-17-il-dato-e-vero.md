# Piano 1 — "Il dato è vero" (integrità dati)

> **Per chi esegue:** i passi usano checkbox (`- [ ]`). Un task = un commit. TDD stretto:
> prima il test che fallisce, poi il fix minimo, poi i test verdi, poi commit.

**Goal:** il sistema smette di dichiarare pagati crediti veri e di abbinare fatture al cliente sbagliato.

**Architettura:** solo backend. Nove difetti indipendenti, ognuno con un test di regressione.
Nessuna migrazione DB, nessun cambio di contratto API: le pagine esistenti continuano a
funzionare identiche. Questo piano è il **prerequisito degli altri tre**: finché il sync
marca pagate fatture vere e il matching sposta soldi sul cliente sbagliato, qualsiasi
riconciliazione dei numeri (Piano 2) poggia su dati falsi.

**Stack:** Python 3.12, FastAPI, SQLAlchemy, pytest. Baseline: **340 test verdi**.

**Comando test (serve sempre):**
```bash
JWT_SECRET=ci-test-secret AUTH_PASSWORD=ci-test-password python -m pytest tests/ -q
```

**Roadmap complessiva** (gli altri piani, da scrivere dopo):
- Piano 2 — "I conti tornano": colonna `paid_at`, definizione unica di scaduto, esclusi che
  scalano, endpoint di riconciliazione, cascata + grafico in home. *(punti owner 2 e 4)*
- Piano 3 — "La scheda sanifica": audit per-cliente nella scheda, contatore e filtri in
  Clienti. *(punti owner 1 e 5)*
- Piano 4 — "Sync automatico": via i pulsanti, resta l'orario del prossimo sync. *(punto 3)*

---

### Task 1: Il prefisso `IT` non deve più saltare il checksum

**Perché:** `validate_piva("1234567890")` (10 cifre) → `None`, ma `validate_piva("IT1234567890")`
→ **accettata** come estera. `IT…` è la grafia intra-UE normale. Conseguenza reale: la fattura
non trova il cliente giusto, non produce nemmeno un suggerimento, l'auto-create genera un
**cliente duplicato**, e `verify.py` mostra un semaforo rosso "P.IVA DIVERSA" su una
contraddizione inesistente.

**Files:**
- Modify: `backend/engine/piva.py:22-30`
- Test: `tests/test_piva.py`

- [ ] **Step 1: scrivi il test che fallisce**

In fondo a `tests/test_piva.py`:

```python
def test_it_prefix_never_bypasses_italian_checksum():
    """IT + cifre è SEMPRE una P.IVA italiana: deve passare dal checksum.

    Regressione: 'IT1234567890' (10 cifre) veniva accettata come P.IVA
    ESTERA (_FOREIGN_RE), saltando il checksum, mentre '1234567890' nuda
    veniva correttamente rifiutata.
    """
    # stessa cifratura, con e senza prefisso: stesso verdetto
    assert validate_piva("1234567890") is None      # 10 cifre: invalida
    assert validate_piva("IT1234567890") is None    # idem, col prefisso
    assert validate_piva("123456789012") is None    # 12 cifre: invalida
    assert validate_piva("IT123456789012") is None  # idem, col prefisso
    # la valida resta valida, normalizzata senza prefisso
    assert validate_piva("IT12345678903") == "12345678903"
    assert validate_piva("12345678903") == "12345678903"


def test_foreign_piva_still_valid():
    """Le P.IVA estere vere non devono essere toccate dal fix."""
    assert validate_piva("DE123456789") == "DE123456789"
    assert validate_piva("FR12345678901") == "FR12345678901"
```

- [ ] **Step 2: esegui e verifica che fallisca**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_piva.py::test_it_prefix_never_bypasses_italian_checksum -v
```
Atteso: **FAIL** — `assert 'IT1234567890' is None`.

- [ ] **Step 3: il fix minimo**

In `backend/engine/piva.py`, sostituisci `normalize_piva`:

```python
def normalize_piva(raw: Optional[str]) -> str:
    """Uppercase, senza spazi né prefisso 'IT' ridondante."""
    if not raw:
        return ""
    piva = re.sub(r"[\s.\-]", "", raw.strip().upper())
    # 'IT12345678901' e '12345678901' sono la stessa P.IVA italiana.
    # Il prefisso va tolto ogni volta che il resto è tutto CIFRE, non solo
    # quando sono esattamente 11: altrimenti una P.IVA italiana corrotta
    # (10 o 12 cifre) resta 'ITxxx', passa per estera (_FOREIGN_RE) e salta
    # il checksum. L'Italia ha solo P.IVA di 11 cifre: se il resto è
    # numerico, è italiana — valida o corrotta che sia.
    if piva.startswith("IT") and piva[2:].isdigit():
        piva = piva[2:]
    return piva
```

- [ ] **Step 4: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_piva.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```
Atteso: tutti PASS, **342 test** (i 340 di baseline + 2 nuovi).

- [ ] **Step 5: commit**

```bash
git add backend/engine/piva.py tests/test_piva.py
git commit -m "fix(piva): il prefisso IT non salta più il checksum italiano"
```

---

### Task 2: `SPA` non è sempre una forma legale

**Perché:** `"SPA"` sta in `LEGAL_FORMS`, quindi viene tagliata **in qualsiasi posizione** del
nome. Verificato: `HOTEL SPA MILANO SRL` e `HOTEL MILANO SRL` producono entrambi la chiave
`hotel milano`. In Italia *spa* è una parola vera del settore ospitalità — esattamente il
target commerciale. È un generatore di collisioni, e il più insidioso: `light_similarity_score_strict`
su queste coppie vale **100**, quindi la guardia del Task 3 non lo intercetterebbe.

**Regola scelta:** una società ha **una** forma legale. Se ne abbiamo già trovata una
(`SRL`, `SNC`…), allora `spa` è una parola vera del nome. La sigla puntata `S.P.A.` resta
rimossa ovunque (è inequivocabile); quella nuda solo a fine nome e solo se non c'è già
un'altra forma legale.

**Files:**
- Modify: `backend/engine/normalizer.py:16-20` (lista), `:88-96` (loop)
- Test: `tests/test_normalizer.py`

- [ ] **Step 1: scrivi il test che fallisce**

In fondo a `tests/test_normalizer.py`:

```python
def test_spa_word_is_not_stripped_when_another_legal_form_exists():
    """'Hotel Spa Milano Srl': la forma legale è SRL, quindi 'spa' è una
    parola del nome. Regressione: collassava su 'Hotel Milano Srl'."""
    assert normalize_ragione_sociale("HOTEL SPA MILANO SRL") == "hotel spa milano"
    assert normalize_ragione_sociale("HOTEL MILANO SRL") == "hotel milano"
    assert normalize_ragione_sociale("HOTEL SPA MILANO SRL") != \
        normalize_ragione_sociale("HOTEL MILANO SRL")

    assert normalize_ragione_sociale("Beauty Spa Srl") == "beauty spa"
    assert normalize_ragione_sociale("Beauty Srl") == "beauty"


def test_spa_as_legal_form_is_still_stripped():
    """Quando SPA è davvero la forma legale, va via come prima."""
    # puntata: rimossa ovunque, è inequivocabile
    assert normalize_ragione_sociale("Rossi S.p.A.") == "rossi"
    assert normalize_ragione_sociale("Rossi S.P.A. Milano") == "rossi milano"
    # nuda a fine nome, senza altra forma legale: è la forma legale
    assert normalize_ragione_sociale("Rossi SPA") == "rossi"
    # le due grafie restano equivalenti
    assert normalize_ragione_sociale("Rossi SPA") == normalize_ragione_sociale("Rossi S.p.A.")
```

- [ ] **Step 2: esegui e verifica che fallisca**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_normalizer.py::test_spa_word_is_not_stripped_when_another_legal_form_exists -v
```
Atteso: **FAIL** — `assert 'hotel milano' == 'hotel spa milano'`.

- [ ] **Step 3: il fix**

3a. In `backend/engine/normalizer.py`, riga 20, togli la sigla nuda dalla lista:

```python
    "S.P.A.",
```
(era `"S.P.A.", "SPA",`)

3b. Subito sotto la definizione di `LEGAL_FORMS` (dopo la riga `]`, ~riga 38), aggiungi:

```python
# Forme la cui variante SENZA punti è una parola italiana vera: "spa" è un
# centro benessere, comunissimo fra gli alberghi e i ristoranti che sono il
# target del prodotto. La sigla PUNTATA ("S.p.A.") resta rimossa ovunque —
# è inequivocabile; quella NUDA la gestiamo a parte (vedi _strip_bare_spa).
NODOTS_ANYWHERE_UNSAFE = {"S.P.A."}
```

3c. Sostituisci il loop delle forme legali (righe 86-96) con:

```python
    # Remove legal forms (must handle both with and without dots)
    legal_form_found = False
    for form in LEGAL_FORMS:
        escaped = re.escape(form.lower())
        # Dot-flexible: "s.r.l." matcha "s.r.l." e "s.r.l"
        stripped = re.sub(rf"(?<!\w){escaped}\.?(?!\w)", "", normalized)
        if stripped != normalized:
            legal_form_found = True
            normalized = stripped
        # Variante senza punti ("srl"), salvo quelle ambigue con parole vere
        nodots = form.replace(".", "").lower()
        if nodots != form.lower() and form not in NODOTS_ANYWHERE_UNSAFE:
            stripped = re.sub(rf"(?<!\w){re.escape(nodots)}(?!\w)", "", normalized)
            if stripped != normalized:
                legal_form_found = True
                normalized = stripped

    # "spa" nuda: è la forma legale SOLO se la società non ne ha già un'altra
    # e solo a fine nome. "Hotel Spa Milano Srl" → la forma è SRL, quindi
    # 'spa' è parte del nome e va conservata.
    if not legal_form_found:
        normalized = re.sub(r"(?<!\w)spa\.?\s*$", "", normalized)
```

- [ ] **Step 4: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_normalizer.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```
Atteso: tutti PASS (i 55 test esistenti del normalizer **devono** restare verdi: se uno rompe,
è una regressione vera, non un test da aggiornare — fermati e chiedi).

- [ ] **Step 5: commit**

```bash
git add backend/engine/normalizer.py tests/test_normalizer.py
git commit -m "fix(normalizer): 'spa' è una parola, non sempre una forma legale"
```

---

### Task 3: `name_exact` deve verificare la somiglianza prima di abbinare

**Perché:** il normalizzatore taglia `di Nome Cognome`, quindi `Osteria di Mario Rossi` e
`Osteria di Luigi Bianchi` producono entrambi la chiave `osteria`. Se esiste **un solo**
cliente con quella chiave, `matching.py:151` abbina in **automatico** con score 100, senza
quarantena. La fattura di Bianchi finisce sul profilo di Rossi e ci resta (`run_matching`
processa solo `customer_id IS NULL`). Poi parte il sollecito WhatsApp all'azienda sbagliata.

Il repo **conosce già** questo collasso e lo blocca negli altri due percorsi: `repair.py:271-283`
(con un commento che cita testualmente le due osterie) e `sync.py:730-746`. Manca solo nella
porta d'ingresso di ogni fattura nuova. Questo task copia la guardia già dimostrata.

**Files:**
- Modify: `backend/engine/matching.py:21-23` (import), `:31-35` (costante), `:151-175` (guardia)
- Modify: `backend/engine/repair.py:48-51` (import), `:70` (rimuovi il duplicato)
- Test: `tests/test_matching.py`

- [ ] **Step 1: scrivi il test che fallisce**

In fondo a `tests/test_matching.py`:

```python
def test_name_exact_requires_light_similarity(session):
    """Due insegne diverse che collassano sulla stessa chiave NON si
    abbinano in automatico: vanno in quarantena.

    Regressione: 'Osteria di Mario Rossi' e 'Osteria di Luigi Bianchi'
    normalizzano entrambe a 'osteria'; con un solo cliente a sistema la
    fattura di Bianchi veniva abbinata a Rossi con score 100.
    """
    cust = Customer(ragione_sociale="Osteria di Mario Rossi")
    session.add(cust)
    session.commit()

    inv = Invoice(
        invoice_number="1/2026", amount=100.0, amount_due=100.0,
        source_platform="fatturapro",
        customer_name_raw="OSTERIA DI LUIGI BIANCHI",
        customer_piva_raw=None,
    )
    session.add(inv)
    session.commit()

    result = match_invoice_to_customer(inv, session.query(Customer).all())

    assert result.customer is None, "non deve abbinare in automatico"
    assert result.suggested_customer is not None, "deve suggerire in quarantena"
    assert result.suggested_method == "name_ambiguous"


def test_name_exact_still_matches_the_same_business(session):
    """Il caso legittimo continua a funzionare: stessa insegna, grafie diverse."""
    cust = Customer(ragione_sociale="Trattoria Da Gino S.R.L.")
    session.add(cust)
    session.commit()

    inv = Invoice(
        invoice_number="2/2026", amount=100.0, amount_due=100.0,
        source_platform="fatturapro",
        customer_name_raw="TRATTORIA DA GINO SRL",
        customer_piva_raw=None,
    )
    session.add(inv)
    session.commit()

    result = match_invoice_to_customer(inv, session.query(Customer).all())

    assert result.customer is not None
    assert result.customer.id == cust.id
    assert result.method == "name_exact"
```

> Nota per chi esegue: la fixture `session` esiste già in `tests/conftest.py`; guarda come la
> usano i test vicini in `tests/test_matching.py` e segui lo stesso stile.

- [ ] **Step 2: esegui e verifica che fallisca**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_matching.py::test_name_exact_requires_light_similarity -v
```
Atteso: **FAIL** — `assert result.customer is None` fallisce (ha abbinato Rossi).

- [ ] **Step 3: il fix**

3a. In `backend/engine/matching.py`, righe 21-23, aggiungi l'import:

```python
from backend.engine.normalizer import (
    normalize_ragione_sociale, name_similarity_score,
    light_similarity_score_strict,
)
```

3b. Sotto `MIN_DISTINCTIVE_NAME_LEN = 4` (~riga 35), aggiungi la costante (spostata qui da
`repair.py`, così i due percorsi condividono la stessa soglia):

```python
# Sopra questa somiglianza il nome CONFERMA il candidato. Sotto, due insegne
# diverse collassate sulla stessa chiave normalizzata ('Osteria di Mario
# Rossi' / 'Osteria di Luigi Bianchi') → quarantena, decide l'operatore.
NAME_CONCORDANT_THRESHOLD = 75
```

3c. In `matching.py`, dentro il ramo `if len(name_matches) == 1:`, **prima** di
`result.customer = candidate` (riga 168), inserisci:

```python
            # Il nome normalizzato coincide, ma la normalizzazione è
            # aggressiva (taglia forme legali e 'di Nome Cognome'): due
            # insegne diverse possono collassare sulla stessa chiave.
            # Serve lo scorer STRICT (token_sort, non token_set): col
            # subset-bonus il collasso monolaterale ('Osteria di Mario
            # Rossi' vs 'Osteria SRL') varrebbe 100 e passerebbe.
            # Stessa guardia già attiva in repair.py e sync.py.
            light = light_similarity_score_strict(
                invoice.customer_name_raw or "",
                candidate.ragione_sociale or "",
            )
            if light < NAME_CONCORDANT_THRESHOLD:
                result.suggested_customer = candidate
                result.suggested_method = "name_ambiguous"
                result.suggested_score = light
                log_warn(
                    f"Invoice {invoice.invoice_number}: normalized name "
                    f"matches '{candidate.ragione_sociale}' but light score "
                    f"is {light} — quarantined"
                )
                return result
```

3d. In `backend/engine/repair.py`, righe 48-51, importa la costante invece di ridefinirla:

```python
from backend.engine.matching import (
    match_invoice_to_customer, piva_contradiction, run_matching,
    PIVA_NAME_MISMATCH_THRESHOLD, NAME_CONCORDANT_THRESHOLD,
)
```

3e. In `repair.py`, **cancella** la riga 70 e il suo commento (ora vive in `matching.py`):

```python
# Sopra questa somiglianza il nome della fattura CONFERMA il cliente attuale.
NAME_CONCORDANT_THRESHOLD = 75
```

- [ ] **Step 4: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_matching.py tests/test_repair.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```
Atteso: tutti PASS. `test_repair.py` (19 test) verifica che lo spostamento della costante
non abbia rotto il repair.

- [ ] **Step 5: commit**

```bash
git add backend/engine/matching.py backend/engine/repair.py tests/test_matching.py
git commit -m "fix(matching): name_exact non abbina più due insegne collassate sulla stessa chiave"
```

---

### Task 4: la P.IVA da un'anagrafica parziale non va scritta

**Perché:** a `sync.py:217` la scadenza si applica solo `if scad_ok`. Due righe dopo, a `:222`,
la P.IVA — la fonte più autorevole del matching — si applica **sempre**: `cli_ok` è calcolato
(`:202`), riportato nel result (`:207`) e **mai usato come gate**. Il guard degli omonimi
(`fatturapro.py:813-822`) si calcola solo sulle righe scaricate: un'anagrafica troncata lo
acceca, e il "Bar Roma" milanese si prende la P.IVA di quello romano.

Il repair **è già gatato** sulla completezza (`sync.py:1226-1228`), col commento *"con fetch
parziali un detach potrebbe basarsi su dati incompleti"*. Lo stesso ragionamento vale a
maggior ragione per la **scrittura**.

**Files:**
- Modify: `backend/api/sync.py:222-227`
- Test: `tests/test_sync_hardening.py`

- [ ] **Step 1: leggi il contesto esatto**

```bash
sed -n '190,270p' backend/api/sync.py
```
Serve per vedere come `scad_ok` gata il blocco delle scadenze (riga 217) e replicare la
stessa forma. Guarda anche `tests/test_sync_hardening.py::test_partial_scadenzario_does_not_apply_due_dates`:
è il test gemello da imitare.

- [ ] **Step 2: scrivi il test che fallisce**

Modella il nuovo test **sullo stile di** `test_partial_scadenzario_does_not_apply_due_dates`
(stessa struttura di mock del connettore), cambiando l'oggetto:

```python
def test_partial_anagrafica_does_not_apply_piva(monkeypatch, session):
    """Con anagrafica INCOMPLETA la P.IVA non va scritta: il guard degli
    omonimi si calcola solo sulle righe scaricate, quindi un'anagrafica
    troncata può attribuire la P.IVA di un omonimo mai letto.

    Gemello di test_partial_scadenzario_does_not_apply_due_dates.
    """
    # fetch_clienti_map ritorna (map, complete=False)
    # → la fattura NON deve ricevere customer_piva_raw dall'anagrafica
    ...
```

> Chi esegue: apri `tests/test_sync_hardening.py`, copia la struttura del test gemello
> (mock di `fatturapro.fetch_clienti_map` che ritorna `({...}, False)`), e asserisci che
> `inv.customer_piva_raw` resti quello di partenza (`None` o il valore originale).

- [ ] **Step 3: esegui e verifica che fallisca**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_sync_hardening.py::test_partial_anagrafica_does_not_apply_piva -v
```
Atteso: **FAIL** — la P.IVA è stata scritta lo stesso.

- [ ] **Step 4: il fix**

In `backend/api/sync.py`, avvolgi il blocco del join anagrafica (righe 222-227) in un gate,
esattamente come il `if scad_ok:` di riga 217:

```python
                # SOLO su anagrafica COMPLETA: il guard degli omonimi
                # (clienti_map['ambiguous']) si calcola sulle sole righe
                # scaricate — con un fetch parziale un omonimo mai letto
                # non viene rilevato e la P.IVA finisce sull'azienda
                # sbagliata. Stessa disciplina di scad_ok (riga 217) e del
                # repair (riga 1226).
                if cli_ok:
                    ...   # il blocco esistente delle righe 222-227, indentato
```

- [ ] **Step 5: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_sync_hardening.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```
Atteso: tutti PASS (46 test in `test_sync_hardening.py` + il nuovo).

- [ ] **Step 6: commit**

```bash
git add backend/api/sync.py tests/test_sync_hardening.py
git commit -m "fix(sync): la P.IVA si scrive solo su anagrafica completa (gate cli_ok)"
```

---

### Task 5: lo scadenzario deve partire da `start = 0`

**Perché:** `fetch_scadenze_map` consuma la pagina renderizzata (ordinamento di default, ~10
righe) e poi fa partire la paginazione AJAX da `start = PAGE` = **100**, con
`orderby=scadenze.DataScadenzaPagamento desc`. Le righe 0-99 di quell'ordinamento non
vengono **mai** richieste. Con DESC le prime 100 sono le scadenze più lontane nel futuro,
cioè **le fatture aperte emesse di recente** — quelle che servono. Restano
`due_date_source='assumed'` (emissione+30): con termini reali a 60/90 giorni risultano
scadute 30-60 giorni prima del vero, si apre una pratica e si sollecita un debito non
ancora esigibile. Deterministico, a ogni run.

È il bug che il file stesso documenta di aver già corretto altrove: `fatturapro.py:422-424`
(*"Si parte da start=0 così TUTTE le pagine condividono lo stesso ordinamento… un mismatch
faceva saltare deterministicamente una finestra di fatture a ogni sync"*) e il gemello
`_paginate_xcrud_list:588`. Solo `fetch_scadenze_map` parte da 100.

**Files:**
- Modify: `backend/connectors/fatturapro.py:704`
- Test: `tests/test_fatturapro_lists.py`

- [ ] **Step 1: scrivi il test che fallisce**

```python
def test_scadenze_pagination_starts_from_zero(monkeypatch):
    """La paginazione AJAX deve chiedere start=0: con orderby DESC imposto,
    le prime 100 righe sono le scadenze più lontane = le fatture aperte
    recenti. Partendo da 100 non venivano MAI richieste.

    Gemello di fetch_overdue_invoices (fatturapro.py:424) e
    _paginate_xcrud_list (:588), che partono già da 0.
    """
    starts = []
    # mock del POST su xcrud_ajax.php che registra data["xcrud[start]"]
    # ... (segui lo stile dei test di paginazione già presenti nel file)
    assert starts[0] == "0", f"la prima pagina AJAX deve partire da 0, non da {starts[0]}"
```

> Chi esegue: `tests/test_fatturapro_lists.py` ha già 25 test con i mock HTTP pronti — copia
> la struttura del test di paginazione più vicino invece di inventarne una nuova.

- [ ] **Step 2: esegui e verifica che fallisca**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_fatturapro_lists.py::test_scadenze_pagination_starts_from_zero -v
```
Atteso: **FAIL** — `la prima pagina AJAX deve partire da 0, non da 100`.

- [ ] **Step 3: il fix**

In `backend/connectors/fatturapro.py`, riga 704:

```python
            PAGE = 100
            # Si parte da start=0 così TUTTE le pagine condividono
            # l'ordinamento imposto (DESC per data scadenza). Partendo da
            # PAGE, le prime 100 righe di QUELL'ordinamento — le scadenze
            # più lontane, cioè le fatture aperte recenti — non venivano mai
            # richieste. Il re-ingest delle righe già lette dalla pagina
            # renderizzata è innocuo: `result` è un dict per doc_key con
            # merge via min(), `covered` è un set.
            start = 0
```

- [ ] **Step 4: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_fatturapro_lists.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```
Atteso: tutti PASS. **Attenzione:** `test_convergence_stops_early_when_targets_covered`
asserisce `posts["n"] == 0` (il target è in pagina 1, l'AJAX non parte). Con `start=0` il
comportamento non cambia: la convergenza si valuta *prima* del POST. Se questo test rompe,
fermati e chiedi — significa che il ciclo va riletto.

- [ ] **Step 5: commit**

```bash
git add backend/connectors/fatturapro.py tests/test_fatturapro_lists.py
git commit -m "fix(fatturapro): lo scadenzario parte da start=0, non salta più le prime 100 righe"
```

---

### Task 6: tiebreaker sulla paginazione delle fatture

**Perché — il più grave del piano.** La paginazione xcrud usa `xcrud[orderby]: "documenti.Data"`
+ `start`/`limit`: un **ordinamento non totale** (la data non è univoca) su cui si fa
LIMIT/OFFSET. Il codice *sa già* che il confine di pagina è instabile (`fetch_overdue_invoices:379-381`
lo dice, e per questo `_add_batch` deduplica) — ma una finestra che scivola **ripete** righe
esattamente quando ne **salta** altre, e del salto non si accorge nessuno: `dropped_rows`
resta 0, `partial` resta `False`.

Scenario: 14 fatture emesse lo stesso giorno, `PAGE_SIZE=10`. Pagina 1 prende 10 delle 14 in
una permutazione; pagina 2 (`start=10`) ri-esegue la query e il DB restituisce una
permutazione diversa → 2 fatture non compaiono in nessuna pagina. Fetch riportato **completo**
→ `missing_streak=1`; il sync dopo, stessa query, stesse 2 mancanti → `streak=2` → `paid`,
`amount_due=0`, pratica chiusa. `PAID_ABSENCE_STREAK` protegge dalla perdita *casuale*: qui
la perdita è *deterministica* e si ripete identica, quindi la doppia assenza non filtra nulla.

**Files:**
- Modify: `backend/connectors/fatturapro.py:436-451` (orderby), `:493` (usa il ritorno di `_add_batch`)
- Test: `tests/test_fatturapro_lists.py`

- [ ] **Step 1: ispeziona il campo per il tiebreaker**

```bash
sed -n '370,500p' backend/connectors/fatturapro.py
grep -rn 'documenti\.' backend/connectors/fatturapro.py
```
Serve a scegliere il secondo criterio d'ordinamento **univoco** fra i campi che xcrud accetta
(progressivo/numero/id documento). Se xcrud accetta un solo campo in `orderby`, salta al
piano B dello Step 3.

- [ ] **Step 2: scrivi il test che fallisce**

```python
def test_overdue_pagination_has_unique_tiebreaker(monkeypatch):
    """L'orderby della paginazione deve essere TOTALE: con LIMIT/OFFSET su
    un ordinamento per sola data, le righe pari-data scivolano fra le
    pagine e alcune non compaiono in NESSUNA pagina — senza che partial
    scatti. Poi la payment detection le marca pagate.
    """
    orderbys = []
    # mock che registra data["xcrud[orderby]"] a ogni POST
    assert all("documenti.Data" in o and o != "documenti.Data" for o in orderbys), \
        f"orderby non univoco: {orderbys}"
```

- [ ] **Step 3: il fix**

**Piano A (preferito)** — aggiungi il tiebreaker univoco all'orderby:

```python
                        # Ordinamento TOTALE: la sola data non è univoca e
                        # con LIMIT/OFFSET le righe pari-data scivolano fra
                        # le pagine (alcune non compaiono in nessuna, senza
                        # che dropped_rows/partial se ne accorgano → la
                        # payment detection le marca pagate). Il secondo
                        # criterio rende l'ordine deterministico fra le
                        # pagine.
                        "xcrud[orderby]": "documenti.Data, documenti.<CAMPO_UNIVOCO>",
```
Sostituisci `<CAMPO_UNIVOCO>` col campo trovato allo Step 1.

**Piano B (se xcrud accetta un solo campo)** — non silenziare più lo scivolamento: usa il
valore di ritorno di `_add_batch` (oggi buttato a `:493`) per accorgertene. Se una pagina
**non finale** aggiunge meno righe di quante ne ha ricevute, la finestra si è mossa: marca
`partial = True`, così `allow_close=False` e la payment detection non chiude nulla.

```python
                added = self._add_batch(...)
                if added < len(batch) and len(batch) == PAGE_SIZE:
                    # Duplicati su una pagina NON finale = la finestra è
                    # scivolata: se ripete righe, ne sta saltando altre.
                    # Meglio un fetch dichiarato parziale (nessuna chiusura,
                    # nessun paid) che una perdita silenziosa.
                    logger.warning(
                        "overdue pagination: %d duplicati a start=%d — "
                        "finestra instabile, fetch marcato PARZIALE",
                        len(batch) - added, start,
                    )
                    partial = True
```

- [ ] **Step 4: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_fatturapro_lists.py tests/test_fatturapro_fetch.py tests/test_sync_hardening.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```

- [ ] **Step 5: commit**

```bash
git add backend/connectors/fatturapro.py tests/test_fatturapro_lists.py
git commit -m "fix(fatturapro): paginazione con ordinamento totale — niente più fatture saltate (e poi 'pagate')"
```

> **Checkpoint umano:** questo task tocca il fetch che alimenta tutto. Prima di proseguire,
> fai rivedere il diff e considera un full sync di prova su staging.

---

### Task 7: l'archiviazione non deve essere annullata dal sync successivo

**Perché:** si archivia una pratica **perché** il debito è inesigibile — cioè le fatture
scadute restano non pagate. Ma `update_case_lifecycle` guarda solo `overdue`;
`_find_reopenable_case` scarta le archiviate (`cases.py:238-243`) e si cade su `open_new_case`,
che apre una pratica nuova, ci sposta le stesse fatture e a `cases.py:145-146` riporta
`customer.recovery_status` da `archived` a `idle`. Il debitore appena dichiarato inesigibile
ricompare fra i "da contattare per la prima volta" (`dashboard.py:167-183`). Il pulsante
Archivia è di fatto un no-op che dura fino al sync successivo.

**Files:**
- Modify: `backend/engine/cases.py:400-409`
- Test: `tests/test_cases.py`

- [ ] **Step 1: scrivi il test che fallisce**

```python
def test_archived_case_is_not_reopened_by_next_sync(session):
    """Archiviare = 'questo debito non lo inseguo più'. Le fatture scadute
    restano scadute per definizione: il lifecycle successivo NON deve
    aprire una pratica nuova sulle STESSE fatture né riportare il cliente
    a 'idle'.
    """
    # 1. cliente con fattura scaduta → pratica aperta
    # 2. archivia la pratica (close_case(..., reason="archived"))
    # 3. update_case_lifecycle(session, customer)   ← il sync successivo
    # 4. assert: nessuna pratica aperta, recovery_status resta 'archived'
    ...
    assert get_open_case(session, customer.id) is None, \
        "il sync ha riaperto una pratica su fatture già archiviate"
    assert customer.recovery_status == "archived"
```

> Chi esegue: `tests/test_cases.py` ha 21 test con le fixture pronte. Guarda
> `test_case_after_archive_inherits_contacts:230` per lo stile — ma nota che **quel** test
> salda la vecchia fattura e ne aggiunge una nuova, cioè l'unico scenario in cui
> l'archiviazione non ha motivo di esistere: è per questo che il bug è passato.

- [ ] **Step 2: esegui e verifica che fallisca**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_cases.py::test_archived_case_is_not_reopened_by_next_sync -v
```
Atteso: **FAIL** — è stata aperta una pratica nuova e `recovery_status` è tornato `idle`.

- [ ] **Step 3: il fix**

In `backend/engine/cases.py`, prima di chiamare `open_new_case` in `update_case_lifecycle`
(~riga 400), non aprire nulla se **tutte** le fatture scadute erano già nella pratica
archiviata più recente. Apri solo se c'è una scaduta **nuova** (emessa o agganciata dopo il
`closed_at` dell'archiviata):

```python
        # Le fatture già archiviate non riaprono nulla: archiviare significa
        # "non inseguo più QUESTO debito", e le sue fatture restano scadute
        # per definizione. Solo un debito NUOVO (fattura non presente nella
        # pratica archiviata) merita una pratica nuova.
        last_archived = (
            session.query(RecoveryCase)
            .filter(
                RecoveryCase.customer_id == customer.id,
                RecoveryCase.status == "closed",
                RecoveryCase.closed_reason == "archived",
            )
            .order_by(RecoveryCase.closed_at.desc())
            .first()
        )
        if last_archived is not None:
            archived_invoice_ids = {i.id for i in last_archived.invoices}
            fresh = [i for i in overdue if i.id not in archived_invoice_ids]
            if not fresh:
                return  # niente di nuovo: l'archiviazione regge
```

- [ ] **Step 4: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_cases.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```
Atteso: tutti PASS, `test_case_after_archive_inherits_contacts` incluso (il suo scenario ha
una fattura **nuova**, quindi la pratica si apre come prima).

- [ ] **Step 5: commit**

```bash
git add backend/engine/cases.py tests/test_cases.py
git commit -m "fix(cases): l'archiviazione non viene più annullata dal sync successivo"
```

---

### Task 8: dopo un'archiviazione il tono non deve ripartire cordiale

**Perché:** l'eredità dei contatti vive **solo** in `open_new_case:108-118`; `reopen_case` non
la calcola mai. E `_find_reopenable_case` non si ferma sulla pratica archiviata: la **salta**
e prosegue sulle più vecchie, così una `no_overdue` antecedente vince e viene riaperta col
suo contatore a zero. Il cliente appena passato all'avvocato riceve un primo sollecito
cordiale — l'invariante *"il tono non riparte mai dal sollecito cordiale"* violata.

**Files:**
- Modify: `backend/engine/cases.py:238-243` (`_find_reopenable_case`) o `:162-195` (`reopen_case`)
- Test: `tests/test_cases.py`

- [ ] **Step 1: scrivi il test che fallisce**

```python
def test_reopen_never_restarts_tone_after_archive(session):
    """Scenario reale:
    1. fattura X scaduta → pratica 1; scadenza corretta nel futuro →
       pratica 1 chiusa 'no_overdue' (X resta agganciata alla 1)
    2. fattura Y scaduta → pratica 2; due contatti; operatore ARCHIVIA
       → pratica 2 chiusa 'archived' con 2 contatti
    3. arriva la scadenza vera di X (Y ancora insoluta)
    → la pratica 2 (archived) viene saltata e la 1 (no_overdue) riaperta
      con contact_count = 0: sollecito cordiale a chi è dall'avvocato.
    """
    ...
    assert case.inherited_contacts >= 2, \
        "il tono è ripartito da zero dopo un'archiviazione"
```

- [ ] **Step 2: esegui e verifica che fallisca**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_cases.py::test_reopen_never_restarts_tone_after_archive -v
```
Atteso: **FAIL** — `inherited_contacts == 0`.

- [ ] **Step 3: il fix**

In `_find_reopenable_case`, **fermati** al primo candidato archiviato invece di scavalcarlo
(la pratica archiviata è più recente: riaprire una precedente significa tornare indietro nel
tempo):

```python
            # Non scavalcare un'archiviazione: è la decisione più recente
            # dell'operatore. Riaprire una pratica ANTECEDENTE (es. chiusa
            # 'no_overdue') ne erediterebbe il contatore a zero e il tono
            # ripartirebbe cordiale su un cliente già passato all'avvocato.
            if case.closed_reason == "archived":
                return None
```

Così si cade su `open_new_case`, che l'eredità la calcola già (`:108-118`). Verifica che
l'interazione col Task 7 sia coerente: se **non** ci sono fatture nuove, il Task 7 esce
prima e non si apre nulla; se ce ne sono, `open_new_case` eredita i contatti.

- [ ] **Step 4: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_cases.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```

- [ ] **Step 5: commit**

```bash
git add backend/engine/cases.py tests/test_cases.py
git commit -m "fix(cases): il tono non riparte da zero dopo un'archiviazione"
```

---

### Task 9: il sollecito non può citare fatture di un altro cliente

**Perché:** `recovery.py:156` salva `sorted(set(body.invoice_ids))` senza verificare che le
fatture esistano o siano **di quel cliente**. Verificato: sollecito su ClienteA citando la
fattura di ClienteB + un id inesistente → `200`, `invoice_ids: [2, 999999]`, note *"Sollecito
n. 1 via Copia Messaggio (2 fatture)"*. È anche la metà backend della race del frontend (una
risposta lenta fa mostrare il cliente 2 mentre l'URL dice 3: il POST parte sul 3 con le
fatture del 2). Il fix backend chiude il danno **a prescindere** dal fix frontend, che
arriverà nel Piano 3.

**Files:**
- Modify: `backend/api/recovery.py:150-160`
- Test: `tests/test_solleciti_api.py`

- [ ] **Step 1: scrivi il test che fallisce**

```python
def test_sollecito_rejects_invoices_of_another_customer(client, session):
    """Il sollecito deve citare solo fatture DEL cliente: altrimenti la
    storia della pratica si inquina con fatture altrui (e il frontend, in
    race, può davvero mandarle)."""
    # cliente A con fattura 1, cliente B con fattura 2
    resp = client.post(
        f"/api/recovery/customers/{a.id}/solleciti",
        json={"channel": "whatsapp_copy", "invoice_ids": [inv_b.id, 999999]},
        headers=auth_headers,
    )
    assert resp.status_code == 400
```

> Chi esegue: `tests/test_solleciti_api.py` ha 14 test con `client` e `auth_headers` pronti.

- [ ] **Step 2: esegui e verifica che fallisca**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_solleciti_api.py::test_sollecito_rejects_invoices_of_another_customer -v
```
Atteso: **FAIL** — `assert 200 == 400`.

- [ ] **Step 3: il fix**

In `backend/api/recovery.py`, prima di costruire l'azione (~riga 150):

```python
    # Le fatture citate devono essere DI questo cliente: un id estraneo
    # significa che il chiamante sta guardando un altro cliente (race del
    # frontend fra due fetch) — registrarlo inquinerebbe la pratica con
    # fatture altrui e falserebbe il tono del prossimo sollecito.
    own_invoice_ids = {inv.id for inv in customer.invoices}
    unknown = [i for i in body.invoice_ids if i not in own_invoice_ids]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Fatture non appartenenti al cliente {customer.id}: {unknown}"
            ),
        )
```

- [ ] **Step 4: test verdi**

```bash
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/test_solleciti_api.py -v
JWT_SECRET=x AUTH_PASSWORD=x python -m pytest tests/ -q
```

- [ ] **Step 5: commit**

```bash
git add backend/api/recovery.py tests/test_solleciti_api.py
git commit -m "fix(recovery): il sollecito rifiuta fatture di un altro cliente"
```

---

## Chiusura del piano

- [ ] **Suite completa verde**

```bash
JWT_SECRET=ci-test-secret AUTH_PASSWORD=ci-test-password python -m pytest tests/ -q
flake8 backend/ --ignore=E501,W503,W504,F401 --max-line-length=120
```
Atteso: ~355 test PASS (340 baseline + ~15 nuovi), flake8 pulito.

- [ ] **Apri la PR** (il merge lo fa l'owner dal browser)

```bash
git push -u origin fix/integrita-dati-20260717
gh pr create --title "Il dato è vero: 9 difetti di integrità (crediti persi + cliente sbagliato)" --body "..."
```

- [ ] **Dopo il deploy:** lancia un full sync e controlla in pagina Sistema che
  `anagrafica_ok` e `partial` siano coerenti, e che nessuna pratica si sia chiusa a sorpresa.
