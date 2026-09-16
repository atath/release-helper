import pytest
import sys
import tempfile
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import release_helper


def test_parse_args_valid():
    args = release_helper.parse_args(["--project", "jaguarapp-front", "--flow", "sit-to-test"])
    assert args.project == "jaguarapp-front"
    assert args.flow == "sit-to-test"


def test_parse_args_invalid_project():
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "nonexistent", "--flow", "sit-to-test"])


def test_parse_args_invalid_flow():
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "jaguarapp-front", "--flow", "bad-flow"])


def test_has_branch_diff_true():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="5\n", returncode=0)
        result = release_helper.has_branch_diff("/fake/path", "sit", "test")
    assert result is True
    mock_run.assert_called_once_with(
        ["git", "rev-list", "--count", "origin/test..origin/sit"],
        cwd="/fake/path",
        capture_output=True,
        text=True,
        check=True,
    )


def test_has_branch_diff_false():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="0\n", returncode=0)
        result = release_helper.has_branch_diff("/fake/path", "sit", "test")
    assert result is False


def test_branch_exists_on_remote_true():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="  origin/release/sit-test-23042026\n")
        result = release_helper.branch_exists_on_remote("/fake/path", "release/sit-test-23042026")
    assert result is True
    mock_run.assert_called_once_with(
        ["git", "branch", "-r", "--list", "origin/release/sit-test-23042026"],
        cwd="/fake/path",
        capture_output=True,
        text=True,
        check=True,
    )


def test_find_available_branch_name_free():
    with patch("release_helper.branch_exists_on_remote", return_value=False):
        result = release_helper.find_available_branch_name("/fake/path", "release/sit-test-23042026")
    assert result == "release/sit-test-23042026"


def test_find_available_branch_name_iterator_2():
    with patch("release_helper.branch_exists_on_remote", side_effect=[True, False]):
        result = release_helper.find_available_branch_name("/fake/path", "release/sit-test-23042026")
    assert result == "release/sit-test-23042026-2"


def test_find_available_branch_name_iterator_3():
    with patch("release_helper.branch_exists_on_remote", side_effect=[True, True, False]):
        result = release_helper.find_available_branch_name("/fake/path", "release/sit-test-23042026")
    assert result == "release/sit-test-23042026-3"


def test_branch_exists_on_remote_false():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="")
        result = release_helper.branch_exists_on_remote("/fake/path", "release/sit-test-23042026")
    assert result is False


def test_make_branch_name_sit_to_test():
    with patch("release_helper.datetime") as mock_dt:
        mock_dt.date.today.return_value.strftime.return_value = "23042026"
        result = release_helper.make_branch_name("sit-to-test")
    assert result == "release/sit-test-23042026"


def test_make_branch_name_test_to_master():
    with patch("release_helper.datetime") as mock_dt:
        mock_dt.date.today.return_value.strftime.return_value = "23042026"
        result = release_helper.make_branch_name("test-to-master")
    assert result == "release/test-master-23042026"


def test_create_release_branch_calls_git_commands():
    with patch("release_helper.subprocess.run") as mock_run:
        release_helper.create_release_branch("/fake/path", "sit", "release/sit-test-23042026")
    assert mock_run.call_count == 3
    calls = mock_run.call_args_list
    assert calls[0][0][0] == ["git", "checkout", "sit"]
    assert calls[1][0][0] == ["git", "pull", "origin", "sit"]
    assert calls[2][0][0] == ["git", "checkout", "-b", "release/sit-test-23042026"]


def test_push_branch_calls_git_push():
    with patch("release_helper.subprocess.run") as mock_run:
        release_helper.push_branch("/fake/path", "release/sit-test-23042026")
    mock_run.assert_called_once_with(
        ["git", "push", "-u", "origin", "release/sit-test-23042026"],
        cwd="/fake/path",
        check=True,
        capture_output=True,
    )


def test_get_new_feature_note_files():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="FeatureNotes/SAN-100.md\nFeatureNotes/SAN-101.md\n")
        result = release_helper.get_new_feature_note_files("/fake/path")
    assert result == ["SAN-100.md", "SAN-101.md"]
    mock_run.assert_called_once_with(
        ["git", "diff", "origin/test", "origin/sit", "--name-only", "--diff-filter=A", "--", "FeatureNotes/"],
        cwd="/fake/path",
        capture_output=True,
        text=True,
        check=True,
    )


