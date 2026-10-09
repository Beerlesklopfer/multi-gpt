"""Anthropic-Adapter mit gemocktem HTTP (respx, M4-01)."""

import json
import logging

import httpx
import pytest
import respx

from multigpt.chat.models import Provider
from multigpt.chat.providers import (
    ChatMessage,
    Delta,
    Done,
    Error,
    ProviderError,
    ToolCallEvent,
    ToolSpec,
    Usage,
)
from multigpt.chat.providers.anthropic import DEFAULT_MAX_TOKENS, AnthropicAdapter, _EventParser
from tests.test_provider_openai_compat import TrackingStream, sse_response

BASE = "http://anthropic.test/v1"
KEY = "sk-ant-test-GEHEIM-1234567890"
MESSAGES = [ChatMessage("user", "Hallo")]


def make_adapter(api_key=KEY, base_url=BASE):
    return AnthropicAdapter(
        Provider(name="Claude", kind="anthropic", base_url=base_url, api_key=api_key)
    )


def ev(data):
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"


def start(input_tokens=25, **usage):
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1, **usage},
        },
    }


def block_start(index=0, kind="text", **extra):
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": kind, **({"text": ""} if kind == "text" else {}), **extra},
    }


def text(t, index=0):
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": t},
    }


def block_stop(index=0):
    return {"type": "content_block_stop", "index": index}


def message_delta(stop="end_turn", output_tokens=15):
    return {
        "type": "message_delta",
        "delta": {"stop_reason": stop, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }


STOP = {"type": "message_stop"}
PING = {"type": "ping"}


def sse(*events):
    return "".join(ev(e) for e in events).encode()


def normal_body(*texts):
    return sse(
        start(),
        block_start(),
        PING,
        *[text(t) for t in texts],
        block_stop(),
        message_delta(),
        STOP,
    )


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


def run(adapter=None, messages=MESSAGES, **kwargs):
    return list((adapter or make_adapter()).stream("claude-test", messages, **kwargs))


# --- normaler Ablauf ---------------------------------------------------------


def test_normal_stream(mock):
    route = mock.post(f"{BASE}/messages").mock(
        return_value=sse_response(normal_body("Hal", "lo", " Welt"))
    )
    events = run(system="Sei nett.", temperature=0.5)
    assert events == [Delta("Hal"), Delta("lo"), Delta(" Welt"), Usage(25, 15), Done("end_turn")]

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["model"] == "claude-test"
    assert body["stream"] is True
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS
    assert body["temperature"] == 0.5
    assert body["system"] == "Sei nett."
    assert body["messages"] == [{"role": "user", "content": "Hallo"}]
    assert "tools" not in body
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in request.headers


def test_usage_with_cache_and_cumulative_delta(mock):
    body = sse(
        start(input_tokens=10, cache_creation_input_tokens=3, cache_read_input_tokens=5),
        block_start(),
        text("x"),
        block_stop(),
        message_delta(output_tokens=4),
        # message_delta ist kumulativ: der letzte Wert zählt.
        message_delta(output_tokens=9),
        STOP,
    )
    mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    assert run()[-2:] == [Usage(18, 9), Done("end_turn")]


def test_thinking_blocks_not_shown_but_kept(mock):
    body = sse(
        start(),
        block_start(0, "thinking", thinking="", signature=""),
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "geheime Überlegung"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "abc"},
        },
        block_stop(0),
        block_start(1),
        text("Antwort", index=1),
        block_stop(1),
        message_delta(),
        STOP,
    )
    mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    state = {
        "content": [
            {"type": "thinking", "thinking": "geheime Überlegung", "signature": "abc"},
            {"type": "text", "text": "Antwort"},
        ]
    }
    assert run() == [Delta("Antwort"), Usage(25, 15), Done("end_turn", provider_state=state)]


