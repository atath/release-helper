import argparse
import datetime
import itertools
import os
import re
import subprocess
import sys
import threading
import time
import requests

from config import GITLAB_URL, PROJECTS

GITLAB_TOKEN_FILE = os.path.expanduser("~/.config/tokens/gitlab")

FLOWS = ["sit-to-test", "test-to-master"]

FLOW_CONFIG = {
    "sit-to-test":  {"source": "sit",  "target": "test"},
    "test-to-master": {"source": "test", "target": "master"},
}

# ── ANSI ──────────────────────────────────────────────────────────────────────
_G  = '\033[32m'   # green
_Y  = '\033[33m'   # yellow
_R  = '\033[31m'   # red
_B  = '\033[1m'    # bold
_X  = '\033[0m'    # reset
_CL = '\r\033[K'   # clear line


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
    arrow = 'sit → test' if flow == 'sit-to-test' else 'test → master'
    print(f'\n  {_B}release-helper{_X}  {project_name}  {arrow}\n')


def _print_result(title, mr_url):
    print(f'\n  {_G}{_B}✓ Done{_X}\n')
    print(f'    {_B}{title}{_X}')
    print(f'    {mr_url}\n')


# ── Git utilities ──────────────────────────────────────────────────────────────

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Tworzy MR w GitLabie")
    parser.add_argument("--project", required=True, choices=PROJECTS.keys())
    parser.add_argument("--flow", required=True, choices=FLOWS)
    return parser.parse_args(argv)


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


def get_new_feature_note_files(project_path):
    result = subprocess.run(
        ["git", "diff", "origin/test", "origin/sit",
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


def collect_feature_notes(project_path, only_files=None):
    fn_dir = os.path.join(project_path, "FeatureNotes")
    if not os.path.isdir(fn_dir):
        raise ValueError(f"Katalog FeatureNotes nie istnieje: {fn_dir}")
    if only_files is not None:
        files = sorted(f for f in only_files if f.startswith("SAN-"))
    else:
        files = sorted(f for f in os.listdir(fn_dir) if f.startswith("SAN-"))
    if not files:
        raise ValueError("Brak plików FeatureNotes — nie ma co mergować")

    categories = {}
    for filename in files:
        with open(os.path.join(fn_dir, filename)) as f:
            content = f.read().strip()
        header, _, notes = content.partition("\n")
        category = header
        if category not in categories:
            categories[category] = []
        categories[category].append(notes.strip())

    today = datetime.date.today().strftime("%d.%m.%Y")
    lines = [f"## Changelog [sit → test] — {today}"]
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

    response = requests.post(url, headers=headers, json=payload)
    if not response.ok:
        raise RuntimeError(f"GitLab API błąd {response.status_code}: {response.text}")

    return response.json()["web_url"]


# ── Flows ──────────────────────────────────────────────────────────────────────

def run_sit_to_test(project_name):
    project = PROJECTS[project_name]
    path = project["path"]
    namespace = project["namespace"]
    source, target = "sit", "test"

    _print_header(project_name, "sit-to-test")

    with _Step("Fetching remote"):
        subprocess.run(["git", "fetch", "origin"], cwd=path,
                       check=True, capture_output=True)

    with _Step(f"Checking {source} → {target}") as step:
        if not has_branch_diff(path, source, target):
            step.label = f"No commits in {source} not in {target} — aborting"
            sys.exit(1)
        step.label = f"{source} is ahead of {target}"

    branch_name = make_branch_name("sit-to-test")
    with _Step("Resolving branch name") as step:
        branch_name = find_available_branch_name(path, branch_name)
        step.label = f"Branch → {branch_name}"

    with _Step("Checking new FeatureNotes") as step:
        new_fn_files = get_new_feature_note_files(path)
        if not new_fn_files:
            step.label = "Brak nowych FeatureNotes w sit vs test — aborting"
            sys.exit(1)
        step.label = f"{len(new_fn_files)} nowych FeatureNotes"

    with _Step(f"Creating branch {branch_name}"):
        create_release_branch(path, source, branch_name)

    with _Step("Collecting FeatureNotes") as step:
        description = collect_feature_notes(path, only_files=new_fn_files)
        step.label = "FeatureNotes collected"

    with _Step("Pushing branch to origin"):
        push_branch(path, branch_name)

    today = datetime.date.today().strftime("%d.%m.%Y")
    title = f"Release sit -> test {today}"
    with _Step("Creating MR") as step:
        mr_url = create_gitlab_mr(
            namespace=namespace,
            source_branch=branch_name,
            target_branch=target,
            title=title,
            description=description,
        )
        step.label = "MR created"

    _print_result(title, mr_url)
    return mr_url


def run_test_to_master(project_name):
    project = PROJECTS[project_name]
    path = project["path"]
    namespace = project["namespace"]
    source, target = "test", "master"

    _print_header(project_name, "test-to-master")

    with _Step("Fetching remote"):
        subprocess.run(["git", "fetch", "origin"], cwd=path,
                       check=True, capture_output=True)

    with _Step(f"Checking {source} → {target}") as step:
        if not has_branch_diff(path, source, target):
            step.label = f"No commits in {source} not in {target} — aborting"
            sys.exit(1)
        step.label = f"{source} is ahead of {target}"

    branch_name = make_branch_name("test-to-master")
    with _Step("Resolving branch name") as step:
        branch_name = find_available_branch_name(path, branch_name)
        step.label = f"Branch → {branch_name}"

    with _Step(f"Creating branch {branch_name}"):
        create_release_branch(path, source, branch_name)

    with _Step("Running CLG-fe.py") as step:
        version, description = run_clg(path)
        step.label = f"CLG-fe.py → v{version}"

    with _Step("Committing release notes"):
        commit_release(path, version)

    with _Step("Pushing branch to origin"):
        push_branch(path, branch_name)

    title = f"Release test -> master v{version}"
    with _Step("Creating MR") as step:
        mr_url = create_gitlab_mr(
            namespace=namespace,
            source_branch=branch_name,
            target_branch=target,
            title=title,
            description=description,
        )
        step.label = "MR created"

    _print_result(title, mr_url)
    return mr_url


def main():
    args = parse_args()
    if args.flow == "sit-to-test":
        run_sit_to_test(args.project)
    elif args.flow == "test-to-master":
        run_test_to_master(args.project)


if __name__ == "__main__":
    main()
