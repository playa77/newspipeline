"""Guard tests for settings.toml/.env write atomicity (supports invariant
G3's re-read-every-tick contract: a scheduler tick must never observe a torn
settings file) and the G1 secret-refusal scan at the write path.
"""

# version: 0.2.2

import importlib
import threading
import tomllib

import pytest

from newspipeline import config


def test_newspipeline_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NEWSPIPELINE_HOME", str(tmp_path))
    cfg = importlib.reload(config)
    try:
        assert cfg.PROJECT_ROOT == tmp_path.resolve()
        assert cfg.SETTINGS_PATH == tmp_path / "config" / "settings.toml"
        assert cfg.ENV_PATH == tmp_path / ".env"
        assert cfg.DB_PATH == tmp_path / "data" / "news.db"
    finally:
        monkeypatch.delenv("NEWSPIPELINE_HOME")
        importlib.reload(config)
    assert config.PROJECT_ROOT != tmp_path.resolve()


def test_newspipeline_home_empty_string_treated_as_unset(
    monkeypatch, tmp_path, isolated_config,
):
    monkeypatch.setenv("NEWSPIPELINE_HOME", "")
    cfg = importlib.reload(config)
    try:
        # Empty override must not relocate state to the CWD.
        assert cfg.PROJECT_ROOT == config.SETTINGS_PATH.parent.parent
    finally:
        monkeypatch.delenv("NEWSPIPELINE_HOME")
        importlib.reload(config)


def _read_settings(path):
    with path.open("rb") as fh:
        return tomllib.load(fh)


def test_save_settings_round_trips(isolated_config, tmp_path):
    settings = {
        "global": {"default_model": "m", "models": {"analysis": ""}},
        "interests": [{"name": "a", "feeds": []}],
    }
    config.save_settings(settings)
    assert _read_settings(config.SETTINGS_PATH)["global"]["default_model"] == "m"


def test_save_settings_mode_is_0644_and_env_0600(isolated_config, tmp_path):
    config.save_settings({"global": {}, "interests": []})
    assert config.SETTINGS_PATH.stat().st_mode & 0o777 == 0o644
    config.set_api_key("k" * 20)
    assert config.ENV_PATH.stat().st_mode & 0o777 == 0o600


def test_save_settings_leaves_no_temp_files(isolated_config, tmp_path):
    for i in range(5):
        config.save_settings({"global": {"n": i}, "interests": []})
    leftovers = [
        p.name for p in config.SETTINGS_PATH.parent.iterdir()
        if p.name != config.SETTINGS_PATH.name
    ]
    assert leftovers == []


def test_concurrent_writers_never_expose_torn_file(isolated_config, tmp_path):
    # 8 writer threads save distinguishable payloads while reader threads
    # continuously parse the live file: EVERY observation must be complete
    # valid TOML whose payload matches exactly one writer. This fails if the
    # atomic tmp+replace in save_settings is reverted to a plain write.
    variants = [
        {
            "global": {"writer": f"w{i}", "pad": f"{i}" + "x" * 4999},
            "interests": [],
        }
        for i in range(8)
    ]
    writers = {f"w{i}" for i in range(8)}
    errors: list[BaseException] = []
    stop = threading.Event()
    reads = [0]

    def writer(settings):
        try:
            for _ in range(15):
                config.save_settings(settings)
        except BaseException as exc:  # noqa: BLE001 — collected, not raised
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                observed = _read_settings(config.SETTINGS_PATH)
                w = observed["global"]["writer"]
                assert w in writers, f"torn read: writer={w!r}"
                pad = observed["global"]["pad"]
                assert len(pad) == 5000, f"torn read: pad={len(pad)}"
                reads[0] += 1
        except BaseException as exc:  # noqa: BLE001 — collected, not raised
            errors.append(exc)

    config.save_settings(variants[0])  # seed so readers always have a file
    threads = [threading.Thread(target=writer, args=(v,)) for v in variants]
    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    assert errors == []
    # Sanity: the readers actually sampled during the writes.
    assert reads[0] > 0
    final = _read_settings(config.SETTINGS_PATH)
    assert final["global"]["writer"] in writers


def test_mutation_lock_serializes_read_modify_write(isolated_config, tmp_path):
    # Two threads append distinct interests via the same locked RMW cycle
    # used by the web layer; without serialization one append would be lost.
    def add_interest(name):
        with config.mutation_lock:
            settings = config.load_settings()
            settings["interests"].append({"name": name, "feeds": []})
            config.save_settings(settings)

    threads = [
        threading.Thread(target=add_interest, args=(f"i{n}",)) for n in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names = [i["name"] for i in _read_settings(config.SETTINGS_PATH)["interests"]]
    assert sorted(names) == ["i0", "i1", "i2", "i3", "i4", "i5"]


def test_secret_scan_catches_escaping_secrets(isolated_config, tmp_path):
    # G1 at the write path: repr()/TOML escaping must not let a secret
    # containing quotes or backslashes slip into settings.toml.
    for tricky in ('p@ss"w\\ord', 'back\\slash'):
        config.set_api_key(tricky)
        with pytest.raises(ValueError, match="Refusing to persist"):
            config.save_settings({
                "global": {}, "interests": [{"name": tricky}],
            })
        assert not config.SETTINGS_PATH.exists()


def test_env_values_with_newlines_are_rejected(isolated_config, tmp_path):
    # The .env parser is line-based; a multi-line secret would be silently
    # truncated on read and its tail parsed as junk keys. Refuse loudly.
    with pytest.raises(ValueError, match="must not contain newlines"):
        config.set_api_key("line1\nline2")
    assert not config.ENV_PATH.exists()