def test_roles_merged_and_system_param(mock):
    route = mock.post(f"{BASE}/messages").mock(return_value=sse_response(normal_body("x")))
    history = [
        ChatMessage("assistant", "Begrüßung ohne Frage"),
        ChatMessage("user", "Erste Frage"),
        ChatMessage("assistant", "Antwort"),
        ChatMessage("user", "Zweite Frage"),
        ChatMessage("user", "Nachtrag"),
    ]
    run(messages=history, system="Rolle")
    body = json.loads(route.calls.last.request.content)
    assert body["system"] == "Rolle"
    assert body["messages"] == [
        {"role": "user", "content": "Erste Frage"},
        {"role": "assistant", "content": "Antwort"},
        {"role": "user", "content": "Zweite Frage\n\nNachtrag"},
    ]


def test_params_override_max_tokens_but_not_reserved(mock):
    route = mock.post(f"{BASE}/messages").mock(return_value=sse_response(normal_body("x")))
    tools = [{"name": "t", "description": "d", "parameters": {}}]
    run(max_tokens=100, stream=False, model="anders", tools=tools, top_k=None)
    body = json.loads(route.calls.last.request.content)
    assert body["max_tokens"] == 100
    assert body["stream"] is True and body["model"] == "claude-test"
    assert body["tools"] == [
        {"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}
    ]
    assert "top_k" not in body
    assert "system" not in body


def test_default_base_url(mock):
    route = mock.post("https://api.anthropic.com/v1/messages").mock(
        return_value=sse_response(normal_body("x"))
    )
    assert run(make_adapter(base_url=""))[-1] == Done("end_turn")
    assert route.called


def test_missing_message_stop_with_stop_reason_is_complete(mock):
    body = sse(start(), block_start(), text("x"), block_stop(), message_delta())
    mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    assert run() == [Delta("x"), Usage(25, 15), Done("end_turn")]


def test_unknown_events_and_bad_lines_ignored(mock):
    body = (
        b": Kommentar\n\n"
        + sse(start(), {"type": "neues_event", "x": 1}, block_start())
        + b"data: kein json\n\n"
        + sse(text("ok"), block_stop(), message_delta(), STOP)
    )
    mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    assert run() == [Delta("ok"), Usage(25, 15), Done("end_turn")]


# --- Fehler ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "retryable", "fragment"),
    [
        (401, False, "API-Key"),
        (403, False, "API-Key"),
        (402, False, "Guthaben"),
        (404, False, "nicht gefunden"),
        (429, True, "zu viele Anfragen"),
        (500, True, "Serverfehler"),
        (529, True, "überlastet"),
    ],
)
def test_http_errors(mock, caplog, status, retryable, fragment):
    error_body = {
        "type": "error",
        "error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"},
    }
    mock.post(f"{BASE}/messages").mock(return_value=httpx.Response(status, json=error_body))
    with caplog.at_level(logging.DEBUG):
        events = run()
    assert len(events) == 1
    (error,) = events
    assert isinstance(error, Error)
    assert error.retryable is retryable
    assert fragment in error.message
    assert KEY not in error.message
    assert KEY not in caplog.text
    assert "authentication_error" in caplog.text


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (httpx.ConnectError("refused"), "nicht erreichbar"),
        (httpx.ConnectTimeout("timeout"), "nicht erreichbar"),
        (httpx.ReadTimeout("timeout"), "nicht rechtzeitig"),
    ],
)
def test_network_errors(mock, exc, fragment):
    mock.post(f"{BASE}/messages").mock(side_effect=exc)
    (error,) = run()
    assert isinstance(error, Error)
    assert error.retryable is True
    assert fragment in error.message


@pytest.mark.parametrize(
    ("error_type", "retryable", "fragment"),
    [
        ("overloaded_error", True, "überlastet"),
        ("api_error", True, "Fehler abgebrochen"),
        ("invalid_request_error", False, "Fehler abgebrochen"),
    ],
)
def test_error_event_mid_stream(mock, caplog, error_type, retryable, fragment):
    body = sse(
        start(),
        block_start(),
        text("Teil"),
        {"type": "error", "error": {"type": error_type, "message": f"Detail {KEY}"}},
        text("nie"),
    )
    mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    with caplog.at_level(logging.DEBUG):
        events = run()
    assert events[0] == Delta("Teil")
    assert len(events) == 2
    assert isinstance(events[-1], Error)
    assert events[-1].retryable is retryable
    assert fragment in events[-1].message
    assert KEY not in events[-1].message and KEY not in caplog.text
    assert error_type in caplog.text


