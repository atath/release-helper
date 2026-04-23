import pytest
import sys
import tempfile
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, "/home/atath/scripts")
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
    mock_mr.assert_called_once()
    call_kwargs = mock_mr.call_args[1] if mock_mr.call_args[1] else {}
    call_args = mock_mr.call_args[0]
    title = call_kwargs.get("title") or call_args[3]
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


def test_run_test_to_master_full_flow():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.branch_exists_on_remote", return_value=False), \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.run_clg", return_value=("2.9.8", "2.9.8 - 23.04.2026\n...")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/2") as mock_mr:
        url = release_helper.run_test_to_master("jaguarapp-front")

    assert url == "https://git.sandis.io/mr/2"
    call_args = mock_mr.call_args[0]
    call_kwargs = mock_mr.call_args[1] if mock_mr.call_args[1] else {}
    title = call_kwargs.get("title") or call_args[3]
    assert "test -> master" in title
    assert "v2.9.8" in title


def test_run_test_to_master_aborts_on_no_diff():
    with patch("release_helper.has_branch_diff", return_value=False):
        with pytest.raises(SystemExit):
            release_helper.run_test_to_master("jaguarapp-front")


def test_run_test_to_master_uses_iterator_on_existing_branch():
    with patch("release_helper.has_branch_diff", return_value=True), \
         patch("release_helper.find_available_branch_name", return_value="release/test-master-23042026-2") as mock_find, \
         patch("release_helper.create_release_branch"), \
         patch("release_helper.run_clg", return_value=("2.9.8", "content")), \
         patch("release_helper.commit_release"), \
         patch("release_helper.push_branch"), \
         patch("release_helper.create_gitlab_mr", return_value="https://git.sandis.io/mr/2"):
        release_helper.run_test_to_master("jaguarapp-front")
    mock_find.assert_called_once()
