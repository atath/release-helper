import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import panel

SEP = "\033[32m\033[1m" + "─" * 60 + "\033[0m"

WYNIK = (
    "\n  release-helper  jaguarapp-front  sit → test\n\n"
    "  ✓ Pobieranie zmian z remote   (0.4s)\n"
    "\n  \033[32m\033[1m✓ Gotowe\033[0m\n\n"
    f"  {SEP}\n"
    "*JAGUARAPP-FRONT Release sit -> test 30.09.2026*\n"
    "MR: https://git.example/mr/1\n\n"
    "Changelog:\n```\n### [Dodano]\n- SAN-1: Filtr\n```\n"
    f"  {SEP}\n\n"
)

BRAK_ZMIAN = (
    "  ✗ Sprawdzanie sit → test\n"
    f"\n  {SEP}\n"
    "*D2C-FRONT* Brak commitów w sit poza test\n"
    f"  {SEP}\n\n"
)


def test_wiadomosc_z_wyniku_bez_ansi():
    assert panel.wiadomosc(WYNIK) == (
        "*JAGUARAPP-FRONT Release sit -> test 30.09.2026*\n"
        "MR: https://git.example/mr/1\n\n"
        "Changelog:\n```\n### [Dodano]\n- SAN-1: Filtr\n```"
    )


def test_wiadomosc_przy_braku_zmian():
    assert panel.wiadomosc(BRAK_ZMIAN) == "*D2C-FRONT* Brak commitów w sit poza test"


def test_wiadomosc_bez_bloku_to_none():
    assert panel.wiadomosc("Traceback (most recent call last):\n  boom\n") is None


def test_panel_ma_wszystkie_projekty_z_konfiguracji():
    projekty = panel.projekty()
    assert "pelicanapp" in projekty
    assert projekty["claims-front"] == ["sit-to-test", "test-to-stage"]


def test_projekt_statyczny_nie_jest_blokowany_przez_brudne_drzewo():
    # pelicanapp nie robi checkoutu, tylko czyta refy i wystawia MR
    with patch("panel.subprocess.run", side_effect=[_proc(WYNIK, 0)]) as run:
        wynik = panel.uruchom_projekt("pelicanapp", "sit-to-test")
    assert wynik["status"] == "ok" and run.call_count == 1


@pytest.mark.parametrize("projekt,flow", [
    ("nieznany", "sit-to-test"),
    ("claims-front", "test-to-master"),
])
def test_niedozwolona_para_jest_odrzucana(projekt, flow):
    with pytest.raises(ValueError):
        panel.sprawdz_pare(projekt, flow)


def _proc(stdout, returncode):
    return MagicMock(stdout=stdout, stderr="", returncode=returncode)


def test_brudne_repo_blokuje_release():
    with patch("panel.subprocess.run", return_value=_proc(" M src/a.ts\n", 0)) as run:
        wynik = panel.uruchom_projekt("jaguarapp-front", "sit-to-test")
    assert wynik["status"] == "zablokowany"
    assert "niezacommitowane" in wynik["komunikat"]
    assert run.call_count == 1  # helper w ogole nie ruszyl


def test_udany_release():
    with patch("panel.subprocess.run", side_effect=[_proc("", 0), _proc(WYNIK, 0)]):
        wynik = panel.uruchom_projekt("jaguarapp-front", "sit-to-test")
    assert wynik["status"] == "ok"
    assert wynik["wiadomosc"].startswith("*JAGUARAPP-FRONT Release")


def test_brak_zmian():
    with patch("panel.subprocess.run", side_effect=[_proc("", 0), _proc(BRAK_ZMIAN, 1)]):
        wynik = panel.uruchom_projekt("d2c-front", "sit-to-test")
    assert wynik["status"] == "brak_zmian"
    assert wynik["wiadomosc"] == "*D2C-FRONT* Brak commitów w sit poza test"


def test_blad_helpera_daje_log():
    with patch("panel.subprocess.run", side_effect=[_proc("", 0), _proc("Traceback\nRuntimeError: GitLab 500\n", 1)]):
        wynik = panel.uruchom_projekt("d2c-front", "sit-to-test")
    assert wynik["status"] == "blad"
    assert "GitLab 500" in wynik["log"]


def test_drugi_release_w_trakcie_jest_odrzucany():
    zadanie = panel.Zadania()
    blokada = threading.Event()

    def wolny(projekt, flow):
        blokada.wait(2)
        return {"status": "ok"}

    with patch("panel.uruchom_projekt", side_effect=wolny):
        zadanie.start(["jaguarapp-front"], "sit-to-test")
        with pytest.raises(RuntimeError):
            zadanie.start(["d2c-front"], "sit-to-test")
        blokada.set()
        for _ in range(50):
            if not zadanie.trwa():
                break
            time.sleep(0.05)
    assert zadanie.stan()["projekty"][0]["status"] == "ok"


# ── HTTP ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def serwer():
    srv = panel.zbuduj_serwer(0, token="sekret")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def _zadanie(port, sciezka, metoda="GET", body=None, naglowki=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{sciezka}", method=metoda,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=naglowki or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_strona_zawiera_token_i_projekty(serwer):
    kod, html = _zadanie(serwer, "/")
    assert kod == 200 and "sekret" in html and "claims-front" in html


def test_api_bez_tokenu_jest_odrzucane(serwer):
    kod, _ = _zadanie(serwer, "/api/stan")
    assert kod == 403


def test_obcy_host_jest_odrzucany(serwer):
    kod, _ = _zadanie(serwer, "/", naglowki={"Host": "evil.example"})
    assert kod == 403


def test_start_z_niedozwolona_para_to_400(serwer):
    kod, body = _zadanie(serwer, "/api/start", "POST",
                         {"projekty": ["claims-front"], "flow": "test-to-master"},
                         {"X-Token": "sekret", "Content-Type": "application/json"})
    assert kod == 400 and "claims-front" in body


def test_zmiana_konfiguracji_widoczna_bez_restartu(tmp_path):
    cfg = json.load(open(release_helper_cfg()))
    cfg["projects"]["nowy-front"] = {"path": "~/x", "namespace": "g/x", "flows": ["sit-to-test"]}
    plik = tmp_path / "config.json"
    plik.write_text(json.dumps(cfg))
    with patch("release_helper.CONFIG_FILE", str(plik)):
        assert "nowy-front" in panel.projekty()
        panel.sprawdz_pare("nowy-front", "sit-to-test")
        assert "nowy-front" in panel.strona("t")


def release_helper_cfg():
    import release_helper
    return release_helper.CONFIG_FILE


def test_zepsuta_konfiguracja_daje_czytelny_blad(serwer, tmp_path):
    plik = tmp_path / "config.json"
    plik.write_text('{"flows": {"x": {"typ": "zly"}}}')
    with patch("release_helper.CONFIG_FILE", str(plik)):
        kod, tresc = _zadanie(serwer, "/")
    assert kod == 500 and "config.json" in tresc