def test_connection_lost_mid_stream(mock):
    stream = TrackingStream(
        [ev(start()).encode(), ev(block_start()).encode(), ev(text("Teil")).encode(), b"x"],
        fail_after=3,
    )
    mock.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, stream=stream))
    events = run()
    assert events[0] == Delta("Teil")
    assert len(events) == 2
    assert isinstance(events[-1], Error)
    assert events[-1].retryable is True
    assert "unterbrochen" in events[-1].message
    assert stream.closed


def test_stream_ends_without_stop(mock):
    body = sse(start(), block_start(), text("halb"))
    mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    events = run()
    assert events[0] == Delta("halb")
    assert isinstance(events[-1], Error) and events[-1].retryable


def test_unexpected_exception_becomes_error(mock):
    mock.post(f"{BASE}/messages").mock(side_effect=RuntimeError(KEY))
    (error,) = run()
    assert isinstance(error, Error)
    assert error.retryable is False
    assert KEY not in error.message


def test_close_closes_connection(mock):
    parts = [ev(start()).encode(), ev(block_start()).encode()]
    parts += [ev(text(str(i))).encode() for i in range(100)]
    stream = TrackingStream(parts)
    mock.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, stream=stream))
    gen = make_adapter().stream("claude-test", MESSAGES)
    assert next(gen) == Delta("0")
    assert next(gen) == Delta("1")
    assert not stream.closed
    gen.close()
    assert stream.closed


# --- Werkzeuge (M4a) ---------------------------------------------------------


def _tool_use_lines():
    events = [
        start(),
        block_start(0),
        text("Moment"),
        block_stop(0),
        block_start(1, "tool_use", id="toolu_1", name="wetter", input={}),
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"ort": '},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '"Köln"}'},
        },
        block_stop(1),
        message_delta(stop="tool_use"),
        STOP,
    ]
    return [line for e in events for line in ev(e).splitlines()]


def test_parser_collects_tool_use():
    parser = _EventParser(emit_tool_calls=True)
    events = [e for line in _tool_use_lines() for e in parser.feed(line)]
    assert events == [
        Delta("Moment"),
        ToolCallEvent(id="toolu_1", name="wetter", arguments={"ort": "Köln"}),
        Usage(25, 15),
        Done("tool_calls"),
    ]


def test_parser_without_tools_emits_no_tool_calls():
    parser = _EventParser()
    events = [e for line in _tool_use_lines() for e in parser.feed(line)]
    assert not any(isinstance(e, ToolCallEvent) for e in events)
    assert events[-1] == Done("tool_use")


# --- Modellliste und Online-Status ------------------------------------------


def models_page(ids, has_more=False):
    return {
        "data": [{"id": i, "type": "model", "display_name": i} for i in ids],
        "has_more": has_more,
        "first_id": ids[0] if ids else None,
        "last_id": ids[-1] if ids else None,
    }


def test_list_models_paginates(mock):
    route = mock.get(f"{BASE}/models").mock(
        side_effect=[
            httpx.Response(200, json=models_page(["claude-a", "claude-b"], has_more=True)),
            httpx.Response(200, json=models_page(["claude-c"])),
        ]
    )
    assert make_adapter().list_models() == ["claude-a", "claude-b", "claude-c"]
    first, second = route.calls
    assert first.request.headers["x-api-key"] == KEY
    assert first.request.headers["anthropic-version"] == "2023-06-01"
    assert first.request.url.params["limit"] == "1000"
    assert second.request.url.params["after_id"] == "claude-b"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"type": "authentication_error", "message": KEY}}),
        httpx.Response(200, text="kein json"),
        httpx.ConnectError("refused"),
    ],
)
def test_list_models_errors(mock, caplog, response):
    route = mock.get(f"{BASE}/models")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as info:
        make_adapter().list_models()
    assert KEY not in str(info.value)
    assert KEY not in caplog.text