def test_get_new_feature_note_files_empty():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="")
        result = release_helper.get_new_feature_note_files("/fake/path")
    assert result == []


def _make_feature_notes(tmp_dir, files):
    fn_dir = os.path.join(tmp_dir, "FeatureNotes")
    os.makedirs(fn_dir)
    for name, content in files.items():
        with open(os.path.join(fn_dir, name), "w") as f:
            f.write(content)
    return tmp_dir


def test_collect_feature_notes_aggregates_by_category():
    with tempfile.TemporaryDirectory() as tmp:
        _make_feature_notes(tmp, {
            "SAN-100.md": "### [Poprawiono]\n\n- SAN-100: fix A\n",
            "SAN-101.md": "### [Dodano]\n\n- SAN-101: add B\n",
            "SAN-102.md": "### [Poprawiono]\n\n- SAN-102: fix C\n",
        })
        result = release_helper.collect_feature_notes(tmp)
    assert "### [Poprawiono]" in result
    assert "### [Dodano]" in result
    assert "- SAN-100: fix A" in result
    assert "- SAN-101: add B" in result
    assert "- SAN-102: fix C" in result


def test_collect_feature_notes_with_only_files():
    with tempfile.TemporaryDirectory() as tmp:
        _make_feature_notes(tmp, {
            "SAN-100.md": "### [Poprawiono]\n\n- SAN-100: fix A\n",
            "SAN-101.md": "### [Dodano]\n\n- SAN-101: add B\n",
            "SAN-102.md": "### [Poprawiono]\n\n- SAN-102: fix C\n",
        })
        result = release_helper.collect_feature_notes(tmp, only_files=["SAN-100.md"])
    assert "- SAN-100: fix A" in result
    assert "- SAN-101: add B" not in result
    assert "- SAN-102: fix C" not in result


def test_collect_feature_notes_raises_when_empty():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "FeatureNotes"))
        with pytest.raises(ValueError, match="Brak plików FeatureNotes"):
            release_helper.collect_feature_notes(tmp)


def test_collect_feature_notes_raises_when_no_directory():
    with tempfile.TemporaryDirectory() as tmp:
        # tmp dir exists but has no FeatureNotes subdirectory
        with pytest.raises(ValueError, match="Katalog FeatureNotes nie istnieje"):
            release_helper.collect_feature_notes(tmp)


def test_run_clg_returns_version_and_content():
    with tempfile.TemporaryDirectory() as tmp:
        rel_dir = os.path.join(tmp, "RelNotes")
        os.makedirs(rel_dir)

        def fake_clg(*args, **kwargs):
            with open(os.path.join(rel_dir, "2.9.8.md"), "w") as f:
                f.write("2.9.8 - 23.04.2026\n====================\n\n### [Poprawiono]\n\n- SAN-100: fix\n")

        with patch("release_helper.subprocess.run", side_effect=fake_clg):
            version, content = release_helper.run_clg(tmp)

    assert version == "2.9.8"
    assert "2.9.8 - 23.04.2026" in content


def test_commit_release_runs_git_commands():
    with patch("release_helper.subprocess.run") as mock_run:
        release_helper.commit_release("/fake/path", "2.9.8")
    mock_run.assert_any_call(["git", "add", "-A"], cwd="/fake/path", check=True, capture_output=True)
    mock_run.assert_any_call(
        ["git", "commit", "-m", "chore: release notes 2.9.8"],
        cwd="/fake/path",
        check=True,
        capture_output=True,
    )


def test_create_gitlab_mr_returns_url():
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = {"web_url": "https://git.sandis.io/sandis/jaguarapp-front/-/merge_requests/99"}

    with patch("release_helper.requests.post", return_value=mock_response):
        with patch.dict(os.environ, {"GITLAB_TOKEN": "test-token"}):
            url = release_helper.create_gitlab_mr(
                namespace="sandis/jaguarapp-front",
                source_branch="release/sit-test-23042026",
                target_branch="test",
                title="Release sit -> test 23.04.2026",
                description="## Changelog",
            )
    assert url == "https://git.sandis.io/sandis/jaguarapp-front/-/merge_requests/99"


