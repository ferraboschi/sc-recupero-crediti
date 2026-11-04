"""Login rifiutato: il messaggio di FatturaPro deve finire nei log e in
result['fatturapro']['error'] (incidente 26/10→4/11/2026: credenziali
giuste, login rifiutato, 9 giorni senza incassi e nessun indizio nei log)."""
import httpx

from backend import config as cfgmod
from backend.connectors.fatturapro import FatturaProConnector

LOGIN_PAGE = """<html><head><title>FatturaPRO | Accesso</title></head><body>
<form action="signin.php" method="post">
<input type="email" name="username"><input type="password" name="password">
<input type="hidden" name="token" value="abc"></form></body></html>"""

REJECTED_PAGE = LOGIN_PAGE.replace(
    "</form>",
    "</form><div class='alert alert-danger'>ATTENZIONE: i dati inseriti non sono validi, "
    "verifica e riprova inserendo anche la Partita IVA, oppure <a href='#'>recupera le credenziali</a></div>",
)


def _connector(monkeypatch, post_page, post_headers=None):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_USERNAME", "u@example.com")
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_PASSWORD", "segreta")
    seen = {"posted": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/signin.php" and request.method == "POST":
            seen["posted"] = request.content.decode()
            return httpx.Response(200, text=post_page, headers=post_headers or {})
        if path == "/documenti.php":
            return httpx.Response(302, headers={"location": "https://cloud.fatturapro.click/signin.php"})
        return httpx.Response(200, text=LOGIN_PAGE)

    conn = FatturaProConnector()
    conn.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return conn, seen


def test_rejected_login_captures_fatturapro_message_and_headers(monkeypatch, caplog):
    conn, seen = _connector(monkeypatch, REJECTED_PAGE, {"server": "cloudflare", "cf-ray": "9a1b-FRA"})
    caplog.set_level("INFO", logger="backend.connectors.fatturapro")
    assert conn._try_form_login() is False
    assert "dati inseriti non sono validi" in conn.last_login_error
    assert "server=cloudflare" in conn.last_login_error and "cf-ray=9a1b-FRA" in conn.last_login_error
    assert any("rifiutato il login" in r.getMessage() and "dati inseriti" in r.getMessage() for r in caplog.records)
    # il form è stato inviato con i campi attesi (hidden incluso), mai loggato
    assert "username=u%40example.com" in seen["posted"] and "token=abc" in seen["posted"]
    assert not any("segreta" in r.getMessage() for r in caplog.records)


def test_rejected_login_without_alert_has_no_message(monkeypatch):
    conn, _ = _connector(monkeypatch, LOGIN_PAGE)
    assert conn._try_form_login() is False
    assert conn.last_login_error is None


def test_cloudflare_challenge_is_named(monkeypatch):
    page = "<html><body><script src='/cdn-cgi/challenge-platform/x.js'></script></body></html>"
    conn, _ = _connector(monkeypatch, page, {"cf-mitigated": "challenge"})
    assert conn._try_form_login() is False
    assert "Cloudflare" in conn.last_login_error and "cf-mitigated=challenge" in conn.last_login_error


def test_sync_error_message_carries_the_rejection():
    """Il campo error del sync (visibile nel pannello Sistema) riporta il messaggio."""
    from backend.api.sync import _login_failure_error

    class _Rejecting:
        last_login_error = "ATTENZIONE: i dati inseriti non sono validi [server=cloudflare]"

    class _Silent:
        last_login_error = None

    err = _login_failure_error(_Rejecting())
    assert err.startswith("Login failed") and "dati inseriti non sono validi" in err
    assert _login_failure_error(_Silent()) == "Login failed — check FATTURAPRO_USERNAME/PASSWORD env vars"
    assert _login_failure_error(object()).startswith("Login failed")
