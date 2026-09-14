"""Stato SDI dei documenti FatturaPro — definizione unica.

La piattaforma registra SOLO le fatture che lo SDI ha CONSEGNATO o NON
CONSEGNATO (regola owner, 2026-09-14): ogni altro stato (bozza non ancora
trasmessa, in elaborazione, scartata) prevede una modifica del documento e
importarlo produce dati sfalsati (numeri riassegnati, importi diversi).
"""
from typing import Iterable, Optional

# Stati SDI finali accettati: la fattura esiste ed è definitiva.
SDI_FINAL_OK = ("consegnata", "mancata_consegna")

SDI_LABELS = {
    "draft": "Non trasmessa (bozza)",
    "sent": "Trasmessa, in elaborazione",
    "consegnata": "Consegnata",
    "mancata_consegna": "Mancata consegna",
    "scartata": "Scartata dallo SDI",
}


def sdi_state_from_notifications(names: Optional[Iterable[str]]) -> str:
    """Stato SDI dai nomi/file delle notifiche di FatturaPro ("Mostra
    Notifiche"). RC RicevutaConsegna → consegnata; MC NotificaMancataConsegna
    e AT AttestazioneTrasmissione (destinatario irraggiungibile) →
    mancata_consegna; NS NotificaScarto → scartata (prevale: il documento
    verrà corretto e ritrasmesso, il numero può cambiare); NE/EC esito, DT
    decorrenza termini → consegnata (il documento è arrivato). Nessuna
    notifica → sent (in elaborazione: non ancora definitivo)."""
    # Le notifiche arrivano in ordine cronologico: un documento scartato, poi
    # corretto e ritrasmesso (stesso doc_id) ha [NS, RC] → vale l'ULTIMA
    # notifica decisiva, non la peggiore.
    state = "sent"
    for raw in (names or []):
        n = str(raw).lower()
        if "scarto" in n or "_ns_" in n:
            state = "scartata"
        elif "ricevutaconsegna" in n or "_rc_" in n:
            state = "consegnata"
        elif ("mancataconsegna" in n or "_mc_" in n
              or "attestazionetrasmissione" in n or "_at_" in n):
            state = "mancata_consegna"
        elif "esito" in n or "decorrenza" in n or "_ne_" in n or "_ec_" in n or "_dt_" in n:
            state = "consegnata"
    return state


def sdi_state_from_label(label: Optional[str]) -> Optional[str]:
    """Stato SDI dalla colonna 'Stato' della lista completa documenti di
    FatturaPro (testo in italiano, tollerante alle varianti). None se il
    testo non è riconosciuto: il chiamante non deve inventare uno stato."""
    t = (label or "").strip().lower()
    if not t:
        return None
    # Etichette viste sul FatturaPro reale (2026-09-14): "Inviabile" (bozza,
    # non trasmessa), "Inviato SDI" (in elaborazione), "Consegnato".
    if "inviabile" in t:
        return "draft"
    if "scart" in t or "rifiut" in t:
        return "scartata"
    if "mancata" in t or "non consegn" in t or "impossibil" in t:
        return "mancata_consegna"
    if "consegn" in t or "accett" in t or "decorrenza" in t:
        return "consegnata"
    if "da inviare" in t or "bozza" in t or "non inviat" in t or "da trasmettere" in t:
        return "draft"
    if "inviat" in t or "elaboraz" in t or "trasmess" in t or "in attesa" in t:
        return "sent"
    return None
