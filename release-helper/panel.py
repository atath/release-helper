"""Lokalny panel do puszczania releasów przez release_helper.py.

Użycie:  python panel.py [--port 8765]

Serwer słucha tylko na 127.0.0.1. Każdy projekt uruchamia osobny proces
release_helper.py, dokładnie tak jak z terminala, i dostaje osobną wiadomość
do skopiowania. API wymaga tokenu generowanego przy starcie i sprawdza nagłówek
Host, bo na localhost może wysłać żądanie każda strona otwarta w przeglądarce.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import release_helper

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "release_helper.py")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")
_SEP = re.compile(r"^\s*─{20,}\s*$")


def konfiguracja():
    """config.json czytany przy każdym żądaniu: zmiana działa po odświeżeniu strony, bez restartu usługi."""
    return release_helper.load_config()


def projekty(cfg=None):
    """Wszystkie projekty z config.json z dozwolonymi przepływami."""
    cfg = cfg or konfiguracja()
    return {nazwa: list(pc.get("flows", cfg["flows"])) for nazwa, pc in cfg["projects"].items()}


def sprawdz_pare(projekt, flow, cfg=None):
    dozwolone = projekty(cfg)
    if projekt not in dozwolone:
        raise ValueError(f"{projekt}: projekt spoza panelu")
    if flow not in dozwolone[projekt]:
        raise ValueError(f"{projekt}: przepływ {flow} niedostępny (dozwolone: {', '.join(dozwolone[projekt])})")


def wiadomosc(stdout):
    """Tekst między liniami separatora w wyjściu helpera, bez kodów ANSI."""
    linie = _ANSI.sub("", stdout).splitlines()
    sep = [i for i, l in enumerate(linie) if _SEP.match(l)]
    if len(sep) < 2:
        return None
    return "\n".join(linie[sep[-2] + 1:sep[-1]]).strip() or None


def uruchom_projekt(projekt, flow):
    cfg = konfiguracja()
    sprawdz_pare(projekt, flow, cfg)
    sciezka = cfg["projects"][projekt]["path"]
    if cfg["projects"][projekt].get("changelog") == "static":
        return _helper(projekt, flow)   # czyta tylko refy, drzewo robocze go nie dotyczy
    status = subprocess.run(["git", "status", "--porcelain"], cwd=sciezka,
                            capture_output=True, text=True)
    if status.returncode:
        return {"status": "blad", "komunikat": "git status nie działa", "log": status.stderr}
    if status.stdout.strip():
        return {"status": "zablokowany",
                "komunikat": "Repo ma niezacommitowane zmiany. Helper robi checkout, więc najpierw je zacommituj albo schowaj.",
                "log": status.stdout}
    return _helper(projekt, flow)


def _helper(projekt, flow):
    r = subprocess.run([sys.executable, HELPER, "--project", projekt, "--flow", flow],
                       capture_output=True, text=True, cwd=os.path.dirname(HELPER))
    log = _ANSI.sub("", r.stdout + r.stderr)
    tekst = wiadomosc(r.stdout)
    if r.returncode == 0 and tekst:
        return {"status": "ok", "wiadomosc": tekst, "log": log}
    if r.returncode == 1 and tekst:
        return {"status": "brak_zmian", "wiadomosc": tekst, "log": log}
    return {"status": "blad", "komunikat": f"Helper zakończył się kodem {r.returncode}", "log": log[-4000:]}


class Zadania:
    """Jeden release naraz. Projekty idą po kolei, stan czyta strona."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stan = {"flow": None, "projekty": []}
        self._watek = None

    def trwa(self):
        return self._watek is not None and self._watek.is_alive()

    def stan(self):
        with self._lock:
            return json.loads(json.dumps({**self._stan, "trwa": self.trwa()}))

    def start(self, lista, flow):
        with self._lock:
            if self.trwa():
                raise RuntimeError("Release już trwa")
            for p in lista:
                sprawdz_pare(p, flow)
            self._stan = {"flow": flow, "projekty": [{"projekt": p, "status": "czeka"} for p in lista]}
            self._watek = threading.Thread(target=self._praca, args=(lista, flow), daemon=True)
            self._watek.start()

    def _praca(self, lista, flow):
        for i, p in enumerate(lista):
            with self._lock:
                self._stan["projekty"][i]["status"] = "trwa"
            try:
                wynik = uruchom_projekt(p, flow)
            except Exception as e:  # noqa: BLE001 - blad jednego projektu nie zatrzymuje reszty
                wynik = {"status": "blad", "komunikat": str(e)}
            with self._lock:
                self._stan["projekty"][i] = {"projekt": p, **wynik}


