import pytest
import sys
import subprocess
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


def _git_show_mocks(notes):
    """subprocess.run side_effect: kolejne `git show` zwracają treść notatek."""
    return [MagicMock(stdout=body) for body in notes]


def test_collect_feature_notes_reads_content_from_branch_not_working_tree():
    notes = {
        "SAN-100.md": "### [Poprawiono]\n\n- SAN-100: fix A\n",
        "SAN-101.md": "### [Dodano]\n\n- SAN-101: add B\n",
        "SAN-102.md": "### [Poprawiono]\n\n- SAN-102: fix C\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        # katalog celowo pusty — treść ma pochodzić z refu, nie z dysku
        with patch("release_helper.subprocess.run") as mock_run:
            mock_run.side_effect = _git_show_mocks(notes.values())
            result = release_helper.collect_feature_notes(
                tmp, "sit", only_files=list(notes)
            )

    assert "- SAN-100: fix A" in result
    assert "- SAN-101: add B" in result
    assert "- SAN-102: fix C" in result
    assert "### [Poprawiono]" in result
    assert "### [Dodano]" in result

    commands = [call[0][0] for call in mock_run.call_args_list]
    assert commands == [
        ["git", "show", "origin/sit:FeatureNotes/SAN-100.md"],
        ["git", "show", "origin/sit:FeatureNotes/SAN-101.md"],
        ["git", "show", "origin/sit:FeatureNotes/SAN-102.md"],
    ]


def test_collect_feature_notes_with_only_files():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = _git_show_mocks(["### [Poprawiono]\n\n- SAN-100: fix A\n"])
        result = release_helper.collect_feature_notes(
            "/fake/path", "sit", only_files=["SAN-100.md"]
        )

    assert "- SAN-100: fix A" in result
    assert mock_run.call_count == 1


def test_collect_feature_notes_lists_files_from_branch():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(stdout="FeatureNotes/SAN-100.md\nFeatureNotes/SAN-101.md\n"),
            MagicMock(stdout="### [Dodano]\n\n- SAN-100: add A\n"),
            MagicMock(stdout="### [Dodano]\n\n- SAN-101: add B\n"),
        ]
        result = release_helper.collect_feature_notes("/fake/path", "sit")

    assert "- SAN-100: add A" in result
    assert "- SAN-101: add B" in result
    assert mock_run.call_args_list[0][0][0] == [
        "git", "ls-tree", "--name-only", "origin/sit", "FeatureNotes/"
    ]


def test_collect_feature_notes_raises_when_branch_has_no_notes():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = [MagicMock(stdout="")]
        with pytest.raises(ValueError, match="Brak plików FeatureNotes"):
            release_helper.collect_feature_notes("/fake/path", "sit")


def test_collect_feature_notes_raises_when_file_missing_in_branch():
    with patch("release_helper.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(128, "git show")
        with pytest.raises(ValueError, match="SAN-100.md"):
            release_helper.collect_feature_notes("/fake/path", "sit", only_files=["SAN-100.md"])


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
        with patch("release_helper._get_gitlab_token", return_value="test-token"):
            url = release_helper.create_gitlab_mr(
                namespace="sandis/jaguarapp-front",
                source_branch="release/sit-test-23042026",
                target_branch="test",
                title="Release sit -> test 23.04.2026",
                description="## Changelog",
            )
    assert url == "https://git.sandis.io/sandis/jaguarapp-front/-/merge_requests/99"


def test_create_gitlab_mr_raises_on_missing_token():
    with patch("release_helper._get_gitlab_token", side_effect=ValueError("Brak tokenu GitLab")):
        with pytest.raises(ValueError, match="Brak tokenu GitLab"):
            release_helper.create_gitlab_mr("ns/proj", "src", "tgt", "title", "desc")


def test_create_gitlab_mr_raises_on_api_error():
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 422
    mock_response.text = "Branch already exists"

    with patch("release_helper.requests.post", return_value=mock_response):
        with patch("release_helper._get_gitlab_token", return_value="test-token"):
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


# ── claims-front: test-to-master ───────────────────────────────────────────────

def test_make_branch_name_test_to_master():
    with patch("release_helper.datetime") as mock_dt:
        mock_dt.date.today.return_value.strftime.return_value = "23042026"
        result = release_helper.make_branch_name("test-to-master")
    assert result == "release/test-master-23042026"


def test_allowed_flows_reads_project_config():
    assert release_helper.allowed_flows("claims-front") == ["sit-to-test", "test-to-master", "master-to-tag"]
    assert release_helper.allowed_flows("jaguarapp-front") == ["sit-to-test", "test-to-master"]


def test_allowed_flows_falls_back_to_all_flows():
    with patch.dict(release_helper.PROJECTS, {"legacy": {"path": "/x", "namespace": "n"}}):
        assert release_helper.allowed_flows("legacy") == release_helper.FLOWS


def test_parse_args_accepts_test_to_master_for_claims_front():
    args = release_helper.parse_args(["--project", "claims-front", "--flow", "test-to-master"])
    assert args.flow == "test-to-master"


def test_parse_args_rejects_removed_test_to_stage_for_claims_front():
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "claims-front", "--flow", "test-to-stage"])


