# Verifica segnalazioni del 16/07/2026 — guida operativa

Le 5 segnalazioni (993/QOQA, 655/Belfiore, anagrafica Shopify, Domò/YOHO,
Dr. Gahe 899) hanno cause nel codice **già corrette in questo branch**, ma i
dati storici in produzione vanno fatti convergere e un paio di casi possono
richiedere un click manuale. Questa guida dice, per ciascun caso, cosa
succede da solo dopo il deploy e cosa/come verificare.

## Cosa succede da solo dopo il deploy

Il **repair degli abbinamenti ora è ricorrente**: gira a ogni full sync con
fetch completo (prima era one-shot col marker `match_repair_v2` — bruciato,
quindi non correggeva più nulla). Al primo full sync dopo il deploy:

- le fatture con **P.IVA in contraddizione** col cliente abbinato e nome
  dissimile vengono scollegate e riabbinate al cliente giusto (anche le
  PAGATE, che inquinavano i totali storici);
- le fatture il cui **nome coincide esattamente** con un ALTRO cliente
  (caso YOHO su Domò, se il cliente YOHO esiste) vengono spostate sul
  profilo giusto;
- i casi non deterministici finiscono in **review** (pagina Sistema →
  audit abbinamenti), senza rilogggare doppioni a ogni sync.

Non serve più cancellare marker a mano su Supabase.

## Caso per caso

### 1. Fattura 993/2026 (QOQA) sul profilo sbagliato

Query discriminante (Supabase SQL editor):

```sql
SELECT id, invoice_number, source_platform, status, customer_id,
       match_method, match_score, customer_name_raw, customer_piva_raw,
       suggested_customer_id, suggested_method, updated_at
FROM invoices WHERE invoice_number ILIKE '%993%';
```

- `match_method` IN (`manual`, `fuzzy_confirmed`): è una **decisione umana**
  (conferma/riassegnazione sbagliata di un batch precedente) — il repair non
  la tocca per design. Correzione: profilo → Scollega → riassegna, oppure
  Sistema → audit → Scollega.
- `customer_piva_raw` NULL: la P.IVA non è mai arrivata dall'anagrafica
  FatturaPro (join per nome esatto). Verificare su FatturaPro → clienti.php
  che la Denominazione coincida ESATTAMENTE con il nome in fattura. Se QOQA
  è il cliente svizzero, la P.IVA `CHE-…` con suffisso « IVA» viene scartata
  dal formato — in quel caso il repair scatta comunque per nome, se esiste
  il profilo giusto.
- Più righe (fatturapro + fatture24): duplicato da import CSV — scollegare
  la copia sbagliata a mano.

### 2. Fattura 655/2026 (Belfiore) scaduta ma non visibile

Con questo branch le fatture **in quarantena compaiono sul profilo del
cliente suggerito** (sezione "In attesa di conferma", con Conferma/Rifiuta):
il caso più probabile (suggerimento pendente verso Belfiore) si risolve
con un click direttamente dal profilo. Inoltre `M & M` e `M&M` ora
producono la stessa chiave di matching, quindi il name_exact non fallisce
più per la spaziatura.

Query di controllo:

```sql
SELECT id, invoice_number, status, days_overdue, customer_id,
       suggested_customer_id, suggested_method, match_method, missing_streak
FROM invoices WHERE invoice_number ILIKE '%655%';

SELECT id, ragione_sociale, partita_iva, shopify_id, source
FROM customers WHERE ragione_sociale ILIKE '%belfiore%';
```

- 2+ righe clienti "Belfiore" → la fattura sta su un profilo duplicato:
  riassegnarla e (in futuro) fare merge.
- `status='paid'` con la fattura ancora da incassare → il primo sync
  completo la riapre da solo (`paid→open` quando ricompare con saldo>0).

### 3. Anagrafica Shopify (telefono, email, numero d'ordine)

Fix inclusi: paginazione ordini corretta (prima gli ordini vecchi non
venivano MAI letti oltre i primi 250), criteri di match ordine→fattura
allargati (importo anche al netto IVA, finestra 90 giorni), contatti
copiati anche sui profili nati dalle fatture quando condividono la P.IVA
col cliente Shopify, P.IVA spazzatura da `address2` non più salvata.

