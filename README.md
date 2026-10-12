# MultiGPT

Lokales Multi-KI-Chatsystem für die Familie: ein Django-Server, über den alle im
Haushalt mit verschiedenen KI-Anbietern (online und lokal über LM Studio) chatten.
Daten liegen in PostgreSQL mit pgvector.

## Installation per apt

Fertige Pakete für Debian 13 (amd64) kommen aus einer eigenen, signierten Paketquelle:

```sh
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://beerlesklopfer.github.io/multi-gpt/apt/multi-gpt.gpg \
  | sudo tee /etc/apt/keyrings/multi-gpt.gpg >/dev/null
sudo tee /etc/apt/sources.list.d/multi-gpt.sources >/dev/null <<'EOF'
Types: deb
URIs: https://beerlesklopfer.github.io/multi-gpt/apt
Suites: stable
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/multi-gpt.gpg
EOF
sudo apt update
sudo apt install multi-gpt
```

Fingerabdruck des Signierschlüssels: `8822 DF96 2A10 31EC AD6A  6F40 3DE7 6675 16FC 28FF`
(prüfen mit `gpg --show-keys /etc/apt/keyrings/multi-gpt.gpg`). Updates kommen mit
`sudo apt update && sudo apt upgrade`. PostgreSQL mit pgvector, die Fragen bei der
Installation und alles Weitere beschreibt die
[Installationsanleitung](https://beerlesklopfer.github.io/multi-gpt/installation/).
Jedes Paket liegt außerdem am [GitHub-Release](https://github.com/Beerlesklopfer/multi-gpt/releases).

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

Das `.deb` landet im übergeordneten Ordner. Für Veröffentlichungen genügt
`make release VERSION=x.y.z` und der Push des Tags: GitHub Actions (`package.yml`) baut
das Paket auf Debian 13, legt das GitHub-Release an und aktualisiert die apt-Quelle
(`website.yml`, `scripts/apt-repo.sh`, die letzten drei Versionen). Signiert wird mit dem
Repository-Secret `APT_SIGNING_KEY`.

## Dokumentation

- [docs/Plan.md](docs/Plan.md) – Ziele, Architektur, Meilensteine
- [docs/Implementierung.md](docs/Implementierung.md) – Arbeitspakete und Entscheidungen