def test_create_gitlab_mr_raises_on_missing_token():
    with patch("release_helper._get_gitlab_token", side_effect=ValueError("GITLAB_TOKEN")):
        with pytest.raises(ValueError, match="GITLAB_TOKEN"):
            release_helper.create_gitlab_mr("ns/proj", "src", "tgt", "title", "desc")


def test_create_gitlab_mr_raises_on_api_error():
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 422
    mock_response.text = "Branch already exists"

    with patch("release_helper.requests.post", return_value=mock_response):
        with patch.dict(os.environ, {"GITLAB_TOKEN": "test-token"}):
            with pytest.raises(RuntimeError, match="422"):
                release_helper.create_gitlab_mr("ns/proj", "src", "tgt", "title", "desc")


def test_run_sit_to_test_full_flow():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.branch_exists_on_remote", return_value=False), \
         patch("release_helper.get_new_feature_note_files", return_value=["SAN-100.md"]), \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.collect_feature_notes", return_value="## Changelog\n- SAN-100: fix"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/1") as mock_mr, \
         patch("release_helper.datetime") as mock_dt:
        mock_dt.date.today.return_value.strftime.side_effect = lambda fmt: "23042026" if fmt == "%d%m%Y" else "23.04.2026"
        url = release_helper.run_sit_to_test("jaguarapp-front")

    assert url == "https://git.sandis.io/mr/1"
    targets = [c[1]["target_branch"] for c in mock_mr.call_args_list]
    assert targets == ["test"]
    title = mock_mr.call_args_list[0][1]["title"]
    assert "sit -> test" in title
    assert "23.04.2026" in title


def test_run_sit_to_test_aborts_on_no_diff():
    with patch("release_helper.has_branch_diff", return_value=False):
        with pytest.raises(SystemExit):
            release_helper.run_sit_to_test("jaguarapp-front")


def test_run_sit_to_test_uses_iterator_on_existing_branch():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.find_available_branch_name", return_value="release/sit-test-23042026-2") as mock_find, \
         patch("release_helper.get_new_feature_note_files", return_value=["SAN-100.md"]), \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.collect_feature_notes", return_value="## Changelog"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/1"), \
         patch("release_helper.datetime") as mock_dt:
        mock_dt.date.today.return_value.strftime.side_effect = lambda fmt: "23042026" if fmt == "%d%m%Y" else "23.04.2026"
        release_helper.run_sit_to_test("jaguarapp-front")
    mock_find.assert_called_once()


def test_run_sit_to_test_aborts_on_no_new_feature_notes():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.branch_exists_on_remote", return_value=False), \
         patch("release_helper.get_new_feature_note_files", return_value=[]):
        with pytest.raises(SystemExit):
            release_helper.run_sit_to_test("jaguarapp-front")


def test_has_content_diff_true():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        result = release_helper.has_content_diff("/fake/path", "test", "master")
    assert result is True
    mock_run.assert_called_once_with(
        ["git", "diff", "--quiet", "origin/master", "origin/test"],
        cwd="/fake/path",
        capture_output=True,
    )


def test_has_content_diff_false():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = release_helper.has_content_diff("/fake/path", "test", "master")
    assert result is False


def test_run_test_to_master_full_flow():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.branch_exists_on_remote", return_value=False), \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.run_clg", return_value=("2.9.8", "2.9.8 - 23.04.2026\n...")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/2") as mock_mr:
        url = release_helper.run_test_to_master("jaguarapp-front")

    assert url == "https://git.sandis.io/mr/2"
    targets = [c[1]["target_branch"] for c in mock_mr.call_args_list]
    assert targets == ["master", "test", "sit"]
    title = mock_mr.call_args_list[0][1]["title"]
    assert "test -> master" in title
    assert "v2.9.8" in title


def test_run_test_to_master_aborts_on_no_diff():
    with patch("release_helper.has_branch_diff", return_value=False):
        with pytest.raises(SystemExit):
            release_helper.run_test_to_master("jaguarapp-front")


def test_run_test_to_master_aborts_on_no_content_diff():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=False):
        with pytest.raises(SystemExit):
            release_helper.run_test_to_master("jaguarapp-front")


