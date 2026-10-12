"""Statistik je Antwort (chat/stats.py): Token/s und erstes Token im Kopf."""

from multigpt.chat import stats


def test_rounds_add_up_and_first_token_from_first_round():
    state = {}
    stats.add_round(state, 0.0, 0.5, 2.5, 200)  # 2 s Erzeugung
    stats.add_round(state, 3.0, 4.0, 5.0, 100)  # Werkzeugrunde: 1 s
    assert state["stats"] == {"ttft_ms": 500, "gen_ms": 3000, "tokens": 300}
    assert stats.summary(state)["text"] == "100 Token/s · erstes Token nach 0,5 s"


def test_no_output_no_stats_and_short_bursts_without_rate():
    state = {}
    stats.add_round(state, 0.0, None, 1.0, 0)
    assert stats.summary(state) is None
    stats.add_round(state, 0.0, 1.2, 1.25, 40)  # alles auf einmal: keine Rate
    assert stats.summary(state)["text"] == "erstes Token nach 1,2 s"
    slow = {"stats": {"ttft_ms": 100, "gen_ms": 4000, "tokens": 30}}
    assert stats.summary(slow)["text"].startswith("7,5 Token/s")