def zbuduj_serwer(port, token):
    zadania = Zadania()
    hosty = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Obsluga(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _host_ok(self):
            port_real = self.server.server_address[1]
            return self.headers.get("Host") in hosty | {f"127.0.0.1:{port_real}", f"localhost:{port_real}"}

        def _odpowiedz(self, kod, body, typ="application/json; charset=utf-8"):
            dane = body.encode() if isinstance(body, str) else body
            self.send_response(kod)
            self.send_header("Content-Type", typ)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(dane)))
            self.end_headers()
            self.wfile.write(dane)

        def _json(self, kod, obj):
            self._odpowiedz(kod, json.dumps(obj, ensure_ascii=False))

        def _autoryzacja(self):
            if not self._host_ok():
                self._json(403, {"blad": "niedozwolony Host"})
                return False
            if self.path.startswith("/api/") and not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                self._json(403, {"blad": "brak tokenu"})
                return False
            return True

        def do_GET(self):
            if not self._autoryzacja():
                return
            if self.path == "/":
                try:
                    self._odpowiedz(200, strona(token), "text/html; charset=utf-8")
                except (ValueError, OSError) as e:
                    self._odpowiedz(500, f"Błąd w config.json: {e}", "text/plain; charset=utf-8")
            elif self.path == "/api/stan":
                self._json(200, zadania.stan())
            else:
                self._json(404, {"blad": "nie ma"})

        def do_POST(self):
            if not self._autoryzacja():
                return
            if self.path != "/api/start":
                return self._json(404, {"blad": "nie ma"})
            try:
                dlugosc = int(self.headers.get("Content-Length", 0))
                dane = json.loads(self.rfile.read(min(dlugosc, 10000)) or b"{}")
                lista, flow = dane["projekty"], dane["flow"]
                if not isinstance(lista, list) or not lista:
                    raise ValueError("wybierz co najmniej jeden projekt")
                zadania.start([str(p) for p in lista], str(flow))
            except RuntimeError as e:
                return self._json(409, {"blad": str(e)})
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                return self._json(400, {"blad": str(e)})
            self._json(202, zadania.stan())

    return ThreadingHTTPServer(("127.0.0.1", port), Obsluga)


SZABLON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")


def strona(token):
    """panel.html czytany przy każdym wejściu, tak jak config.json."""
    cfg = konfiguracja()
    flowy = {k: {"source": v["source"], "target": v["target"], "typ": v["typ"]} for k, v in cfg["flows"].items()}
    dane = json.dumps({"token": token, "projekty": projekty(cfg), "flowy": flowy}, ensure_ascii=False)
    with open(SZABLON, encoding="utf-8") as f:
        return f.read().replace("__DANE__", dane.replace("</", "<\\/"))




def main():
    ap = argparse.ArgumentParser(description="Lokalny panel releasów")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bez-przegladarki", action="store_true", help="nie otwieraj przeglądarki")
    a = ap.parse_args()
    token = secrets.token_urlsafe(24)
    srv = zbuduj_serwer(a.port, token)
    adres = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Panel: {adres}  (Ctrl+C kończy)")
    if not a.bez_przegladarki:
        webbrowser.open(adres)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