def test_run_test_to_master_uses_iterator_on_existing_branch():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.find_available_branch_name", return_value="release/test-master-23042026-2") as mock_find, \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.run_clg", return_value=("2.9.8", "content")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/2"):
        release_helper.run_test_to_master("jaguarapp-front")
    mock_find.assert_called_once()


# ── test-to-stage ──────────────────────────────────────────────────────────────

def test_make_branch_name_test_to_stage():
    with patch("release_helper.datetime") as mock_dt:
        mock_dt.date.today.return_value.strftime.return_value = "23042026"
        result = release_helper.make_branch_name("test-to-stage")
    assert result == "release/test-stage-23042026"


def test_allowed_flows_reads_project_config():
    assert release_helper.allowed_flows("claims-front") == ["sit-to-test", "test-to-stage"]
    assert release_helper.allowed_flows("jaguarapp-front") == ["sit-to-test", "test-to-master"]


def test_allowed_flows_falls_back_to_all_flows():
    with patch.dict(release_helper.PROJECTS, {"legacy": {"path": "/x", "namespace": "n"}}):
        assert release_helper.allowed_flows("legacy") == release_helper.FLOWS


def test_parse_args_accepts_test_to_stage_for_claims_front():
    args = release_helper.parse_args(["--project", "claims-front", "--flow", "test-to-stage"])
    assert args.flow == "test-to-stage"


def test_parse_args_rejects_test_to_master_for_claims_front():
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "claims-front", "--flow", "test-to-master"])


def test_parse_args_rejects_test_to_stage_for_jaguarapp_front():
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "jaguarapp-front", "--flow", "test-to-stage"])


def test_run_test_to_stage_full_flow():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.branch_exists_on_remote", return_value=False), \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.run_clg", return_value=("0.2.0", "0.2.0 - 23.04.2026\n...")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/3") as mock_mr:
        url = release_helper.run_test_to_stage("claims-front")

    assert url == "https://git.sandis.io/mr/3"
    targets = [c[1]["target_branch"] for c in mock_mr.call_args_list]
    assert targets == ["stage", "test", "sit"]
    titles = [c[1]["title"] for c in mock_mr.call_args_list]
    assert titles[0] == "claims-front Release test -> stage v0.2.0"
    assert titles[1] == "claims-front Release test -> test v0.2.0"
    assert titles[2] == "claims-front Release test -> sit v0.2.0"


def test_run_test_to_stage_branches_off_test():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=True) as mock_diff, \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.branch_exists_on_remote", return_value=False), \
         patch("release_helper.create_release_branch") as mock_create, \
         patch("release_helper.run_clg", return_value=("0.2.0", "content")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/3"):
        release_helper.run_test_to_stage("claims-front")

    assert mock_diff.call_args[0][1:] == ("test", "stage")
    assert mock_create.call_args[0][1] == "test"


def test_run_test_to_stage_aborts_on_no_diff():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=False):
        with pytest.raises(SystemExit):
            release_helper.run_test_to_stage("claims-front")


def test_run_test_to_stage_aborts_on_no_content_diff():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=False):
        with pytest.raises(SystemExit):
            release_helper.run_test_to_stage("claims-front")


def test_run_test_to_stage_uses_iterator_on_existing_branch():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.find_available_branch_name",
               return_value="release/test-stage-23042026-2") as mock_find, \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.run_clg", return_value=("0.2.0", "content")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/3"):
        release_helper.run_test_to_stage("claims-front")
    mock_find.assert_called_once()


def test_print_header_derives_arrow_from_flow_config(capsys):
    release_helper._print_header("claims-front", "test-to-stage")
    assert "test → stage" in capsys.readouterr().out


# ── pelicanapp: statyczny CHANGELOG.md ─────────────────────────────────────────

_CHANGELOG_TEST = """CHANGELOG - PelicanApp
====================

0.22.0 - 17.06.2025
===================

### [Dodano]

- SAN-16560: blokowanie użytkownika

0.21.0 - 20.05.2025
===================

### [Dodano]

- SAN-15724: zapisywanie minipolis
"""

