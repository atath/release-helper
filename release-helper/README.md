# release-helper

Skrypt Pythona do automatycznego tworzenia Merge Requestów w GitLabie dla projektów Sandis.
Obsługuje trzy przepływy: `sit → test`, `test → master` i `master → tag` (wydanie claims-front tagiem).

## Wymagania

- Python 3.8+
- biblioteka `requests`: `pip install requests`
- dostęp SSH do GitLaba (`git@git.sandis.io`)
- Personal Access Token GitLab z uprawnieniem `api`

## Konfiguracja

### 1. Token GitLab

Skrypt czyta token tylko z pliku. Zmiennej `GITLAB_TOKEN` celowo nie obsługuje, bo zmienne
środowiskowe przechodzą do procesów potomnych i łatwo trafiają do logów.

```bash
mkdir -p ~/.config/tokens
echo "twój_token" > ~/.config/tokens/gitlab
chmod 600 ~/.config/tokens/gitlab
```

Token tworzysz w GitLab → *User Settings → Access Tokens* → scope: **api**.

### 2. Projekty i przepływy: `config.json`

Jeden plik dla CLI i panelu. Ścieżki zapisane przez `~`, więc plik nie zawiera nic osobistego
i leży w repozytorium.

- `flows`: przepływy. Każdy ma `source`, `target`, `back_merge` i `typ`:
  - `featurenotes`: opis MR-a z nowych FeatureNotes, bez commita (dziś sit → test),
  - `clg`: `CLG-fe.py` składa RelNote i podbija wersję, commit i MR-y back-merge (dziś test → master),
  - `tag`: tag `vX.Y.Z` na gałęzi źródłowej przez API, bez checkoutu i MR-ów (dziś master → tag w claims-front).
    Projekt może podać `package`, gdy nazwa paczki z `build:vm` różni się od nazwy projektu.
- `projects`: projekty z `path`, `namespace` i listą `flows`. `"changelog": "static"` oznacza statyczny
  `CHANGELOG.md` (pelicanapp). Brak `flows` znaczy „wszystkie przepływy".
- Pole `opis` to komentarz dla człowieka, skrypt go nie czyta.

Nowy przepływ jednego z dwóch typów dodajesz samym wpisem w `config.json`. Skrypt waliduje plik
przy starcie: nieznany typ, brak pola albo projekt z nieistniejącym przepływem kończą się błędem.

Stary `config.py` nie jest już czytany i można go usunąć.

## Użycie

```bash
python release_helper.py --project <projekt> --flow <przepływ>
```

**Projekty:** `jaguarapp-front`, `claims-front`, `d2c-front`, `pelicanapp`  
**Przepływy:** `sit-to-test`, `test-to-master`, `master-to-tag`

Nie każdy przepływ działa w każdym projekcie — progresja gałęzi się różni:

| Projekt | Progresja gałęzi | Dostępne przepływy |
|---|---|---|
| `jaguarapp-front` | sit → test → master | `sit-to-test`, `test-to-master` |
| `d2c-front` | sit → test → master | `sit-to-test`, `test-to-master` |
| `claims-front` | sit → test → master → tag | `sit-to-test`, `test-to-master`, `master-to-tag` |
| `pelicanapp` | develop → sit → test → master | `sit-to-test`, `test-to-master` |

W claims-front produkcja idzie z `master` od 30.10.2026 (wcześniej `stage` i `prod`). Wydanie na produkcję to tag `vX.Y.Z`
na `master`: przepływ `master-to-tag` albo skill `wydaj-wersje-claims` w Claude Code.

