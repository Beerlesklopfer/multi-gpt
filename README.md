# MultiGPT

Lokales Multi-KI-Chatsystem für die Familie: ein Django-Server, über den alle im
Haushalt mit verschiedenen KI-Anbietern (online und lokal über LM Studio) chatten.
Daten liegen in PostgreSQL mit pgvector.

## Entwicklung

Voraussetzungen: Debian 13 (oder ähnlich), Python 3.12+, PostgreSQL mit pgvector
lokal auf dem Unix-Socket.

```sh
make db-create install migrate user dev
```

- `db-create` legt einmalig (per `sudo -u postgres`) eine Rolle für den eigenen
  Systemnutzer, die DB `multigpt` und die Extension `vector` an.
- `install` erzeugt `.venv` und `.env` (aus `.env.example`, mit generierten Schlüsseln).
- `user` legt einen Superuser an, `dev` startet den Entwicklungsserver.

Alle Ziele zeigt `make help`, z. B. `make test`, `make lint`, `make run` (gunicorn).

## Paketbau

```sh
make deb
```

Das `.deb` landet im übergeordneten Ordner. Die vollständige Installationsanleitung
folgt mit Meilenstein 11.

## Dokumentation

- [doc/Plan.md](doc/Plan.md) – Ziele, Architektur, Meilensteine
- [doc/Implementierung.md](doc/Implementierung.md) – Arbeitspakete und Entscheidungen
