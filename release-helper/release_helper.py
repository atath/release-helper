import argparse
import atexit
import datetime
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
import requests

GITLAB_TOKEN_FILE = os.path.expanduser("~/.config/tokens/gitlab")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Typy przepływów, które skrypt umie wykonać. Nowy przepływ jednego z tych typów
# dodajesz samym wpisem w config.json, bez zmian w kodzie.
#   featurenotes: opis MR-a z nowych FeatureNotes, bez commita (jak sit -> test)
#   clg:          CLG-fe.py skleja RelNote i podbija wersje, commit + MR-y back-merge
#   tag:          tag vX.Y.Z na gałęzi źródłowej przez API; release:vm w CI wydaje paczkę i zakłada release
FLOW_TYPES = ("featurenotes", "clg", "tag")


def load_config(path=None):
    """Czyta i waliduje config.json. Wspolne zrodlo dla CLI i panelu."""
    with open(path or CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    flows = cfg.get("flows") or {}
    for name, fc in flows.items():
        for key in ("source", "target", "back_merge", "typ"):
            if key not in fc:
                raise ValueError(f"config.json: flow '{name}' nie ma pola '{key}'")
        if fc["typ"] not in FLOW_TYPES:
            raise ValueError(f"config.json: flow '{name}' ma typ '{fc['typ']}', dozwolone: {FLOW_TYPES}")
    for name, pc in (cfg.get("projects") or {}).items():
        for key in ("path", "namespace"):
            if key not in pc:
                raise ValueError(f"config.json: projekt '{name}' nie ma pola '{key}'")
        nieznane = [f for f in pc.get("flows", []) if f not in flows]
        if nieznane:
            raise ValueError(f"config.json: projekt '{name}' ma nieznany flow: {', '.join(nieznane)}")
        pc["path"] = os.path.expanduser(pc["path"])
    return cfg


_CONFIG = load_config()
GITLAB_URL = _CONFIG["gitlab_url"]
PROJECTS = _CONFIG["projects"]
# back_merge: gałęzie, do których po wydaniu wraca ten sam branch release,
# żeby środowiska niższe nie odstawały od wyższych. Pusta lista przy
# sit-to-test: ten przepływ nie dokłada commita, więc branch release jest
# kopią sit, a MR back-merge nie miałby czego wnieść.
FLOW_CONFIG = _CONFIG["flows"]
FLOWS = list(FLOW_CONFIG)

# ── ANSI ──────────────────────────────────────────────────────────────────────
_G  = '\033[32m'   # green
_Y  = '\033[33m'   # yellow
_R  = '\033[31m'   # red
_BL = '\033[34m'   # blue
_M  = '\033[35m'   # magenta
_B  = '\033[1m'    # bold
_X  = '\033[0m'    # reset
_CL = '\r\033[K'   # clear line

_PROJECT_COLORS = {
    "jaguarapp-front": _G,
    "d2c-front":       _Y,
    "claims-front":    _BL,
    "pelicanapp":      _M,
}

def _project_color(project_name):
    return _PROJECT_COLORS.get(project_name, _B)


class _Step:
    """Context manager: spinner while running, ✓/✗ on exit."""
    _FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._t0 = None

    def _spin(self):
        for frame in itertools.cycle(self._FRAMES):
            sys.stdout.write(f'{_CL}  {_Y}{frame}{_X} {self.label}')
            sys.stdout.flush()
            if self._stop.wait(0.08):
                break

    def __enter__(self):
        self._t0 = time.time()
        if sys.stdout.isatty():
            self._thread.start()
        return self

    def __exit__(self, exc_type, *_):
        elapsed = time.time() - self._t0
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        if exc_type:
            sys.stdout.write(f'{_CL}  {_R}✗{_X} {self.label}\n')
        else:
            sys.stdout.write(f'{_CL}  {_G}✓{_X} {self.label:<50}({elapsed:.1f}s)\n')
        sys.stdout.flush()
        return False


def _print_header(project_name, flow):
    cfg = FLOW_CONFIG[flow]
    arrow = f"{cfg['source']} → {cfg['target']}"
    print(f'\n  {_B}release-helper{_X}  {project_name}  {arrow}\n')


def _print_no_changes(project_name, message):
    color = _project_color(project_name)
    sep = '─' * 60

    def _deferred():
        print(f'\n  {color}{_B}{sep}{_X}')
        print(f"*{project_name.upper()}* {message}")
        print(f'  {color}{_B}{sep}{_X}\n')

    atexit.register(_deferred)


def _print_result(project_name, title, mr_url, changelog):
    color = _project_color(project_name)
    display_title = title.replace(project_name, project_name.upper(), 1)
    print(f'\n  {_G}{_B}✓ Gotowe{_X}\n')
    sep = '─' * 60
    print(f'  {color}{_B}{sep}{_X}')
    slack_msg = (
        f"*{display_title}*\n"
        f"MR: {mr_url}\n"
        f"\n"
        f"Changelog:\n"
        f"```\n"
        f"{changelog}\n"
        f"```"
    )
    print(slack_msg)
    print(f'  {color}{_B}{sep}{_X}\n')


# ── Git utilities ──────────────────────────────────────────────────────────────

def allowed_flows(project_name):
    return PROJECTS[project_name].get("flows", FLOWS)


def changelog_strategy(project_name):
    """Skąd brać changelog: None = FeatureNotes/CLG-fe.py, "static" = CHANGELOG.md."""
    return PROJECTS[project_name].get("changelog")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Tworzy MR w GitLabie albo tag wydania")
    parser.add_argument("--project", required=True, choices=PROJECTS.keys())
    parser.add_argument("--flow", required=True, choices=FLOWS)
    parser.add_argument("--version", help="numer tagu vX.Y.Z (tylko przepływ typu tag; bez niego numer z reguły)")
    parser.add_argument("--plan", action="store_true",
                        help="tylko przepływ typu tag: JSON z planem wydania z lokalnych refów, bez fetch i bez zapisu")
    args = parser.parse_args(argv)

    is_tag = FLOW_CONFIG[args.flow]["typ"] == "tag"
    if (args.version or args.plan) and not is_tag:
        parser.error("--version i --plan działają tylko z przepływem typu tag")
    if args.version and not TAG_RE.match(args.version):
        parser.error(f"--version {args.version}: oczekiwany format vX.Y.Z")

    # argparse nie potrafi uzależnić choices jednego argumentu od drugiego,
    # więc dostępność flow per projekt sprawdzamy po sparsowaniu.
    allowed = allowed_flows(args.project)
    if args.flow not in allowed:
        parser.error(
            f"flow '{args.flow}' niedostępny dla {args.project} "
            f"(dozwolone: {', '.join(allowed)})"
        )
    return args


def make_branch_name(flow):
    today = datetime.date.today().strftime("%d%m%Y")
    slug = flow.replace("-to-", "-")  # sit-to-test → sit-test
    return f"release/{slug}-{today}"


def has_branch_diff(project_path, source, target):
    result = subprocess.run(
        ["git", "rev-list", "--count", f"origin/{target}..origin/{source}"],
        cwd=project_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip()) > 0


def has_content_diff(project_path, source, target):
    result = subprocess.run(
        ["git", "diff", "--quiet", f"origin/{target}", f"origin/{source}"],
        cwd=project_path,
        capture_output=True,
    )
    return result.returncode != 0


def find_available_branch_name(project_path, branch_name):
    if not branch_exists_on_remote(project_path, branch_name):
        return branch_name
    i = 2
    while True:
        candidate = f"{branch_name}-{i}"
        if not branch_exists_on_remote(project_path, candidate):
            return candidate
        i += 1


def branch_exists_on_remote(project_path, branch_name):
    # Checks local remote-tracking refs — requires prior `git fetch origin`
    result = subprocess.run(
        ["git", "branch", "-r", "--list", f"origin/{branch_name}"],
        cwd=project_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def create_release_branch(project_path, source_branch, branch_name):
    subprocess.run(["git", "checkout", source_branch], cwd=project_path,
                   check=True, capture_output=True)
    subprocess.run(["git", "pull", "origin", source_branch], cwd=project_path,
                   check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", branch_name], cwd=project_path,
                   check=True, capture_output=True)


def push_branch(project_path, branch_name):
    subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        cwd=project_path,
        check=True,
        capture_output=True,
    )


def get_new_feature_note_files(project_path, source="sit", target="test"):
    result = subprocess.run(
        ["git", "diff", f"origin/{target}", f"origin/{source}",
         "--name-only", "--diff-filter=A", "--", "FeatureNotes/"],
        cwd=project_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        os.path.basename(p)
        for p in result.stdout.strip().splitlines()
        if p.strip() and os.path.basename(p.strip()).startswith("SAN-")
    )


def list_feature_notes(project_path, branch):
    """Pliki FeatureNotes/SAN-*.md obecne w gałęzi `branch`."""
    result = subprocess.run(
        ["git", "ls-tree", "--name-only", f"origin/{branch}", "FeatureNotes/"],
        cwd=project_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        os.path.basename(path)
        for path in result.stdout.split()
        if os.path.basename(path).startswith("SAN-")
    ]


def collect_feature_notes(project_path, source, only_files=None, target="test"):
    """Agreguje FeatureNotes z gałęzi `source` wg kategorii.

    Treść czytamy z refu, nie z working tree: listę plików wyznacza
    `get_new_feature_note_files` na podstawie origin/*, więc odczyt musi
    patrzeć na ten sam stan niezależnie od tego, co akurat leży na dysku.
    """
    if only_files is not None:
        files = sorted(f for f in only_files if f.startswith("SAN-"))
    else:
        files = sorted(list_feature_notes(project_path, source))
    if not files:
        raise ValueError("Brak plików FeatureNotes — nie ma co mergować")

    categories = {}
    for filename in files:
        try:
            content = _git_show(project_path, source, f"FeatureNotes/{filename}").strip()
        except subprocess.CalledProcessError:
            raise ValueError(
                f"Brak pliku FeatureNotes/{filename} w gałęzi {source}"
            ) from None
        header, _, notes = content.partition("\n")
        category = header
        if category not in categories:
            categories[category] = []
        categories[category].append(notes.strip())

    today = datetime.date.today().strftime("%d.%m.%Y")
    lines = [f"## Changelog [{source} → {target}] — {today}"]
    for category, notes_list in categories.items():
        lines.append(f"\n{category}\n")
        for note in notes_list:
            lines.append(note)

    return "\n".join(lines)


def run_clg(project_path):
    clg_path = os.path.join(project_path, "CLG-fe.py")
    subprocess.run([sys.executable, clg_path], cwd=project_path,
                   check=True, capture_output=True)

    rel_notes_dir = os.path.join(project_path, "RelNotes")
    _semver_re = re.compile(r"^\d+\.\d+\.\d+\.md$")
    versions = sorted(
        (f for f in os.listdir(rel_notes_dir) if _semver_re.match(f)),
        key=lambda v: tuple(map(int, v.replace(".md", "").split("."))),
    )
    latest_file = versions[-1]
    version = latest_file.replace(".md", "")

    with open(os.path.join(rel_notes_dir, latest_file)) as f:
        content = f.read()

    return version, content


def _git_show(project_path, branch, path):
    result = subprocess.run(
        ["git", "show", f"origin/{branch}:{path}"],
        cwd=project_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _changelog_entries(text):
    """[(kategoria, wpis)] dla każdej linii `- ...` w CHANGELOG.md."""
    entries = []
    category = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("###"):
            category = stripped
        elif stripped.startswith("- "):
            entries.append((category, stripped))
    return entries


def collect_static_changelog(project_path, source, target):
    """Wpisy CHANGELOG.md obecne w `source`, a nieobecne w `target`, + wersja z VERSION.

    Projekty ze statycznym changelogiem (pelicanapp) bumpują CHANGELOG.md i
    VERSION już na branchu feature — skrypt ich nie generuje, tylko czyta.
    Porównujemy treść wpisów, a nie numery wersji: ta sama sekcja bywa przy
    wydaniu przenumerowana, więc numer nie identyfikuje zmian.
    """
    source_entries = _changelog_entries(_git_show(project_path, source, "CHANGELOG.md"))
    target_entries = {
        entry for _, entry in _changelog_entries(_git_show(project_path, target, "CHANGELOG.md"))
    }

    grouped = {}
    for category, entry in source_entries:
        if entry not in target_entries:
            grouped.setdefault(category, []).append(entry)

    if not grouped:
        raise ValueError(f"Brak nowych wpisów w CHANGELOG.md ({source} vs {target})")

    lines = []
    for category, entries in grouped.items():
        if category:
            lines.extend([category, ""])
        lines.extend(entries)
        lines.append("")

    version = _git_show(project_path, source, "VERSION").strip()
    return version, "\n".join(lines).strip()


def commit_release(project_path, version):
    subprocess.run(["git", "add", "-A"], cwd=project_path,
                   check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"chore: release notes {version}"],
        cwd=project_path,
        check=True,
        capture_output=True,
    )


# ── GitLab API ─────────────────────────────────────────────────────────────────

class MergeRequestExistsError(RuntimeError):
    """GitLab odrzucił MR, bo dla tej pary gałęzi jest już otwarty inny."""

    def __init__(self, url):
        super().__init__(f"Otwarty MR już istnieje: {url}")
        self.url = url


def _get_gitlab_token():
    # Tylko z pliku: zmienna środowiskowa przecieka do procesów potomnych i transkryptów.
    if os.path.isfile(GITLAB_TOKEN_FILE):
        with open(GITLAB_TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token
    raise ValueError(f"Brak tokenu GitLab. Zapisz token w {GITLAB_TOKEN_FILE}")


def create_gitlab_mr(namespace, source_branch, target_branch, title, description):
    token = _get_gitlab_token()

    encoded_ns = namespace.replace("/", "%2F")
    url = f"{GITLAB_URL}/api/v4/projects/{encoded_ns}/merge_requests"
    headers = {"PRIVATE-TOKEN": token}
    payload = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": False,
    }

    # GitLab rejestruje nowy branch po `git push` z niewielkim opóźnieniem
    # (Sidekiq/Gitaly) — tuż po pushu MR call bywa szybszy niż ta rejestracja.
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        response = requests.post(url, headers=headers, json=payload)
        if response.ok:
            return response.json()["web_url"]

        if response.status_code == 409:
            existing_iid = re.search(
                r"already exists for this source branch: !(\d+)", response.text
            )
            if existing_iid:
                raise MergeRequestExistsError(
                    f"{GITLAB_URL}/{namespace}/-/merge_requests/{existing_iid.group(1)}"
                )

        is_branch_not_registered_yet = (
            response.status_code == 400
            and "does not exist" in response.text
            and "source_branch" in response.text
        )
        if is_branch_not_registered_yet and attempt < max_attempts:
            time.sleep(attempt * 2)
            continue

        raise RuntimeError(f"GitLab API błąd {response.status_code}: {response.text}")


# ── Flows ──────────────────────────────────────────────────────────────────────

def _create_flow_mrs(project_name, namespace, branch_name, flow, label, description):
    """MR do gałęzi docelowej + MRy back-merge. Zwraca URL MRa docelowego."""
    cfg = FLOW_CONFIG[flow]
    source = cfg["source"]
    target_url = None

    for target in [cfg["target"]] + cfg["back_merge"]:
        with _Step(f"Tworzenie MR → {target}") as step:
            url = create_gitlab_mr(
                namespace=namespace,
                source_branch=branch_name,
                target_branch=target,
                title=f"{project_name} Release {source} -> {target} {label}",
                description=description,
            )
            step.label = f"MR → {target} utworzony"
        if target_url is None:
            target_url = url

    return target_url


def run_sit_to_test(project_name):
    return run_featurenotes_flow(project_name, "sit-to-test")


def run_featurenotes_flow(project_name, flow):
    """Przepływ bez commita: branch release z gałęzi źródłowej, opis MR-a z nowych FeatureNotes."""
    project = PROJECTS[project_name]
    path = project["path"]
    namespace = project["namespace"]
    cfg = FLOW_CONFIG[flow]
    source, target = cfg["source"], cfg["target"]

    _print_header(project_name, flow)

    with _Step("Pobieranie zmian z remote"):
        subprocess.run(["git", "fetch", "origin"], cwd=path,
                       check=True, capture_output=True)

    with _Step(f"Sprawdzanie {source} → {target}") as step:
        if not has_branch_diff(path, source, target):
            step.label = f"Brak commitów w {source} poza {target} — przerywam"
            _print_no_changes(project_name, f"Brak commitów w {source} poza {target}")
            sys.exit(1)
        step.label = f"{source} wyprzedza {target}"

    branch_name = make_branch_name(flow)
    with _Step("Wyznaczanie nazwy brancha") as step:
        branch_name = find_available_branch_name(path, branch_name)
        step.label = f"Branch → {branch_name}"

    with _Step("Sprawdzanie nowych FeatureNotes") as step:
        new_fn_files = get_new_feature_note_files(path, source, target)
        if not new_fn_files:
            step.label = f"Brak nowych zmian w {source} vs {target} — przerywam"
            _print_no_changes(project_name, f"Brak nowych zmian w {source} vs {target}")
            sys.exit(1)
        step.label = f"{len(new_fn_files)} nowych FeatureNotes"

    with _Step(f"Tworzenie brancha {branch_name}"):
        create_release_branch(path, source, branch_name)

    with _Step("Zbieranie FeatureNotes") as step:
        description = collect_feature_notes(path, source, only_files=new_fn_files, target=target)
        step.label = "FeatureNotes zebrane"

    with _Step("Wysyłanie brancha do origin"):
        push_branch(path, branch_name)

    today = datetime.date.today().strftime("%d.%m.%Y")

    mr_url = _create_flow_mrs(
        project_name, namespace, branch_name, flow, today, description
    )

    title = f"{project_name} Release {source} -> {target} {today}"
    _print_result(project_name, title, mr_url, description)
    return mr_url


def run_clg_flow(project_name, flow):
    """Przepływ oparty o CLG-fe.py: bump wersji, RelNote, MR + back-merge."""
    project = PROJECTS[project_name]
    path = project["path"]
    namespace = project["namespace"]
    cfg = FLOW_CONFIG[flow]
    source, target = cfg["source"], cfg["target"]

    _print_header(project_name, flow)

    with _Step("Pobieranie zmian z remote"):
        subprocess.run(["git", "fetch", "origin"], cwd=path,
                       check=True, capture_output=True)

    with _Step(f"Sprawdzanie {source} → {target}") as step:
        if not has_branch_diff(path, source, target):
            step.label = f"Brak commitów w {source} poza {target} — przerywam"
            _print_no_changes(project_name, f"Brak commitów w {source} poza {target}")
            sys.exit(1)
        if not has_content_diff(path, source, target):
            step.label = f"Brak nowych zmian w {source} vs {target} — przerywam"
            _print_no_changes(project_name, f"Brak nowych zmian w {source} vs {target}")
            sys.exit(1)
        step.label = f"{source} wyprzedza {target}"

    branch_name = make_branch_name(flow)
    with _Step("Wyznaczanie nazwy brancha") as step:
        branch_name = find_available_branch_name(path, branch_name)
        step.label = f"Branch → {branch_name}"

    with _Step(f"Tworzenie brancha {branch_name}"):
        create_release_branch(path, source, branch_name)

    with _Step("Uruchamianie CLG-fe.py") as step:
        version, description = run_clg(path)
        step.label = f"CLG-fe.py → v{version}"

    with _Step("Commitowanie release notes"):
        commit_release(path, version)

    with _Step("Wysyłanie brancha do origin"):
        push_branch(path, branch_name)

    mr_url_target = _create_flow_mrs(
        project_name, namespace, branch_name, flow, f"v{version}", description
    )

    title = f"{project_name} Release {source} -> {target} v{version}"
    _print_result(project_name, title, mr_url_target, description)
    return mr_url_target


def run_static_flow(project_name, flow):
    """Projekty ze statycznym CHANGELOG.md: jeden MR prosto z gałęzi źródłowej.

    Nie powstaje commit release'owy, więc branch release byłby kopią źródła,
    a MRy back-merge nie miałyby czego wnieść.
    """
    project = PROJECTS[project_name]
    path = project["path"]
    namespace = project["namespace"]
    cfg = FLOW_CONFIG[flow]
    source, target = cfg["source"], cfg["target"]

    _print_header(project_name, flow)

    with _Step("Pobieranie zmian z remote"):
        subprocess.run(["git", "fetch", "origin"], cwd=path,
                       check=True, capture_output=True)

    with _Step(f"Sprawdzanie {source} → {target}") as step:
        if not has_branch_diff(path, source, target):
            step.label = f"Brak commitów w {source} poza {target} — przerywam"
            _print_no_changes(project_name, f"Brak commitów w {source} poza {target}")
            sys.exit(1)
        if not has_content_diff(path, source, target):
            step.label = f"Brak nowych zmian w {source} vs {target} — przerywam"
            _print_no_changes(project_name, f"Brak nowych zmian w {source} vs {target}")
            sys.exit(1)
        step.label = f"{source} wyprzedza {target}"

    with _Step("Czytanie CHANGELOG.md") as step:
        try:
            version, description = collect_static_changelog(path, source, target)
        except ValueError as error:
            step.label = f"{error} — przerywam"
            _print_no_changes(project_name, str(error))
            sys.exit(1)
        step.label = f"CHANGELOG.md → v{version}"

    with _Step(f"Tworzenie MR → {target}") as step:
        try:
            mr_url = create_gitlab_mr(
                namespace=namespace,
                source_branch=source,
                target_branch=target,
                title=f"{project_name} Release {source} -> {target} v{version}",
                description=description,
            )
            step.label = f"MR → {target} utworzony"
        except MergeRequestExistsError as error:
            mr_url = error.url
            step.label = f"MR → {target} już istnieje — używam go"

    title = f"{project_name} Release {source} -> {target} v{version}"
    _print_result(project_name, title, mr_url, description)
    return mr_url


def run_test_to_master(project_name):
    return run_clg_flow(project_name, "test-to-master")


# ── Tag wydania (claims: release:vm w CI) ─────────────────────────────────────
# Push na gałąź źródłową uruchamia build:vm, który wysyła paczkę generyczną o wersji równej SHA commita.
# Tag vX.Y.Z uruchamia release:vm: ten wydaje paczkę commita z tagu i sam zakłada GitLab Release.
# Wdrożenie wydanej paczki na produkcyjną VM jest osobnym, ręcznym krokiem.

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")   # ten sam wzorzec co reguła release:vm
MAINTAINER = 40
RELEASE_WAIT_S = 600
RELEASE_POLL_S = 10


def _semver(tag):
    m = TAG_RE.match(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _git_out(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


def last_version_tag(path):
    """Najwyższy lokalny tag vX.Y.Z i jego commit albo (None, None)."""
    tags = [t for t in _git_out(path, "tag", "-l", "v*").splitlines() if _semver(t)]
    if not tags:
        return None, None
    last = max(tags, key=_semver)
    return last, _git_out(path, "rev-parse", f"{last}^{{commit}}")


def propose_version(last, merges):
    """Same merge'e gałęzi hotfix/* od ostatniego tagu dają patch, każdy inny merge minor."""
    ma, mi, pa = _semver(last) or (0, 0, 0)
    if merges and all(m.startswith("Merge branch 'hotfix/") for m in merges):
        return f"v{ma}.{mi}.{pa + 1}"
    return f"v{ma}.{mi + 1}.0"


def tag_plan(project_name, flow):
    """Plan wydania z lokalnych refów (bez fetch): ostatni tag, SHA źródła, commity i merge'e od tagu, propozycja."""
    path = PROJECTS[project_name]["path"]
    source = FLOW_CONFIG[flow]["source"]
    sha = _git_out(path, "rev-parse", f"origin/{source}")
    last, last_sha = last_version_tag(path)
    zakres = f"{last}..origin/{source}" if last else f"origin/{source}"
    commits = int(_git_out(path, "rev-list", "--count", zakres))
    merges = [m for m in _git_out(path, "log", "--first-parent", "--merges", "--format=%s", zakres).splitlines() if m]
    tickets = sorted(set(re.findall(r"SAN-\d+", " ".join(merges))), key=lambda t: int(t[4:]))
    return {"ostatni": last, "ostatni_sha": last_sha, "sha": sha, "commity": commits, "merge": merges,
            "zadania": tickets, "proponowany": propose_version(last, merges)}


def _gitlab(method, path, **kwargs):
    url = f"{GITLAB_URL}/api/v4/{path}"
    response = requests.request(method, url, headers={"PRIVATE-TOKEN": _get_gitlab_token()}, timeout=30, **kwargs)
    if response.status_code == 404 and method == "GET":
        return None
    if not response.ok:
        raise RuntimeError(f"GitLab API {method} {path}: {response.status_code} {response.text[:300]}")
    return response.json()


def release_problems(namespace, package, source, sha):
    """Powody, dla których tag nie wyda paczki: build:vm, paczka dla SHA, uprawnienia do chronionych tagów v*."""
    ns = namespace.replace("/", "%2F")
    problems = []
    pipes = _gitlab("GET", f"projects/{ns}/pipelines", params={"sha": sha, "ref": source, "per_page": 5}) or []
    job = None
    if pipes:
        jobs = _gitlab("GET", f"projects/{ns}/pipelines/{pipes[0]['id']}/jobs", params={"per_page": 50}) or []
        job = next((j for j in jobs if j["name"] == "build:vm"), None)
    if job is None:
        problems.append(f"brak joba build:vm dla {sha[:8]} na {source}")
    elif job["status"] in ("created", "pending", "running"):
        problems.append(f"build:vm dla {sha[:8]} jeszcze trwa, spróbuj za kilka minut")
    elif job["status"] != "success":
        problems.append(f"build:vm dla {sha[:8]} ma status {job['status']}")
    pkgs = _gitlab("GET", f"projects/{ns}/packages", params={"package_name": package, "package_type": "generic",
                                                             "per_page": 100, "order_by": "created_at", "sort": "desc"}) or []
    if not any(p["name"] == package and p["version"] == sha for p in pkgs) and job and job["status"] == "success":
        problems.append(f"brak paczki {package} dla {sha[:8]}, release:vm nie miałby czego wydać")
    me = _gitlab("GET", "user")
    member = _gitlab("GET", f"projects/{ns}/members/all/{me['id']}") if me else None
    if not member or member["access_level"] < MAINTAINER:
        problems.append("tagi v* są chronione (Maintainer), Twój token nie ma tego dostępu")
    return problems


def wait_for_release(namespace, version, sha, timeout=RELEASE_WAIT_S, interval=RELEASE_POLL_S, sleep=time.sleep):
    """Czeka na release:vm z pipeline'u tagu. Zwraca (status joba, url release'u | None, url pipeline'u | None)."""
    ns = namespace.replace("/", "%2F")
    waited, status, pipe_url = 0, "brak pipeline'u", None
    while True:
        pipes = _gitlab("GET", f"projects/{ns}/pipelines", params={"ref": version, "per_page": 5}) or []
        if pipes:
            pipe_url = pipes[0]["web_url"]
            jobs = _gitlab("GET", f"projects/{ns}/pipelines/{pipes[0]['id']}/jobs") or []
            job = next((j for j in jobs if j["name"] == "release:vm"), None)
            status = job["status"] if job else "brak joba release:vm"
            if job and status not in ("created", "pending", "running", "waiting_for_resource", "preparing"):
                break
        if waited >= timeout:
            return f"{status} po {timeout // 60} min", None, pipe_url
        sleep(interval)
        waited += interval
    release = _gitlab("GET", f"projects/{ns}/releases/{version}") if status == "success" else None
    links = [l["url"] for l in (release or {}).get("assets", {}).get("links", [])]
    if release and not any(sha in u for u in links):
        return "release bez paczki z tego commita", release["_links"]["self"], pipe_url
    return status, (release or {}).get("_links", {}).get("self"), pipe_url


def _print_tag_result(project_name, version, sha, release_url, pipe_url, tickets):
    color = _project_color(project_name)
    print(f'\n  {_G}{_B}✓ Gotowe{_X}\n')
    sep = '─' * 60
    zadania = ", ".join(tickets) or "brak numerów SAN w merge'ach"
    print(f'  {color}{_B}{sep}{_X}')
    print(f"*{project_name.upper()} {version}* wydane z {sha[:8]}\n"
          f"Release: {release_url}\n"
          f"Pipeline: {pipe_url}\n"
          f"Zadania: {zadania}\n"
          f"Paczka wydana w GitLabie. Wdrożenie na produkcyjną VM to osobny krok.")
    print(f'  {color}{_B}{sep}{_X}\n')


def run_tag_flow(project_name, flow, version=None, sleep=time.sleep):
    project = PROJECTS[project_name]
    path, namespace = project["path"], project["namespace"]
    package = project.get("package", project_name)
    source = FLOW_CONFIG[flow]["source"]

    _print_header(project_name, flow)

    with _Step("Pobieranie zmian i tagów z remote"):
        subprocess.run(["git", "fetch", "origin", "--tags"], cwd=path, check=True, capture_output=True)

    with _Step(f"Plan wydania z {source}") as step:
        plan = tag_plan(project_name, flow)
        if plan["commity"] == 0 or plan["ostatni_sha"] == plan["sha"]:
            step.label = f"{source} bez zmian od {plan['ostatni']} — przerywam"
            _print_no_changes(project_name, f"Brak zmian w {source} od {plan['ostatni']}")
            sys.exit(1)
        version = version or plan["proponowany"]
        if plan["ostatni"] and _semver(version) <= _semver(plan["ostatni"]):
            step.label = f"{version} nie jest wyższy niż {plan['ostatni']} — przerywam"
            print(f"\n  {_R}{version} musi być wyższy niż ostatni tag {plan['ostatni']}{_X}", file=sys.stderr)
            sys.exit(2)
        step.label = f"{plan['ostatni'] or 'brak tagu'} → {version} na {plan['sha'][:8]} ({plan['commity']} commitów)"

    with _Step("Sprawdzanie build:vm, paczki i uprawnień") as step:
        problems = release_problems(namespace, package, source, plan["sha"])
        if problems:
            step.label = "Wydanie zablokowane — przerywam"
            for p in problems:
                print(f"  {_R}✗ {p}{_X}", file=sys.stderr)
            sys.exit(2)
        step.label = "build:vm, paczka i uprawnienia w porządku"

    with _Step(f"Tworzenie tagu {version} na {plan['sha'][:8]}"):
        # ref = SHA, nie nazwa gałęzi: merge, który wszedłby w międzyczasie, nie trafi do wydania bez sprawdzenia
        _gitlab("POST", f"projects/{namespace.replace('/', '%2F')}/repository/tags",
                json={"tag_name": version, "ref": plan["sha"]})

    with _Step("Czekanie na release:vm") as step:
        status, release_url, pipe_url = wait_for_release(namespace, version, plan["sha"], sleep=sleep)
        step.label = f"release:vm: {status}"
    if status != "success" or not release_url:
        print(f"\n  {_R}Tag {version} założony, ale wydanie nie skończyło się sukcesem: {status}. "
              f"Pipeline: {pipe_url}{_X}", file=sys.stderr)
        sys.exit(2)

    _print_tag_result(project_name, version, plan["sha"], release_url, pipe_url, plan["zadania"])
    return release_url


def main():
    args = parse_args()
    if FLOW_CONFIG[args.flow]["typ"] == "tag":
        if args.plan:
            print(json.dumps(tag_plan(args.project, args.flow), ensure_ascii=False))
        else:
            run_tag_flow(args.project, args.flow, args.version)
    elif changelog_strategy(args.project) == "static":
        run_static_flow(args.project, args.flow)
    elif FLOW_CONFIG[args.flow]["typ"] == "featurenotes":
        run_featurenotes_flow(args.project, args.flow)
    else:
        run_clg_flow(args.project, args.flow)


if __name__ == "__main__":
    main()