def test_parse_args_rejects_test_to_stage_for_jaguarapp_front():
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "jaguarapp-front", "--flow", "test-to-stage"])


def test_run_test_to_master_claims_front_full_flow():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.branch_exists_on_remote", return_value=False), \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.run_clg", return_value=("0.2.0", "0.2.0 - 23.04.2026\n...")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/3") as mock_mr:
        url = release_helper.run_test_to_master("claims-front")

    assert url == "https://git.sandis.io/mr/3"
    targets = [c[1]["target_branch"] for c in mock_mr.call_args_list]
    assert targets == ["master", "test", "sit"]
    titles = [c[1]["title"] for c in mock_mr.call_args_list]
    assert titles[0] == "claims-front Release test -> master v0.2.0"
    assert titles[1] == "claims-front Release test -> test v0.2.0"
    assert titles[2] == "claims-front Release test -> sit v0.2.0"


def test_run_test_to_master_claims_front_branches_off_test():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=True) as mock_diff, \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.branch_exists_on_remote", return_value=False), \
         patch("release_helper.create_release_branch") as mock_create, \
         patch("release_helper.run_clg", return_value=("0.2.0", "content")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/3"):
        release_helper.run_test_to_master("claims-front")

    assert mock_diff.call_args[0][1:] == ("test", "master")
    assert mock_create.call_args[0][1] == "test"


def test_run_test_to_master_claims_front_aborts_on_no_diff():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=False):
        with pytest.raises(SystemExit):
            release_helper.run_test_to_master("claims-front")


def test_run_test_to_master_claims_front_aborts_on_no_content_diff():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=False):
        with pytest.raises(SystemExit):
            release_helper.run_test_to_master("claims-front")


def test_run_test_to_master_claims_front_uses_iterator_on_existing_branch():
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.has_content_diff", return_value=True), \
         patch("release_helper.find_available_branch_name",
               return_value="release/test-master-23042026-2") as mock_find, \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.run_clg", return_value=("0.2.0", "content")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/3"):
        release_helper.run_test_to_master("claims-front")
    mock_find.assert_called_once()


def test_print_header_derives_arrow_from_flow_config(capsys):
    release_helper._print_header("claims-front", "test-to-master")
    assert "test → master" in capsys.readouterr().out


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
        with patch("release_helper._get_gitlab_token", return_value="test-token"):
            with pytest.raises(release_helper.MergeRequestExistsError) as exc:
                release_helper.create_gitlab_mr("sandis/pelicanapp", "sit", "test", "title", "desc")

    assert exc.value.url == "https://git.sandis.io/sandis/pelicanapp/-/merge_requests/123"