# W sit ta sama sekcja bywa przenumerowana (0.22.0 → 0.23.0), a pod nią ląduje
# nowa sekcja z innym numerem — dlatego porównujemy wpisy, nie numery wersji.
_CHANGELOG_SIT = """CHANGELOG - PelicanApp
====================

0.23.0 - 17.06.2025
===================

### [Dodano]

- SAN-16560: blokowanie użytkownika

0.22.0 - 13.06.2025
===================

### [Zmieniono]

- SAN-15863: aktualizacja wersji PHP

0.21.0 - 20.05.2025
===================

### [Dodano]

- SAN-15724: zapisywanie minipolis
"""


def _static_changelog_mocks(source_text=_CHANGELOG_SIT, target_text=_CHANGELOG_TEST, version="0.23.0\n"):
    return [
        MagicMock(stdout=source_text),
        MagicMock(stdout=target_text),
        MagicMock(stdout=version),
    ]


def test_collect_static_changelog_returns_only_new_entries():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = _static_changelog_mocks()
        version, changelog = release_helper.collect_static_changelog("/fake/path", "sit", "test")

    assert version == "0.23.0"
    assert changelog == "### [Zmieniono]\n\n- SAN-15863: aktualizacja wersji PHP"


def test_collect_static_changelog_ignores_version_renumbering():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = _static_changelog_mocks()
        _, changelog = release_helper.collect_static_changelog("/fake/path", "sit", "test")

    assert "0.23.0" not in changelog
    assert "0.22.0" not in changelog
    assert "SAN-16560" not in changelog


def test_collect_static_changelog_groups_entries_by_category():
    source = _CHANGELOG_SIT.replace(
        "- SAN-15863: aktualizacja wersji PHP",
        "- SAN-15863: aktualizacja wersji PHP\n- SAN-15999: zmiana limitu",
    ).replace(
        "- SAN-16560: blokowanie użytkownika",
        "- SAN-16560: blokowanie użytkownika\n- SAN-17000: nowy raport",
    )
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = _static_changelog_mocks(source_text=source)
        _, changelog = release_helper.collect_static_changelog("/fake/path", "sit", "test")

    assert changelog == (
        "### [Dodano]\n"
        "\n"
        "- SAN-17000: nowy raport\n"
        "\n"
        "### [Zmieniono]\n"
        "\n"
        "- SAN-15863: aktualizacja wersji PHP\n"
        "- SAN-15999: zmiana limitu"
    )


def test_collect_static_changelog_reads_from_both_branches_and_version_file():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = _static_changelog_mocks()
        release_helper.collect_static_changelog("/fake/path", "sit", "test")

    commands = [call[0][0] for call in mock_run.call_args_list]
    assert commands == [
        ["git", "show", "origin/sit:CHANGELOG.md"],
        ["git", "show", "origin/test:CHANGELOG.md"],
        ["git", "show", "origin/sit:VERSION"],
    ]


def test_collect_static_changelog_raises_when_no_new_entries():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = _static_changelog_mocks(source_text=_CHANGELOG_TEST)
        with pytest.raises(ValueError, match="CHANGELOG.md"):
            release_helper.collect_static_changelog("/fake/path", "sit", "test")


def test_run_static_flow_creates_single_mr_from_source_branch():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.collect_static_changelog", return_value=("0.23.0", "- SAN-17000: nowa funkcja")), \
         patch("release_helper.subprocess.run"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/7") as mock_mr:
        url = release_helper.run_static_flow("pelicanapp", "sit-to-test")

    assert url == "https://git.sandis.io/mr/7"
    assert mock_mr.call_count == 1
    kwargs = mock_mr.call_args_list[0][1]
    assert kwargs["source_branch"] == "sit"
    assert kwargs["target_branch"] == "test"
    assert kwargs["namespace"] == "sandis/pelicanapp"
    assert "sit -> test" in kwargs["title"]
    assert "v0.23.0" in kwargs["title"]
    assert kwargs["description"] == "- SAN-17000: nowa funkcja"


def test_run_static_flow_does_not_touch_release_branch():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.collect_static_changelog", return_value=("0.23.0", "changelog")), \
         patch("release_helper.subprocess.run"), \
         patch("release_helper.create_release_branch") as mock_branch, \
         patch("release_helper.push_branch") as mock_push, \
         patch("release_helper.commit_release") as mock_commit, \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/7"):
        release_helper.run_static_flow("pelicanapp", "sit-to-test")

    mock_branch.assert_not_called()
    mock_push.assert_not_called()
    mock_commit.assert_not_called()