`pelicanapp` działa inaczej niż projekty frontendowe — patrz [Statyczny CHANGELOG.md](#statyczny-changelogmd).

Wywołanie niedozwolonego przepływu kończy się błędem:

```
release_helper.py: error: flow 'test-to-master' niedostępny dla <projekt>
(dozwolone: sit-to-test)
```

### Przykłady

```bash
python release_helper.py --project jaguarapp-front --flow sit-to-test
python release_helper.py --project jaguarapp-front --flow test-to-master
python release_helper.py --project claims-front --flow test-to-master
python release_helper.py --project pelicanapp --flow test-to-master
```

## Panel w przeglądarce

Panel działa jako usługa użytkownika w systemd i startuje przy logowaniu. Wchodzisz na
**http://127.0.0.1:8765/** (warto dodać do zakładek), wybierasz przepływ i projekty, klikasz
„Wystaw release”, potem „Potwierdź”. Panel uruchamia `release_helper.py` osobno dla każdego
projektu, po kolei, i pokazuje każdą wiadomość osobno, z przyciskiem „Kopiuj”. Projekt bez zmian
dostaje „brak zmian”, błąd pokazuje log helpera. Projekty i przepływy bierze z `config.json`,
czytanego przy każdym wejściu: po zmianie pliku wystarczy odświeżyć stronę.

- Repo z niezacommitowanymi zmianami jest blokowane przed uruchomieniem helpera, bo helper robi
  `git checkout`. Projekty ze statycznym changelogiem nie są blokowane, bo czytają tylko refy.
- Jeden release naraz. Drugie kliknięcie w trakcie zwraca błąd.
- Serwer słucha tylko na `127.0.0.1`. API wymaga tokenu generowanego przy starcie i sprawdza nagłówek `Host`,
  bo na localhost może wysłać żądanie każda strona otwarta w przeglądarce.

Wygląd strony siedzi w `panel.html`, też czytanym przy każdym wejściu.

Usługa: `~/.config/systemd/user/release-panel.service`.

```bash
systemctl --user status release-panel     # stan
systemctl --user restart release-panel    # tylko po zmianie kodu
journalctl --user -u release-panel -f     # logi
systemctl --user disable --now release-panel   # wyłączenie
```

Ręcznie, bez usługi: `python panel.py` (otwiera przeglądarkę), `--port 9000 --bez-przegladarki`.

## Przepływy

### sit → test

1. Sprawdza czy `sit` ma commity których nie ma `test` (jeśli nie — abort)
2. Sprawdza czy w `sit` są nowe pliki `FeatureNotes/SAN-*.md` względem `test` (jeśli nie — abort)
3. Tworzy branch `release/sit-test-DDMMYYYY` (lub `-2`, `-3` jeśli już istnieje)
4. Agreguje nowe FeatureNotes wg kategorii jako opis MRa
5. Pushuje branch i tworzy MR w GitLabie

**Tytuł MRa:** `Release sit -> test DD.MM.YYYY`

Powstaje jeden MR, bez back-merge do `sit`. Ten przepływ nie dokłada commita,
więc branch release jest kopią `sit` — MR back-merge nie miałby czego wnieść.

### test → master

1. Sprawdza czy `test` ma commity których nie ma `master` (jeśli nie — abort)
2. Tworzy branch `release/test-master-DDMMYYYY` (lub `-2`, `-3` jeśli już istnieje)
3. Uruchamia `CLG-fe.py` — scala FeatureNotes w RelNote, bumps `package.json`
4. Commituje wynik, pushuje branch i tworzy MR w GitLabie

**Tytuł MRa:** `Release test -> master vX.Y.Z`

Skrypt tworzy też MRy back-merge do `test` i `sit`, żeby niższe środowiska nie odstawały.

### master → tag

Tylko `claims-front`. Push na `master` uruchamia w CI `build:vm`, który wysyła paczkę o wersji równej SHA commita.
Tag `vX.Y.Z` uruchamia `release:vm`: ten wydaje paczkę commita z tagu i sam zakłada GitLab Release.

```bash
python release_helper.py --project claims-front --flow master-to-tag --plan            # JSON z planem, bez zapisu
python release_helper.py --project claims-front --flow master-to-tag                   # numer z reguły
python release_helper.py --project claims-front --flow master-to-tag --version v0.10.0
```

1. `git fetch origin --tags`, ostatni tag `vX.Y.Z` i SHA `origin/master` (bez zmian od tagu — abort, kod 1)
2. Numer: same merge'e `hotfix/*` od ostatniego tagu dają patch, każdy inny merge minor. `--version` go nadpisuje,
   ale musi być wyższy niż ostatni tag.
3. Sprawdza w GitLabie `build:vm` dla SHA, paczkę dla SHA i rolę Maintainer (tagi `v*` są chronione). Problem — kod 2.
4. Zakłada tag przez API na SHA, nie na nazwę gałęzi: merge, który wszedłby w międzyczasie, nie trafi do wydania.
5. Czeka do 10 min na `release:vm` i sprawdza, że release ma paczkę z tego SHA.

Release w GitLabie nie oznacza wdrożenia: wgranie paczki na produkcyjną VM to osobny, ręczny krok.
Numeracja tagów jest osobną serią i nie wynika z `package.json` ani z `RelNotes/`.

## Statyczny CHANGELOG.md

Dotyczy projektów z kluczem `"changelog": "static"` w konfiguracji — dziś tylko `pelicanapp`.

Te repozytoria nie mają `FeatureNotes/` ani `CLG-fe.py`. Numer wersji (`VERSION`) i wpis
w `CHANGELOG.md` dopisuje deweloper na branchu feature, więc do momentu release'u wszystko
jest już w gałęzi źródłowej. Skrypt niczego nie generuje i nie commituje — tylko czyta.

Przebieg:

1. Sprawdza czy gałąź źródłowa ma commity i realne zmiany względem docelowej (jeśli nie — abort)
2. Wyciąga z `CHANGELOG.md` wpisy obecne w gałęzi źródłowej, a nieobecne w docelowej
3. Czyta wersję z pliku `VERSION` gałęzi źródłowej
4. Tworzy jeden MR **prosto z gałęzi źródłowej** do docelowej

**Tytuł MRa:** `pelicanapp Release test -> master vX.Y.Z`

Nie powstaje branch `release/...` ani MRy back-merge — bez commita release'owego branch
byłby kopią źródła, a back-merge nie miałby czego wnieść.

Porównywane są **treści wpisów**, nie numery wersji: przy wydaniu ta sama sekcja bywa
przenumerowana (`0.22.0` → `0.23.0`), więc numer nie identyfikuje zmian.

Ponowne uruchomienie na tej samej parze gałęzi nie wywala się — skrypt wykrywa otwarty
już MR (HTTP 409) i zwraca jego adres.

## Format FeatureNotes

Każdy plik `FeatureNotes/SAN-XXXX.md` powinien mieć strukturę:

```
### [Nazwa kategorii]

- SAN-XXXX: opis zmiany
- SAN-XXXX: kolejna zmiana
```

Kategorie (`### [Dodano]`, `### [Poprawiono]`, itp.) są grupowane automatycznie w opisie MRa.

Treść notatek skrypt czyta z gałęzi źródłowej (`git show origin/sit:FeatureNotes/...`),
a nie z katalogu roboczego. Niezacommitowane i niewypchnięte zmiany nie trafią do opisu MRa.

## Testy

```bash
python -m pytest tests/ -v
```