def test_create_gitlab_mr_raises_runtime_error_on_409_without_iid():
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 409
    mock_response.text = '{"message":["Conflict"]}'

    with patch("release_helper.requests.post", return_value=mock_response):
        with patch("release_helper._get_gitlab_token", return_value="test-token"):
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
         patch("release_helper.run_featurenotes_flow") as mock_sit, \
         patch.object(sys, "argv", ["release_helper.py", "--project", "jaguarapp-front", "--flow", "sit-to-test"]):
        release_helper.main()

    mock_sit.assert_called_once_with("jaguarapp-front", "sit-to-test")
    mock_static.assert_not_called()


def test_get_gitlab_token_ignores_env(tmp_path):
    plik = tmp_path / "gitlab"
    plik.write_text("z-pliku\n")
    with patch("release_helper.GITLAB_TOKEN_FILE", str(plik)), \
         patch.dict(os.environ, {"GITLAB_TOKEN": "z-env"}):
        assert release_helper._get_gitlab_token() == "z-pliku"


def test_get_gitlab_token_raises_without_file(tmp_path):
    with patch("release_helper.GITLAB_TOKEN_FILE", str(tmp_path / "brak")), \
         patch.dict(os.environ, {"GITLAB_TOKEN": "z-env"}):
        with pytest.raises(ValueError, match="Brak tokenu GitLab"):
            release_helper._get_gitlab_token()


# ── claims-front: master-to-tag (tag vX.Y.Z, release:vm w CI) ─────────────────

SHA = "e1c9e188bc6990597ce592558c239d2195f6e2d6"
PLAN = {"ostatni": "v0.9.0", "ostatni_sha": "6b385c1c" + "0" * 32, "sha": SHA, "commity": 5,
        "merge": ["Merge branch 'hotfix/SAN-26383' into 'master'"], "zadania": ["SAN-26383"], "proponowany": "v0.9.1"}


@pytest.mark.parametrize("last,merges,expected", [
    ("v0.9.0", ["Merge branch 'hotfix/SAN-1' into 'master'", "Merge branch 'hotfix/SAN-2-x' into 'master'"], "v0.9.1"),
    ("v0.9.0", ["Merge branch 'hotfix/SAN-1' into 'master'", "Merge branch 'release/test-master-01112026' into 'master'"], "v0.10.0"),
    ("v1.2.3", [], "v1.3.0"),
    (None, ["Merge branch 'feature/SAN-1' into 'master'"], "v0.1.0"),
])
def test_propose_version(last, merges, expected):
    assert release_helper.propose_version(last, merges) == expected


def test_parse_args_version_and_plan_only_for_tag_flow():
    args = release_helper.parse_args(["--project", "claims-front", "--flow", "master-to-tag", "--version", "v0.9.1"])
    assert args.version == "v0.9.1"
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "claims-front", "--flow", "test-to-master", "--version", "v0.9.1"])
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "claims-front", "--flow", "test-to-master", "--plan"])
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "claims-front", "--flow", "master-to-tag", "--version", "0.9.1"])


def test_master_to_tag_only_for_claims_front():
    with pytest.raises(SystemExit):
        release_helper.parse_args(["--project", "jaguarapp-front", "--flow", "master-to-tag"])


def _tag_flow(plan=PLAN, problems=(), wait=("success", "https://git.example/rel", "https://git.example/pipe"), version=None):
    with patch("release_helper.subprocess.run"), \
         patch("release_helper.tag_plan", return_value=dict(plan)), \
         patch("release_helper.release_problems", return_value=list(problems)), \
         patch("release_helper._gitlab") as gitlab, \
         patch("release_helper.wait_for_release", return_value=wait):
        try:
            wynik = release_helper.run_tag_flow("claims-front", "master-to-tag", version)
        except SystemExit as e:
            return e.code, gitlab
        return wynik, gitlab