def test_run_static_flow_targets_master():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.collect_static_changelog", return_value=("0.23.0", "changelog")), \
         patch("release_helper.subprocess.run"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/8") as mock_mr:
        release_helper.run_static_flow("pelicanapp", "test-to-master")

    kwargs = mock_mr.call_args_list[0][1]
    assert kwargs["source_branch"] == "test"
    assert kwargs["target_branch"] == "master"


def test_run_static_flow_aborts_on_no_diff():
    with patch("release_helper.has_branch_diff", return_value=False), \
         patch("release_helper.subprocess.run"):
        with pytest.raises(SystemExit):
            release_helper.run_static_flow("pelicanapp", "sit-to-test")


def test_run_static_flow_aborts_on_no_content_diff():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=False), \
         patch("release_helper.subprocess.run"):
        with pytest.raises(SystemExit):
            release_helper.run_static_flow("pelicanapp", "sit-to-test")


def test_run_static_flow_aborts_on_untouched_changelog():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.collect_static_changelog", side_effect=ValueError("CHANGELOG.md")), \
         patch("release_helper.subprocess.run"), \
         patch("release_helper.create_gitlab_mr") as mock_mr:
        with pytest.raises(SystemExit):
            release_helper.run_static_flow("pelicanapp", "sit-to-test")
    mock_mr.assert_not_called()


def test_run_static_flow_reuses_existing_mr():
    existing = release_helper.MergeRequestExistsError("https://git.sandis.io/mr/5")
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.collect_static_changelog", return_value=("0.23.0", "changelog")), \
         patch("release_helper.subprocess.run"), \
         patch("release_helper.create_gitlab_mr", side_effect=existing):
        url = release_helper.run_static_flow("pelicanapp", "sit-to-test")

    assert url == "https://git.sandis.io/mr/5"


def test_create_gitlab_mr_raises_merge_request_exists_on_409():
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 409
    mock_response.text = (
        '{"message":["Another open merge request already exists for this source branch: !123"]}'
    )

    with patch("release_helper.requests.post", return_value=mock_response):
        with patch.dict(os.environ, {"GITLAB_TOKEN": "test-token"}):
            with pytest.raises(release_helper.MergeRequestExistsError) as exc:
                release_helper.create_gitlab_mr("sandis/pelicanapp", "sit", "test", "title", "desc")

    assert exc.value.url == "https://git.sandis.io/sandis/pelicanapp/-/merge_requests/123"


def test_create_gitlab_mr_raises_runtime_error_on_409_without_iid():
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 409
    mock_response.text = '{"message":["Conflict"]}'

    with patch("release_helper.requests.post", return_value=mock_response):
        with patch.dict(os.environ, {"GITLAB_TOKEN": "test-token"}):
            with pytest.raises(RuntimeError, match="409"):
                release_helper.create_gitlab_mr("sandis/pelicanapp", "sit", "test", "title", "desc")


def test_changelog_strategy_reads_project_config():
    assert release_helper.changelog_strategy("pelicanapp") == "static"
    assert release_helper.changelog_strategy("jaguarapp-front") is None


def test_main_dispatches_static_flow_for_pelicanapp():
    with patch("release_helper.run_static_flow") as mock_static, \
         patch("release_helper.run_sit_to_test") as mock_sit, \
         patch("release_helper.run_clg_flow") as mock_clg, \
         patch.object(sys, "argv", ["release_helper.py", "--project", "pelicanapp", "--flow", "sit-to-test"]):
        release_helper.main()

    mock_static.assert_called_once_with("pelicanapp", "sit-to-test")
    mock_sit.assert_not_called()
    mock_clg.assert_not_called()


def test_main_keeps_featurenotes_flow_for_jaguarapp_front():
    with patch("release_helper.run_static_flow") as mock_static, \
         patch("release_helper.run_sit_to_test") as mock_sit, \
         patch.object(sys, "argv", ["release_helper.py", "--project", "jaguarapp-front", "--flow", "sit-to-test"]):
        release_helper.main()

    mock_sit.assert_called_once_with("jaguarapp-front")
    mock_static.assert_not_called()
