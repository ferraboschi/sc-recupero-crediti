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
    assert err.startswith("Login rifiutato da FatturaPro") and "dati inseriti non sono validi" in err
    assert "PASSWORD" not in err  # il motivo è noto: niente falsi suggerimenti
    assert _login_failure_error(_Silent()) == "Login failed — check FATTURAPRO_USERNAME/PASSWORD env vars"
    assert _login_failure_error(object()).startswith("Login failed")


def test_close_signs_out_only_when_authenticated(monkeypatch):
    """A fine sync la sessione va chiusa su FatturaPro (sessione unica per
    account), ma senza login non si chiama signout."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="ok")

    conn = FatturaProConnector()
    conn.client = httpx.Client(transport=httpx.MockTransport(handler))
    conn.close()
    assert "/signout.php" not in calls

    conn2 = FatturaProConnector()
    conn2.client = httpx.Client(transport=httpx.MockTransport(handler))
    conn2._authenticated = True
    conn2.close()
    assert calls.count("/signout.php") == 1
    assert conn2._authenticated is False


def test_signout_failure_is_not_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    conn = FatturaProConnector()
    conn.client = httpx.Client(transport=httpx.MockTransport(handler))
    conn._authenticated = True
    assert conn.logout() is False
    conn.close()  # non solleva


# ── Sessione unica: «L'account è già in uso su un altro dispositivo» ──────────

WARN = ("L'account e' gia' in uso su un altro dispositivo: se prosegui l'altra "
        "sessione verra' chiusa.")
DASH = "<html><body>documenti xcrud</body></html>"


def _single_session_connector(monkeypatch, interstitial_html):
    """Dopo il POST di login FatturaPro mostra l'avviso; documenti.php è
    protetto finché la conferma non arriva (flag 'confirmed')."""
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_USERNAME", "u@example.com")
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_PASSWORD", "segreta")
    state = {"confirmed": False, "posts": [], "gets": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path, qs = request.url.path, request.url.query.decode()
        if request.method == "POST":
            body = request.content.decode()
            state["posts"].append((path, body))
            if "force=1" in body or "prosegui=1" in body or "conferma=1" in body:
                state["confirmed"] = True
                return httpx.Response(302, headers={"location": "https://cloud.fatturapro.click/dashboard.php"})
            if path == "/signin.php":
                return httpx.Response(200, text=interstitial_html)
            return httpx.Response(404)
        state["gets"].append(path + ("?" + qs if qs else ""))
        if path == "/signin.php" and "force=1" in qs:
            state["confirmed"] = True
            return httpx.Response(302, headers={"location": "https://cloud.fatturapro.click/dashboard.php"})
        if path == "/documenti.php":
            if state["confirmed"]:
                return httpx.Response(200, text=DASH)
            return httpx.Response(302, headers={"location": "https://cloud.fatturapro.click/signin.php"})
        if path == "/dashboard.php":
            return httpx.Response(200, text=DASH)
        return httpx.Response(200, text=LOGIN_PAGE)

    conn = FatturaProConnector()
    conn.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return conn, state


def test_single_session_confirm_form_without_password(monkeypatch, caplog):
    html = (f"<html><body><div class='alert alert-warning'>{WARN}</div>"
            "<form action='signin.php' method='post'><input type='hidden' name='force' value='1'>"
            "<button type='submit' name='prosegui' value='1'>Prosegui</button></form>"
            "<form action='signin.php' method='post'><input type='email' name='username'>"
            "<input type='password' name='password'></form></body></html>")
    conn, state = _single_session_connector(monkeypatch, html)
    caplog.set_level("INFO", logger="backend.connectors.fatturapro")
    assert conn._try_form_login() is True
    assert state["confirmed"]
    confirm = state["posts"][-1][1]
    assert "force=1" in confirm and "prosegui=1" in confirm and "password" not in confirm
    assert any("Single-session page: form action=signin.php" in r.getMessage() for r in caplog.records)
    assert not any("segreta" in r.getMessage() for r in caplog.records)


def test_single_session_login_form_with_extra_hidden_field(monkeypatch):
    html = (f"<html><body><div class='alert'>{WARN}</div>"
            "<form action='signin.php' method='post'><input type='email' name='username'>"
            "<input type='password' name='password'><input type='hidden' name='force' value='1'>"
            "<button type='submit'>Prosegui</button></form></body></html>")
    conn, state = _single_session_connector(monkeypatch, html)
    assert conn._try_form_login() is True
    confirm = state["posts"][-1][1]
    assert "force=1" in confirm and "username=u%40example.com" in confirm and "password=segreta" in confirm


def test_single_session_link(monkeypatch):
    html = (f"<html><body><div class='alert'>{WARN}</div>"
            "<a href='signin.php?force=1'>Prosegui comunque</a>"
            "<form action='signin.php' method='post'><input type='password' name='password'></form>"
            "</body></html>")
    conn, state = _single_session_connector(monkeypatch, html)
    assert conn._try_form_login() is True
    assert "/signin.php?force=1" in state["gets"]


def test_single_session_unrecognised_page_fails_once_with_message(monkeypatch, caplog):
    """Pagina senza alcun form/link di conferma: un solo tentativo, niente
    loop, e il motivo resta visibile."""
    html = f"<html><body><div class='alert'>{WARN}</div></body></html>"
    conn, state = _single_session_connector(monkeypatch, html)
    caplog.set_level("INFO", logger="backend.connectors.fatturapro")
    assert conn._try_form_login() is False
    assert len(state["posts"]) == 1
    assert "altro dispositivo" in conn.last_login_error
    assert any("nessun form/link di conferma" in r.getMessage() for r in caplog.records)


def test_plain_rejection_does_not_trigger_confirm(monkeypatch):
    """«Dati non validi» non è l'avviso di sessione: nessun secondo POST."""
    conn, seen = _connector(monkeypatch, REJECTED_PAGE)
    assert conn._try_form_login() is False
    assert "altro dispositivo" not in (conn.last_login_error or "")
