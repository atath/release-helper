# release-helper

Skrypt Pythona do automatycznego tworzenia Merge Requestów w GitLabie dla projektów frontendowych.
Obsługuje dwa przepływy: `sit → test` oraz `test → master`.

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

## Użycie

```bash
python release_helper.py --project <projekt> --flow <przepływ>
```

**Projekty:** `jaguarapp-front`, `claims-front`, `d2c-front`  
**Przepływy:** `sit-to-test`, `test-to-master`

### Przykłady

```bash
python release_helper.py --project jaguarapp-front --flow sit-to-test
python release_helper.py --project jaguarapp-front --flow test-to-master
```

## Przepływy

### sit → test

1. Sprawdza czy `sit` ma commity których nie ma `test` (jeśli nie — abort)
2. Sprawdza czy w `sit` są nowe pliki `FeatureNotes/SAN-*.md` względem `test` (jeśli nie — abort)
3. Tworzy branch `release/sit-test-DDMMYYYY` (lub `-2`, `-3` jeśli już istnieje)
4. Agreguje nowe FeatureNotes wg kategorii jako opis MRa
5. Pushuje branch i tworzy MR w GitLabie

**Tytuł MRa:** `Release sit -> test DD.MM.YYYY`

### test → master

1. Sprawdza czy `test` ma commity których nie ma `master` (jeśli nie — abort)
2. Tworzy branch `release/test-master-DDMMYYYY` (lub `-2`, `-3` jeśli już istnieje)
3. Uruchamia `CLG-fe.py` — scala FeatureNotes w RelNote, bumps `package.json`
4. Commituje wynik, pushuje branch i tworzy MR w GitLabie

**Tytuł MRa:** `Release test -> master vX.Y.Z`

## Format FeatureNotes

Każdy plik `FeatureNotes/SAN-XXXX.md` powinien mieć strukturę:

```
### [Nazwa kategorii]

- SAN-XXXX: opis zmiany
- SAN-XXXX: kolejna zmiana
```

Kategorie (`### [Dodano]`, `### [Poprawiono]`, itp.) są grupowane automatycznie w opisie MRa.

## Testy

```bash
python -m pytest tests/ -v
```
