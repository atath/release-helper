# release-helper

Skrypt Pythona do automatycznego tworzenia Merge Requestów w GitLabie dla projektów Sandis.
Obsługuje trzy przepływy: `sit → test`, `test → stage` oraz `test → master`.

## Wymagania

- Python 3.8+
- biblioteka `requests`: `pip install requests`
- dostęp SSH do GitLaba (`git@git.sandis.io`)
- Personal Access Token GitLab z uprawnieniem `api`

## Konfiguracja

### 1. Token GitLab

Token można podać na dwa sposoby (skrypt sprawdza w tej kolejności):

```bash
# Zmienna środowiskowa:
export GITLAB_TOKEN=twój_token

# Lub plik (zalecane):
mkdir -p ~/.config/tokens
echo "twój_token" > ~/.config/tokens/gitlab
chmod 600 ~/.config/tokens/gitlab
```

Token tworzysz w GitLab → *User Settings → Access Tokens* → scope: **api**.

### 2. Ścieżki projektów

Skopiuj szablon konfiguracji i uzupełnij lokalne ścieżki:

```bash
cp config.py.dist config.py
```

Następnie edytuj `config.py` — zamień `YOUR_USER` na swoją nazwę użytkownika:

```python
PROJECTS = {
    "jaguarapp-front": {
        "path": "/home/YOUR_USER/projekty/jaguarapp-front",
        ...
    },
    ...
}
```

`config.py` jest w `.gitignore` — Twoje ścieżki nie trafią do repozytorium.

Klucz `flows` ogranicza dostępne przepływy do tych, które projekt realnie wspiera.
Pominięcie klucza oznacza „wszystkie przepływy".

## Użycie

```bash
python release_helper.py --project <projekt> --flow <przepływ>
```

**Projekty:** `jaguarapp-front`, `claims-front`, `d2c-front`, `pelicanapp`  
**Przepływy:** `sit-to-test`, `test-to-stage`, `test-to-master`

Nie każdy przepływ działa w każdym projekcie — progresja gałęzi się różni:

| Projekt | Progresja gałęzi | Dostępne przepływy |
|---|---|---|
| `jaguarapp-front` | sit → test → master | `sit-to-test`, `test-to-master` |
| `d2c-front` | sit → test → master | `sit-to-test`, `test-to-master` |
| `claims-front` | sit → test → stage → master | `sit-to-test`, `test-to-stage` |
| `pelicanapp` | develop → sit → test → master | `sit-to-test`, `test-to-master` |

W claims-front krok `stage → master` robisz ręcznie w GitLabie — skrypt go nie obsługuje.

`pelicanapp` działa inaczej niż projekty frontendowe — patrz [Statyczny CHANGELOG.md](#statyczny-changelogmd).

Wywołanie niedozwolonego przepływu kończy się błędem:

```
release_helper.py: error: flow 'test-to-master' niedostępny dla claims-front
(dozwolone: sit-to-test, test-to-stage)
```

### Przykłady

```bash
python release_helper.py --project jaguarapp-front --flow sit-to-test
python release_helper.py --project jaguarapp-front --flow test-to-master
python release_helper.py --project claims-front --flow test-to-stage
python release_helper.py --project pelicanapp --flow test-to-master
```

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

### test → stage

Tylko `claims-front`. Przebieg identyczny jak `test → master`, inna gałąź docelowa:

1. Sprawdza czy `test` ma commity i realne zmiany względem `stage` (jeśli nie — abort)
2. Tworzy branch `release/test-stage-DDMMYYYY` (lub `-2`, `-3` jeśli już istnieje)
3. Uruchamia `CLG-fe.py` — scala FeatureNotes w RelNote, bumps `package.json`
4. Commituje wynik, pushuje branch i tworzy MRy do `stage`, `test` i `sit`

**Tytuł MRa:** `Release test -> stage vX.Y.Z`

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