Due verifiche che SOLO il proprietario può fare:

1. **Scope del token Shopify** (Dev Dashboard → app): servono
   `read_customers`, `read_orders` e — importante — `read_all_orders`:
   senza quest'ultimo Shopify restituisce SILENZIOSAMENTE solo gli ordini
   degli ultimi 60 giorni, e le fatture da recuperare sono per definizione
   più vecchie. Nessun errore nei log: solo zero match.
2. **Tag B2B**: viene importato solo chi ha il tag `B2B` su Shopify. Un
   cliente senza tag è invisibile all'app (niente contatti, niente ordini).

Dopo il deploy, pagina Sistema → stato sync: ora mostra anche l'esito
dell'order matching (prima gli errori restavano sepolti nel JSON).

### 4. Domò Milano ↔ YOHO MILANO (fatture F24)

Causa confermata: il vecchio motore (fino al rework) abbinava in automatico
col fuzzy a soglia 75, e `token_set_ratio('domo milano','yoho milano') = 81`
per via del token condiviso "milano". Quegli abbinamenti erano congelati.

- Se il cliente **YOHO MILANO esiste** in anagrafica: il repair ricorrente
  sposta da solo le sue fatture al primo full sync (relink name_exact).
- Se **non esiste**: le fatture restano su Domò finché non nasce il
  profilo. Nella pagina Posizioni → "Da confermare", i suggerimenti
  sbagliati ora hanno il bottone **"Crea nuovo cliente"**: crearlo dalla
  prima fattura YOHO, poi il sync successivo sistema le altre.

Query di controllo:

```sql
SELECT i.id, i.invoice_number, i.customer_name_raw, i.match_method, i.match_score
FROM invoices i
WHERE i.customer_id = (SELECT id FROM customers WHERE ragione_sociale ILIKE '%dom%milano%' LIMIT 1)
  AND i.customer_name_raw ILIKE '%yoho%';

SELECT id, ragione_sociale, created_at FROM customers WHERE ragione_sociale ILIKE '%yoho%';
```

### 5. Dr. Gahe di Mercuri Christian — insoluto 899/2026 non rilevato

Causa confermata: la fattura elettronica della ditta individuale è
intestata alla sola persona («MERCURI CHRISTIAN»), che la chiave di
matching riduceva a similarità 25 con «Dr. Gahe di Mercuri Christian»
(la chiave del cliente è "dr gahe"): niente match, niente suggerimento —
e l'auto-create creava un profilo duplicato che assorbiva la fattura.

Col fix, il confronto "light" (che conserva `di Nome Cognome`) rende la
persona CONCORDE con l'insegna: con la P.IVA la fattura si abbina da sola;
senza P.IVA produce almeno un suggerimento (visibile anche sul profilo).

Query di controllo:

```sql
SELECT id, invoice_number, source_platform, status, customer_id,
       match_method, suggested_customer_id, suggested_method,
       days_overdue, due_date, customer_name_raw
FROM invoices WHERE invoice_number ILIKE '%899%';

SELECT id, ragione_sociale, partita_iva, source, shopify_id
FROM customers WHERE ragione_sociale ILIKE '%gahe%' OR ragione_sociale ILIKE '%mercuri%';
```

- Se esce un cliente duplicato «MERCURI CHRISTIAN» (o «Christian Mercuri»
  da Shopify senza company): le sue fatture vanno riassegnate al profilo
  Dr. Gahe e il duplicato va svuotato; il repair sposterà i futuri
  disallineamenti.
- Se la riga fattura NON esiste: è una Fattura24 mai importata → re-import
  del CSV aggiornato (POST /sync/import-csv), controllando `skipped/errors`
  nella risposta.

## Nota operativa sul sync

Il servizio Render è su piano free: l'istanza dorme e il job delle 08:30
può non scattare (lo scheduler è in-process, il tick perso non viene
recuperato). Il sync di fatto gira 60 secondi dopo il wake. Se serve
puntualità: upgrade del piano, oppure un ping esterno (UptimeRobot) alle
08:25, oppure un Render Cron Job che chiama `POST /api/sync/full`.
Il bottone Sync della Dashboard ora attende il VERO completamento prima di
dire "completata" (prima mostrava i dati pre-sync come esito).
