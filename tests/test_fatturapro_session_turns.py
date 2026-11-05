"""FatturaPRO tiene UNA sola sessione per utente (incidente 26/10→5/11/2026:
«L'account e' gia' in uso su un altro dispositivo»). SC Recupero, l'app ordini e
le persone condividono l'utente: turni, uscita a fine giro, e mai chiudere la
sessione di chi lavora in orario d'ufficio."""
from datetime import datetime, timezone

import httpx

from backend import config as cfgmod
from backend.connectors import fatturapro as fp
from backend.connectors.fatturapro import FatturaProConnector, in_use_policy, is_account_in_use_page

LOGIN_PAGE = """<html><body><form action="signin.php" method="post">
<input type="email" name="username"><input type="password" name="password"></form></body></html>"""
IN_USE_PAGE = """<html><body><div class="alert alert-warning">L'account e' gia' in uso su un altro dispositivo:
se prosegui l'altra sessione verra' chiusa.</div><form action="signin.php" method="post">
<input type="hidden" name="prosegui" value="1">
<a href="signin.php">Annulla</a><button type="submit" name="conferma_sessione">Prosegui</button></form></body></html>"""
DOCS_PAGE = "<html><body>documenti xcrud</body></html>"


def _connector(monkeypatch):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_USERNAME", "u@example.com")
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_PASSWORD", "segreta")
    state = {"in": False, "posts": [], "signout": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/signin.php" and request.method == "POST":
            body = request.content.decode()
            state["posts"].append(body)
            if "conferma_sessione" in body:
                state["in"] = True
                return httpx.Response(200, text=DOCS_PAGE)
            return httpx.Response(200, text=IN_USE_PAGE)
        if path == "/documenti.php":
            if state["in"]:
                return httpx.Response(200, text=DOCS_PAGE)
            return httpx.Response(302, headers={"location": "https://cloud.fatturapro.click/signin.php"})
        if path == "/signout.php":
            state["signout"] += 1
            state["in"] = False
            return httpx.Response(302, headers={"location": "https://cloud.fatturapro.click/signin.php"})
        return httpx.Response(200, text=LOGIN_PAGE)

    conn = FatturaProConnector()
    conn.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return conn, state


def test_policy_waits_in_office_hours_rome_and_takes_over_otherwise():
    assert in_use_policy(datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)) == "wait"        # gio 10:00 Roma
    assert in_use_policy(datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)) == "take_over"  # gio 19:00 Roma
    assert in_use_policy(datetime(2026, 11, 5, 6, 30, tzinfo=timezone.utc)) == "take_over"  # gio 7:30 Roma
    assert in_use_policy(datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)) == "take_over"  # sabato
    assert in_use_policy(datetime(2026, 7, 6, 6, 30, tzinfo=timezone.utc)) == "wait"        # lun 8:30 Roma (ora legale)


def test_in_use_page_recognised():
    assert is_account_in_use_page(IN_USE_PAGE)
    assert not is_account_in_use_page(LOGIN_PAGE)


def test_office_hours_never_presses_prosegui(monkeypatch):
    conn, state = _connector(monkeypatch)
    monkeypatch.setattr(fp, "in_use_policy", lambda now=None: "wait")
    assert conn.login() is False
    assert not any("conferma_sessione" in p for p in state["posts"])
    assert "orario d'ufficio" in (conn.last_login_error or "")


def test_outside_office_hours_takes_over_then_signs_out_on_close(monkeypatch):
    conn, state = _connector(monkeypatch)
    monkeypatch.setattr(fp, "in_use_policy", lambda now=None: "take_over")
    assert conn.login() is True
    assert any("conferma_sessione" in p for p in state["posts"])
    conn.close()
    assert state["signout"] == 1


def test_close_without_login_does_not_sign_out(monkeypatch):
    conn, state = _connector(monkeypatch)
    conn.close()
    assert state["signout"] == 0
