"""Every setting the loader understands must survive Settings > Save.

openalex_api_key was loaded but never written back, so saving Settings wiped it.
"""
from mouseion import config as C


def test_every_toml_key_survives_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_CONFIG_PATH", tmp_path / "config.toml")
    cfg = C.Config()
    expected = {}
    for section, mapping in C._TOML_MAP.items():
        for key, attr in mapping.items():
            cur = getattr(cfg, attr)
            if isinstance(cur, bool):
                val = not cur
            elif isinstance(cur, int):
                val = cur + 7
            elif attr.endswith("_path") or attr == "db_path":
                val = str(tmp_path / f"{attr}_x")
            else:
                val = f'{attr} "quoted" \\back'        # hostile characters must round-trip too
            setattr(cfg, attr, val)
            expected[attr] = val
    C._save(cfg)
    loaded = C.Config()
    C._apply_toml(loaded, C._read_toml(C._CONFIG_PATH))
    lost = {a: (getattr(loaded, a), v) for a, v in expected.items() if str(getattr(loaded, a)) != str(v)}
    assert not lost, f"settings dropped or mangled by save: {sorted(lost)}"


def test_save_keeps_a_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_CONFIG_PATH", tmp_path / "config.toml")
    (tmp_path / "config.toml").write_text('[providers]\nopenalex_email = "old"\n', encoding="utf-8")
    C._save(C.Config())
    assert "old" in (tmp_path / "config.toml.bak").read_text(encoding="utf-8")