def test_is_online_true(mock):
    mock.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json=models_page(["a"])))
    assert make_adapter().is_online() is True


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(500),
        httpx.Response(401),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("timeout"),
        RuntimeError("kaputt"),
    ],
)
def test_is_online_false(mock, outcome):
    route = mock.get(f"{BASE}/models")
    if isinstance(outcome, BaseException):
        route.mock(side_effect=outcome)
    else:
        route.mock(return_value=outcome)
    assert make_adapter().is_online(timeout=0.1) is False


WEATHER = ToolSpec(
    name="wetter",
    description="Wetter für einen Ort",
    parameters={"type": "object", "properties": {"ort": {"type": "string"}}, "required": ["ort"]},
)


def json_delta(index, part):
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": part},
    }


def test_tool_definition_and_choice_in_body(mock):
    route = mock.post(f"{BASE}/messages").mock(return_value=sse_response(normal_body("x")))
    run(tools=[WEATHER], tool_choice="required")
    body = json.loads(route.calls.last.request.content)
    assert body["tools"] == [
        {
            "name": "wetter",
            "description": "Wetter für einen Ort",
            "input_schema": WEATHER.parameters,
        }
    ]
    assert body["tool_choice"] == {"type": "any"}


@pytest.mark.parametrize(
    "choice, expected",
    [
        ("auto", {"type": "auto"}),
        ("none", {"type": "none"}),
        ("wetter", {"type": "tool", "name": "wetter"}),
        (None, None),
    ],
)
def test_tool_choice_mapping(mock, choice, expected):
    route = mock.post(f"{BASE}/messages").mock(return_value=sse_response(normal_body("x")))
    run(tools=[WEATHER], tool_choice=choice)
    assert json.loads(route.calls.last.request.content).get("tool_choice") == expected


def test_streamed_parallel_tool_calls(mock):
    body = sse(
        start(),
        block_start(0, "tool_use", id="toolu_a", name="wetter", input={}),
        json_delta(0, ""),
        json_delta(0, '{"o'),
        json_delta(0, 'rt": "Kö'),
        json_delta(0, 'ln"}'),
        block_stop(0),
        block_start(1, "tool_use", id="toolu_b", name="wetter", input={}),
        json_delta(1, '{"ort": "Bonn"}'),
        block_stop(1),
        block_start(2, "tool_use", id="toolu_c", name="uhrzeit", input={}),
        block_stop(2),
        message_delta(stop="tool_use"),
        STOP,
    )
    mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    assert run(tools=[WEATHER]) == [
        ToolCallEvent(id="toolu_a", name="wetter", arguments={"ort": "Köln"}),
        ToolCallEvent(id="toolu_b", name="wetter", arguments={"ort": "Bonn"}),
        ToolCallEvent(id="toolu_c", name="uhrzeit", arguments={}),
        Usage(25, 15),
        Done("tool_calls"),
    ]


@pytest.mark.parametrize("raw", ['{"ort": "Kö', "[1, 2]"])
def test_invalid_tool_arguments_become_error(mock, caplog, raw):
    body = sse(
        start(),
        block_start(0, "tool_use", id="toolu_a", name="wetter", input={}),
        json_delta(0, raw),
        block_stop(0),
        message_delta(stop="tool_use"),
        STOP,
    )
    mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    events = run(tools=[WEATHER])
    assert len(events) == 1
    assert isinstance(events[0], Error) and events[0].retryable
    assert "ungültige Argumente" in events[0].message and "wetter" in events[0].message


