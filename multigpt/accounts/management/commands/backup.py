"""Sicherung (``mgpt-ctl backup``, M12-02): Datenbank, Medien und Konfiguration in einem Archiv.

    sudo mgpt-ctl backup [--dir /var/backups/multi-gpt] [--keep 7] [--no-media]

Das Archiv ``multigpt-<Rechner>-<JJJJMMTT-hhmmss>.tar`` (Rechte 0600) enthält:

- ``database.dump`` – ``pg_dump -Fc`` der Datenbank (läuft als Besitzer der Datenbank),
- ``media/`` – ``MEDIA_ROOT`` (Anhänge, Dokumente, Profilbilder),
- ``env`` – die Konfiguration (``/etc/multi-gpt/.env``) mit ``SECRET_KEY`` und
  ``FIELD_ENCRYPTION_KEY``: ohne sie sind gespeicherte API-Keys nach dem Zurückspielen
  unlesbar. Das Archiv deshalb wie ein Passwort behandeln.
- ``MANIFEST.json`` – Version, Zeitpunkt, Datenbank, Zahl der Mediendateien.

``--keep N`` löscht danach die ältesten eigenen Archive im Zielordner, bis N übrig sind.
Zurückspielen: Wiki „Backup“.
"""

import io
import json
import os
import socket
import subprocess
import tarfile
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from multigpt.version import version_label

DEFAULT_DIR = "/var/backups/multi-gpt"
PREFIX = "multigpt-"


def _pg_env(db: dict) -> tuple[list[str], dict]:
    args = []
    if db.get("HOST"):
        args += ["--host", str(db["HOST"])]
    if db.get("PORT"):
        args += ["--port", str(db["PORT"])]
    if db.get("USER"):
        args += ["--username", str(db["USER"])]
    env = dict(os.environ)
    if db.get("PASSWORD"):
        env["PGPASSWORD"] = str(db["PASSWORD"])
    return args, env


def dump_database(target: Path) -> None:
    db = settings.DATABASES["default"]
    args, env = _pg_env(db)
    command = ["pg_dump", "--format=custom", "--no-password", *args, "--file", str(target)]
    command.append(str(db["NAME"]))
    try:
        subprocess.run(command, env=env, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CommandError("pg_dump fehlt (Paket postgresql-client).") from exc
    except subprocess.CalledProcessError as exc:
        raise CommandError(f"pg_dump fehlgeschlagen: {exc.stderr.strip()[:500]}") from exc


def env_file() -> Path | None:
    path = os.environ.get("MULTI_GPT_ENV_FILE")
    return Path(path) if path and Path(path).is_file() else None


def prune(directory: Path, keep: int) -> list[Path]:
    archives = sorted(directory.glob(f"{PREFIX}*.tar"), key=lambda p: p.stat().st_mtime)
    removed = archives[: max(0, len(archives) - keep)]
    for path in removed:
        path.unlink()
    return removed


class Command(BaseCommand):
    help = "Sichert Datenbank, Medien und Konfiguration in ein datiertes Archiv."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir", default=DEFAULT_DIR, help=f"Zielordner (Standard {DEFAULT_DIR})"
        )
        parser.add_argument("--keep", type=int, help="nur die neuesten N Archive behalten")
        parser.add_argument("--no-media", action="store_true", help="ohne Medienordner")

    def handle(self, *args, **options):
        directory = Path(options["dir"])
        if not directory.is_dir():
            raise CommandError(f"Zielordner {directory} fehlt.")
        if options["keep"] is not None and options["keep"] < 1:
            raise CommandError("--keep: mindestens 1.")
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        host = socket.gethostname().split(".")[0] or "server"
        archive = directory / f"{PREFIX}{host}-{stamp}.tar"
        partial = archive.with_suffix(".tar.part")
        media = Path(settings.MEDIA_ROOT)
        media_files = 0
        old_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(dir=directory) as tmp:
                dump = Path(tmp) / "database.dump"
                dump_database(dump)
                with tarfile.open(partial, "w") as tar:
                    tar.add(dump, arcname="database.dump")
                    config = env_file()
                    if config is not None:
                        tar.add(config, arcname="env")
                    if not options["no_media"] and media.is_dir():
                        for path in sorted(media.rglob("*")):
                            if path.is_file() and not path.is_symlink():
                                tar.add(path, arcname=f"media/{path.relative_to(media)}")
                                media_files += 1
                    manifest = {
                        "format": 1,
                        "version": version_label(),
                        "created": timezone.now().isoformat(),
                        "database": str(settings.DATABASES["default"]["NAME"]),
                        "media_root": str(media),
                        "media_files": media_files,
                        "env": config is not None,
                    }
                    data = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
                    info = tarfile.TarInfo("MANIFEST.json")
                    info.size = len(data)
                    info.mtime = int(timezone.now().timestamp())
                    info.mode = 0o600
                    tar.addfile(info, io.BytesIO(data))
            partial.chmod(0o600)
            partial.rename(archive)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            os.umask(old_umask)
        size_mb = archive.stat().st_size / 1e6
        self.stdout.write(
            self.style.SUCCESS(
                f"Gesichert: {archive} ({size_mb:.1f} MB, {media_files} Mediendateien"
                f"{', mit Konfiguration' if env_file() else ', ohne Konfiguration'})."
            )
        )
        if env_file() is None:
            self.stdout.write(
                "Hinweis: Die .env fehlt im Archiv – FIELD_ENCRYPTION_KEY gesondert sichern."
            )
        if options["keep"]:
            for path in prune(directory, options["keep"]):
                self.stdout.write(f"Gelöscht (älter): {path.name}")
