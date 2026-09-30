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
FLOW_TYPES = ("featurenotes", "clg")


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
    parser = argparse.ArgumentParser(description="Tworzy MR w GitLabie")
    parser.add_argument("--project", required=True, choices=PROJECTS.keys())
    parser.add_argument("--flow", required=True, choices=FLOWS)
    args = parser.parse_args(argv)

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
    token = os.environ.get("GITLAB_TOKEN")
    if token:
        return token
    if os.path.isfile(GITLAB_TOKEN_FILE):
        return open(GITLAB_TOKEN_FILE).read().strip()
    raise ValueError(
        f"Brak tokenu GitLab. Ustaw GITLAB_TOKEN lub zapisz token w {GITLAB_TOKEN_FILE}"
    )


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


def run_test_to_stage(project_name):
    return run_clg_flow(project_name, "test-to-stage")


def run_test_to_master(project_name):
    return run_clg_flow(project_name, "test-to-master")


def main():
    args = parse_args()
    if changelog_strategy(args.project) == "static":
        run_static_flow(args.project, args.flow)
    elif FLOW_CONFIG[args.flow]["typ"] == "featurenotes":
        run_featurenotes_flow(args.project, args.flow)
    else:
        run_clg_flow(args.project, args.flow)


if __name__ == "__main__":
    main()