def test_history_with_tool_calls_and_results(mock):
    route = mock.post(f"{BASE}/messages").mock(return_value=sse_response(normal_body("x")))
    history = [
        ChatMessage("user", "Wetter in Köln und Bonn?"),
        ChatMessage(
            "assistant",
            "Ich schaue nach.",
            tool_calls=[
                ToolCallEvent("toolu_a", "wetter", {"ort": "Köln"}),
                ToolCallEvent("toolu_b", "wetter", {"ort": "Bonn"}),
            ],
        ),
        # Ergebnisse in anderer Reihenfolge; Reihenfolge der Aufrufe zählt.
        ChatMessage("tool", "Regen", tool_call_id="toolu_b", name="wetter", is_error=True),
        ChatMessage("tool", "Sonne", tool_call_id="toolu_a", name="wetter"),
        ChatMessage("user", "Und morgen?"),
    ]
    run(messages=history, tools=[WEATHER])
    body = json.loads(route.calls.last.request.content)
    assert body["messages"] == [
        {"role": "user", "content": "Wetter in Köln und Bonn?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Ich schaue nach."},
                {"type": "tool_use", "id": "toolu_a", "name": "wetter", "input": {"ort": "Köln"}},
                {"type": "tool_use", "id": "toolu_b", "name": "wetter", "input": {"ort": "Bonn"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": "Sonne"},
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_b",
                    "content": "Regen",
                    "is_error": True,
                },
                {"type": "text", "text": "Und morgen?"},
            ],
        },
    ]


def test_history_repairs_missing_and_orphan_results(mock):
    route = mock.post(f"{BASE}/messages").mock(return_value=sse_response(normal_body("x")))
    history = [
        ChatMessage("tool", "verwaist", tool_call_id="toolu_x", name="wetter"),
        ChatMessage("user", "Frage"),
        ChatMessage("assistant", "", tool_calls=[ToolCallEvent("toolu_a", "wetter", {})]),
        ChatMessage("user", "Abgebrochen, neue Frage"),
    ]
    run(messages=history, tools=[WEATHER])
    messages = json.loads(route.calls.last.request.content)["messages"]
    assert messages[0] == {"role": "user", "content": "Frage"}
    assert messages[1]["content"] == [
        {"type": "tool_use", "id": "toolu_a", "name": "wetter", "input": {}}
    ]
    result, text_block = messages[2]["content"]
    assert result["tool_use_id"] == "toolu_a" and result["is_error"] is True
    assert text_block == {"type": "text", "text": "Abgebrochen, neue Frage"}


def test_thinking_signature_round_trip(mock):
    body = sse(
        start(),
        block_start(0, "thinking", thinking="", signature=""),
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Ich brauche das Wetter."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "SIG-1"},
        },
        block_stop(0),
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": "VERSCHLUESSELT"},
        },
        block_stop(1),
        block_start(2, "tool_use", id="toolu_a", name="wetter", input={}),
        json_delta(2, '{"ort": "Köln"}'),
        block_stop(2),
        message_delta(stop="tool_use"),
        STOP,
    )
    route = mock.post(f"{BASE}/messages").mock(return_value=sse_response(body))
    events = run(tools=[WEATHER])
    call, done = events[0], events[-1]
    assert call == ToolCallEvent("toolu_a", "wetter", {"ort": "Köln"})
    expected_content = [
        {"type": "thinking", "thinking": "Ich brauche das Wetter.", "signature": "SIG-1"},
        {"type": "redacted_thinking", "data": "VERSCHLUESSELT"},
        {"type": "tool_use", "id": "toolu_a", "name": "wetter", "input": {"ort": "Köln"}},
    ]
    assert done == Done("tool_calls", provider_state={"content": expected_content})

    # Zweite Runde: Assistant-Antwort wörtlich, danach das Ergebnis.
    route.mock(return_value=sse_response(normal_body("Sonnig")))
    history = [
        *MESSAGES,
        ChatMessage("assistant", "", tool_calls=[call], provider_state=done.provider_state),
        ChatMessage("tool", "Sonne", tool_call_id="toolu_a", name="wetter"),
    ]
    run(messages=history, tools=[WEATHER])
    sent = json.loads(route.calls.last.request.content)["messages"]
    assert sent[1] == {"role": "assistant", "content": expected_content}
    assert sent[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "Sonne"}],
    }
