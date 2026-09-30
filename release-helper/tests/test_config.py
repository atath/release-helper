import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import release_helper

POPRAWNY = {
    "gitlab_url": "https://git.example",
    "flows": {
        "sit-to-test": {"source": "sit", "target": "test", "back_merge": [], "typ": "featurenotes"},
        "test-to-master": {"source": "test", "target": "master", "back_merge": ["test", "sit"], "typ": "clg"},
    },
    "projects": {
        "a-front": {"path": "~/projekty/a-front", "namespace": "g/a-front", "flows": ["sit-to-test"]},
    },
}


def zapisz(tmp_path, dane):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(dane))
    return str(p)


def test_wczytanie_rozwija_tylde(tmp_path):
    cfg = release_helper.load_config(zapisz(tmp_path, POPRAWNY))
    assert cfg["projects"]["a-front"]["path"] == os.path.expanduser("~/projekty/a-front")
    assert list(cfg["flows"]) == ["sit-to-test", "test-to-master"]


@pytest.mark.parametrize("zmiana,fraza", [
    (lambda d: d["flows"]["sit-to-test"].update(typ="inny"), "typ"),
    (lambda d: d["projects"]["a-front"].update(flows=["nieznany"]), "nieznany"),
    (lambda d: d["projects"]["a-front"].pop("namespace"), "namespace"),
    (lambda d: d["flows"]["test-to-master"].pop("target"), "target"),
])
def test_bledna_konfiguracja_jest_odrzucana(tmp_path, zmiana, fraza):
    dane = json.loads(json.dumps(POPRAWNY))
    zmiana(dane)
    with pytest.raises(ValueError, match=fraza):
        release_helper.load_config(zapisz(tmp_path, dane))


def test_konfiguracja_z_repo_jest_poprawna():
    cfg = release_helper.load_config()
    assert {"jaguarapp-front", "claims-front", "d2c-front", "pelicanapp"} <= set(cfg["projects"])
    assert cfg["flows"]["sit-to-test"]["typ"] == "featurenotes"


def test_przeplyw_featurenotes_z_innych_galezi_czyta_te_galezie():
    from unittest.mock import patch, MagicMock
    with patch("release_helper.subprocess.run") as run:
        run.return_value = MagicMock(stdout="FeatureNotes/SAN-1.md\n")
        release_helper.get_new_feature_note_files("/x", source="dev", target="sit")
    assert run.call_args[0][0][:4] == ["git", "diff", "origin/sit", "origin/dev"]
