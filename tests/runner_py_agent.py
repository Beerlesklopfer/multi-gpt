"""Python-Agent als eigener Prozess – Gegenstück zum Rust-Binary in den Gleichwertigkeitstests.

Wie ``python -P -m runner_agent`` (gleiche Umgebungsvariablen, gleiche Exit-Codes),
zusätzlich nur für Tests:

- ``MGPT_ENGINE=fake`` (nur mit ``MGPT_INSECURE_DEV=1``): ``FakeEngine`` statt Podman,
  Wurzel ``MGPT_FAKE_ROOT``. Entspricht dem Cargo-Feature ``fake-engine`` des Rust-Agenten.
- ``MGPT_LABELS="k=v k2=v2"``: zusätzliche Labels an allen Podman-Objekten (Aufräumen je Lauf).

Start: ``python -m tests.runner_py_agent`` mit der Projektwurzel als Arbeitsverzeichnis.
"""

from __future__ import annotations

import os
import sys

from runner_agent import agent as agent_module
from runner_agent import config as config_module
from runner_agent import podman as podman_module
from runner_agent.config import ConfigError

_from_env = config_module.from_env
_Podman = podman_module.PodmanEngine


def _labels(raw: str) -> dict[str, str]:
    labels = {}
    for item in raw.split():
        key, sep, value = item.partition("=")
        if not sep or not key.startswith("multigpt.test"):
            raise ConfigError(f"MGPT_LABELS: nur multigpt.test*-Labels erlaubt: {item}")
        labels[key] = value
    return labels


def from_env(env=None):
    env = os.environ if env is None else env
    config = _from_env(env)
    config.labels = _labels(env.get("MGPT_LABELS", ""))
    return config


def engine(config, socket_path=None):
    """Engine nach MGPT_ENGINE: ``fake`` nur im Entwicklungsmodus (wie das Cargo-Feature)."""
    if os.environ.get("MGPT_ENGINE", "podman") == "fake":
        if not config.insecure_dev:
            raise SystemExit(2)
        from runner_agent.engine import FakeEngine

        return FakeEngine(config, root=os.environ.get("MGPT_FAKE_ROOT") or None)
    return _Podman(config, socket_path)


def main() -> int:
    # Unverändertes runner_agent.agent.main, nur Einstellungen und Engine ersetzt.
    agent_module.from_env = from_env
    podman_module.PodmanEngine = engine
    return agent_module.main()


if __name__ == "__main__":
    sys.exit(main())
