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


def flow_nazwy(cfg):
    return {k: f"{v['source']} → {v['target']}" for k, v in cfg["flows"].items()}


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


def strona(token):
    cfg = konfiguracja()
    dane = json.dumps({"token": token, "projekty": projekty(cfg), "flowy": flow_nazwy(cfg)}, ensure_ascii=False)
    return STRONA.replace("__DANE__", dane.replace("</", "<\\/"))


STRONA = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panel releasów</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27%3E%3Ccircle cx=%278%27 cy=%278%27 r=%277%27 fill=%27%232f6fde%27/%3E%3C/svg%3E">
<style>
:root { --bg:#f6f7f9; --karta:#fff; --tekst:#1c2430; --slaby:#667085; --ramka:#dde2ea;
        --akcent:#2f6fde; --ok:#1f8a4c; --uwaga:#b7791f; --blad:#c0362c; --kod:#f1f3f6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#12151b; --karta:#1b2029; --tekst:#e6e9ef; --slaby:#98a2b3; --ramka:#2c3340;
          --akcent:#6b9cff; --ok:#4cc38a; --uwaga:#e0b04f; --blad:#ff7a70; --kod:#141820; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--tekst); font:15px/1.5 system-ui, sans-serif; }
main { max-width:860px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:20px; margin:0 0 4px; }
.pod { color:var(--slaby); margin:0 0 20px; }
section { background:var(--karta); border:1px solid var(--ramka); border-radius:10px; padding:16px; margin-bottom:16px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--slaby); margin:0 0 10px; }
.opcje { display:flex; flex-wrap:wrap; gap:8px; }
.opcje label { display:flex; align-items:center; gap:6px; border:1px solid var(--ramka); border-radius:8px; padding:6px 12px; cursor:pointer; }
.opcje label.wyl { opacity:.45; cursor:not-allowed; }
button { font:inherit; border-radius:8px; border:1px solid var(--ramka); background:var(--karta); color:var(--tekst); padding:8px 14px; cursor:pointer; }
button.glowny { background:var(--akcent); border-color:var(--akcent); color:#fff; font-weight:600; }
button.potwierdz { background:var(--blad); border-color:var(--blad); color:#fff; font-weight:600; }
button:disabled { opacity:.5; cursor:not-allowed; }
.akcje { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.info { color:var(--slaby); font-size:13px; }
.wynik { border-left:4px solid var(--ramka); }
.wynik.ok { border-left-color:var(--ok); } .wynik.brak_zmian { border-left-color:var(--uwaga); }
.wynik.blad, .wynik.zablokowany { border-left-color:var(--blad); } .wynik.trwa { border-left-color:var(--akcent); }
.naglowek { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:8px; }
.naglowek strong { font-size:15px; }
.znak { font-size:12px; font-weight:600; padding:2px 8px; border-radius:99px; background:var(--kod); }
pre { margin:0; background:var(--kod); border-radius:8px; padding:12px; white-space:pre-wrap; word-break:break-word; font:13px/1.45 ui-monospace, monospace; }
details { margin-top:8px; } summary { cursor:pointer; color:var(--slaby); font-size:13px; }
</style>
</head>
<body>
<main>
  <h1>Panel releasów</h1>
  <p class="pod">Każdy projekt to osobne uruchomienie release_helper.py i osobna wiadomość do skopiowania.</p>
  <section>
    <h2>Przepływ</h2>
    <div class="opcje" id="flowy"></div>
  </section>
  <section>
    <h2>Projekty</h2>
    <div class="opcje" id="projekty"></div>
  </section>
  <section>
    <div class="akcje">
      <button class="glowny" id="start">Wystaw release</button>
      <button id="anuluj" hidden>Anuluj</button>
      <span class="info" id="info"></span>
    </div>
  </section>
  <div id="wyniki"></div>
</main>
<script id="dane" type="application/json">__DANE__</script>
<script>
const D = JSON.parse(document.getElementById('dane').textContent);
const $ = id => document.getElementById(id);
const ETYKIETY = {czeka:'czeka', trwa:'w trakcie', ok:'gotowe', brak_zmian:'brak zmian', blad:'błąd', zablokowany:'zablokowany'};
let flow = null, potwierdzanie = false, odpytywanie = null;

function api(sciezka, opcje = {}) {
  return fetch(sciezka, {...opcje, headers: {'X-Token': D.token, 'Content-Type': 'application/json'}})
    .then(async r => ({ok: r.ok, dane: await r.json()}));
}

function rysujFlowy() {
  const wszystkie = new Set(Object.values(D.projekty).flat());
  $('flowy').innerHTML = '';
  Object.entries(D.flowy).filter(([k]) => wszystkie.has(k)).forEach(([k, nazwa]) => {
    const l = document.createElement('label');
    l.innerHTML = `<input type="radio" name="flow" value="${k}"> ${nazwa}`;
    l.querySelector('input').onchange = () => { flow = k; rysujProjekty(); resetPotwierdzenia(); };
    $('flowy').append(l);
  });
}

function rysujProjekty() {
  $('projekty').innerHTML = '';
  Object.entries(D.projekty).forEach(([p, dozwolone]) => {
    const ok = flow && dozwolone.includes(flow);
    const l = document.createElement('label');
    if (!ok) l.className = 'wyl';
    l.title = ok ? '' : (flow ? 'Ten projekt nie ma tego przepływu' : 'Najpierw wybierz przepływ');
    l.innerHTML = `<input type="checkbox" value="${p}" ${ok ? '' : 'disabled'}> ${p}`;
    l.querySelector('input').onchange = resetPotwierdzenia;
    $('projekty').append(l);
  });
}

function wybrane() { return [...document.querySelectorAll('#projekty input:checked')].map(i => i.value); }

function resetPotwierdzenia() {
  potwierdzanie = false;
  $('start').className = 'glowny'; $('start').textContent = 'Wystaw release';
  $('anuluj').hidden = true; $('info').textContent = '';
}

$('anuluj').onclick = resetPotwierdzenia;
$('start').onclick = async () => {
  const lista = wybrane();
  if (!flow || !lista.length) { $('info').textContent = 'Wybierz przepływ i co najmniej jeden projekt.'; return; }
  if (!potwierdzanie) {
    potwierdzanie = true;
    $('start').className = 'potwierdz'; $('start').textContent = 'Potwierdź: wystaw MR-y';
    $('anuluj').hidden = false;
    $('info').textContent = `${D.flowy[flow]} dla: ${lista.join(', ')}. Powstaną gałęzie release i MR-y w GitLabie.`;
    return;
  }
  resetPotwierdzenia();
  const r = await api('/api/start', {method: 'POST', body: JSON.stringify({projekty: lista, flow})});
  if (!r.ok) { $('info').textContent = r.dane.blad; return; }
  rysujWyniki(r.dane); odpytuj();
};

function odpytuj() {
  clearInterval(odpytywanie);
  odpytywanie = setInterval(async () => {
    const r = await api('/api/stan');
    if (!r.ok) return;
    rysujWyniki(r.dane);
    if (!r.dane.trwa) clearInterval(odpytywanie);
  }, 1000);
}

function rysujWyniki(stan) {
  $('start').disabled = stan.trwa;
  $('wyniki').innerHTML = '';
  stan.projekty.forEach(p => {
    const s = document.createElement('section');
    s.className = `wynik ${p.status}`;
    s.innerHTML = `<div class="naglowek"><strong></strong><span class="akcje"><span class="znak"></span></span></div>`;
    s.querySelector('strong').textContent = p.projekt;
    s.querySelector('.znak').textContent = ETYKIETY[p.status] || p.status;
    if (p.wiadomosc) {
      const b = document.createElement('button');
      b.textContent = 'Kopiuj';
      b.onclick = () => navigator.clipboard.writeText(p.wiadomosc)
        .then(() => { b.textContent = 'Skopiowano'; setTimeout(() => b.textContent = 'Kopiuj', 1500); })
        .catch(() => { b.textContent = 'Zaznacz tekst ręcznie'; });
      s.querySelector('.akcje').prepend(b);
      const pre = document.createElement('pre'); pre.textContent = p.wiadomosc; s.append(pre);
    }
    if (p.komunikat) { const d = document.createElement('p'); d.textContent = p.komunikat; s.append(d); }
    if (p.log && p.status !== 'ok') {
      const det = document.createElement('details');
      det.innerHTML = '<summary>Log helpera</summary><pre></pre>';
      det.querySelector('pre').textContent = p.log; s.append(det);
    }
    $('wyniki').append(s);
  });
}

rysujFlowy(); rysujProjekty();
api('/api/stan').then(r => { if (r.ok && r.dane.projekty.length) { rysujWyniki(r.dane); if (r.dane.trwa) odpytuj(); } });
</script>
</body>
</html>
"""


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
