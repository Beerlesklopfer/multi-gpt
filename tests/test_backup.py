"""``mgpt-ctl backup`` (M12-02): Archiv mit Datenbank, Medien, Konfiguration; Aufräumen."""

import io
import json
import os
import shutil
import tarfile

import pytest
from django.core.management import CommandError, call_command

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump fehlt"),
]


def run(*args) -> str:
    out = io.StringIO()
    call_command("backup", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def setup(tmp_path, settings, monkeypatch):
    media = tmp_path / "media"
    (media / "attachments").mkdir(parents=True)
    (media / "attachments" / "bild.png").write_bytes(b"png")
    settings.MEDIA_ROOT = str(media)
    env = tmp_path / "env"
    env.write_text("SECRET_KEY=x\nFIELD_ENCRYPTION_KEY=y\n")
    monkeypatch.setenv("MULTI_GPT_ENV_FILE", str(env))
    target = tmp_path / "backups"
    target.mkdir()
    return target


def test_archive_contains_dump_media_env_and_manifest(setup):
    out = run("--dir", str(setup))
    archives = list(setup.glob("multigpt-*.tar"))
    assert len(archives) == 1 and "Gesichert" in out and "1 Mediendateien" in out
    archive = archives[0]
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert {"database.dump", "env", "MANIFEST.json", "media/attachments/bild.png"} <= set(names)
        manifest = json.load(tar.extractfile("MANIFEST.json"))
        dump = tar.extractfile("database.dump").read()
    assert manifest["media_files"] == 1 and manifest["env"] is True
    assert dump.startswith(b"PGDMP")  # pg_dump -Fc
    assert not list(setup.glob("*.part")) and not [p for p in setup.iterdir() if p.is_dir()]


def test_no_media_and_keep(setup):
    for _ in range(3):
        run("--dir", str(setup), "--no-media", "--keep", "2")
        # eindeutige Zeitstempel je Sekunde: älteste per mtime
        for i, path in enumerate(sorted(setup.glob("multigpt-*.tar"))):
            os.utime(path, (1_000_000 + i, 1_000_000 + i))
            path.rename(path.with_name(f"multigpt-test-{i:03d}-{path.stat().st_ino}.tar"))
    archives = list(setup.glob("multigpt-*.tar"))
    assert len(archives) == 2
    with tarfile.open(archives[0]) as tar:
        assert not [n for n in tar.getnames() if n.startswith("media/")]


def test_errors(setup, tmp_path):
    with pytest.raises(CommandError, match="fehlt"):
        run("--dir", str(tmp_path / "gibtsnicht"))
    with pytest.raises(CommandError, match="mindestens 1"):
        run("--dir", str(setup), "--keep", "0")