def test_run_tag_flow_tags_sha_not_branch_and_returns_release(capsys):
    wynik, gitlab = _tag_flow()
    assert wynik == "https://git.example/rel"
    gitlab.assert_called_once_with("POST", "projects/sandis%2Fclaims-front/repository/tags",
                                   json={"tag_name": "v0.9.1", "ref": SHA})
    out = capsys.readouterr().out
    assert "*CLAIMS-FRONT v0.9.1* wydane z e1c9e188" in out and "SAN-26383" in out


def test_run_tag_flow_uses_given_version():
    _, gitlab = _tag_flow(version="v0.10.0")
    assert gitlab.call_args[1]["json"]["tag_name"] == "v0.10.0"


def test_run_tag_flow_rejects_version_not_higher_than_last():
    kod, gitlab = _tag_flow(version="v0.9.0")
    assert kod == 2 and not gitlab.called


def test_run_tag_flow_uses_package_json_version():
    _, gitlab = _tag_flow(plan={**PLAN, "wersja_pakietu": "v0.9.1"})
    assert gitlab.call_args[1]["json"]["tag_name"] == "v0.9.1"


def test_run_tag_flow_rejects_version_other_than_package_json(capsys):
    kod, gitlab = _tag_flow(plan={**PLAN, "wersja_pakietu": "v0.9.1"}, version="v0.10.0")
    assert kod == 2 and not gitlab.called
    assert "package.json" in capsys.readouterr().err


def test_run_tag_flow_package_json_not_bumped_blocks_with_hint(capsys):
    kod, gitlab = _tag_flow(plan={**PLAN, "wersja_pakietu": "v0.9.0", "proponowany": "v0.9.0"})
    assert kod == 2 and not gitlab.called
    assert "podbij wersję" in capsys.readouterr().err


def _git_repo(tmp_path, package_json):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    if package_json is not None:
        (tmp_path / "package.json").write_text(package_json)
    (tmp_path / "plik").write_text("x")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("package_json,expected", [
    ('{"name": "frontend", "version": "1.0.1"}', "v1.0.1"),
    ('{"name": "frontend", "version": "1.0.1-rc1"}', None),
    ('{"name": "frontend"}', None),
    ("nie json", None),
    (None, None),
])
def test_package_version(tmp_path, package_json, expected):
    sha = _git_repo(tmp_path, package_json)
    assert release_helper.package_version(str(tmp_path), sha) == expected


def test_run_tag_flow_no_changes_exits_1():
    kod, gitlab = _tag_flow(plan={**PLAN, "commity": 0})
    assert kod == 1 and not gitlab.called


def test_run_tag_flow_blocked_by_problems_does_not_tag():
    kod, gitlab = _tag_flow(problems=["build:vm dla e1c9e188 jeszcze trwa"])
    assert kod == 2 and not gitlab.called


def test_run_tag_flow_failed_release_exits_2():
    kod, gitlab = _tag_flow(wait=("failed", None, "https://git.example/pipe"))
    assert kod == 2 and gitlab.called


def test_wait_for_release_polls_until_job_finishes():
    odpowiedzi = [
        [{"id": 1, "web_url": "https://git.example/pipe"}], [{"name": "release:vm", "status": "running"}],
        [{"id": 1, "web_url": "https://git.example/pipe"}], [{"name": "release:vm", "status": "success"}],
        {"_links": {"self": "https://git.example/rel"},
         "assets": {"links": [{"url": f"https://git.example/packages/claims-front/{SHA}/claims-front.tar.gz"}]}},
    ]
    sleeps = []
    with patch("release_helper._gitlab", side_effect=odpowiedzi):
        wynik = release_helper.wait_for_release("sandis/claims-front", "v0.9.1", SHA, sleep=sleeps.append)
    assert wynik == ("success", "https://git.example/rel", "https://git.example/pipe")
    assert sleeps == [release_helper.RELEASE_POLL_S]


def test_wait_for_release_times_out():
    with patch("release_helper._gitlab", return_value=[]):
        status, rel, _ = release_helper.wait_for_release("sandis/claims-front", "v0.9.1", SHA, timeout=20, interval=10,
                                                         sleep=lambda s: None)
    assert rel is None and "po 0 min" in status
