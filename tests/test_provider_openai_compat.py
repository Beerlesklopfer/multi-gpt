"""OpenAI-kompatibler Adapter mit gemocktem HTTP (respx, M3-02/M3-06)."""

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
from multigpt.chat.providers.openai_compat import OpenAICompatAdapter, _ChunkParser

BASE = "http://lmstudio.test:1234/v1"
KEY = "sk-test-GEHEIM-1234567890"
MESSAGES = [ChatMessage("user", "Hallo")]


def make_adapter(api_key=KEY, base_url=BASE):
    return OpenAICompatAdapter(
        Provider(name="Test", kind="openai_compat", base_url=base_url, api_key=api_key)
    )


def sse(*chunks, done=True, comments=()):
    lines = [f"{c}\n\n" for c in comments]
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def content(text, finish=None):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}


USAGE_CHUNK = {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7}}


def sse_response(body, status=200):
    return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})


class TrackingStream(httpx.SyncByteStream):
    """Liefert Bytes stückweise; merkt sich close() und kann mitten drin abreißen."""

    def __init__(self, parts, fail_after=None):
        self.parts = parts
        self.fail_after = fail_after
        self.closed = False

    def __iter__(self):
        for i, part in enumerate(self.parts):
            if self.fail_after is not None and i >= self.fail_after:
                raise httpx.RemoteProtocolError("peer closed connection")
            yield part

    def close(self):
        self.closed = True


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


def run(adapter=None, **kwargs):
    return list((adapter or make_adapter()).stream("test-model", MESSAGES, **kwargs))


# --- normaler Ablauf ---------------------------------------------------------


def test_normal_stream(mock):
    route = mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(
            sse(
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                content("Hal"),
                content("lo"),
                content(" Welt", finish="stop"),
                USAGE_CHUNK,
            )
        )
    )
    events = run(system="Sei nett.", temperature=0.5)
    assert events == [Delta("Hal"), Delta("lo"), Delta(" Welt"), Usage(12, 7), Done("stop")]

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.5
    assert body["messages"] == [
        {"role": "system", "content": "Sei nett."},
        {"role": "user", "content": "Hallo"},
    ]
    assert "tools" not in body
    assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_empty_deltas_and_comments_ignored(mock):
    body = sse(
        {"choices": [{"index": 0, "delta": {}}]},
        content(""),
        content(None),
        content("A"),
        {"choices": [{"index": 0, "delta": {"content": "B"}, "finish_reason": "length"}]},
        comments=(": OPENROUTER PROCESSING", ": OPENROUTER PROCESSING"),
    )
    # Kommentar auch mitten im Stream.
    body = body.replace(b"data: [DONE]", b": OPENROUTER PROCESSING\n\ndata: [DONE]")
    mock.post(f"{BASE}/chat/completions").mock(return_value=sse_response(body))
    assert run() == [Delta("A"), Delta("B"), Done("length")]


def test_tools_in_openai_form_accepted(mock):
    route = mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(sse(content("x", finish="stop")))
    )
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    run(tools=tools)
    assert json.loads(route.calls.last.request.content)["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "t",
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_reserved_params_not_overridden(mock):
    route = mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(sse(content("x", finish="stop")))
    )
    run(stream=False, model="anders")
    body = json.loads(route.calls.last.request.content)
    assert body["stream"] is True and body["model"] == "test-model"


def test_without_key_no_authorization_header(mock):
    route = mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(sse(content("x", finish="stop")))
    )
    run(make_adapter(api_key=""))
    assert "Authorization" not in route.calls.last.request.headers


def test_default_base_url_and_openrouter_header(mock):
    openai = mock.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=sse_response(sse(content("x", finish="stop")))
    )
    assert run(make_adapter(base_url=""))[-1] == Done("stop")
    assert "X-Title" not in openai.calls.last.request.headers

    router = mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=sse_response(sse(content("x", finish="stop")))
    )
    run(make_adapter(base_url="https://openrouter.ai/api/v1/"))
    assert router.calls.last.request.headers["X-Title"] == "MultiGPT"


def test_missing_done_with_finish_reason_is_complete(mock):
    mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(sse(content("x", finish="stop"), done=False))
    )
    assert run() == [Delta("x"), Done("stop")]


# --- Fehler ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "retryable", "fragment"),
    [
        (401, False, "API-Key"),
        (404, False, "nicht gefunden"),
        (429, True, "zu viele Anfragen"),
        (500, True, "Serverfehler"),
        (503, True, "Serverfehler"),
    ],
)
def test_http_errors(mock, caplog, status, retryable, fragment):
    error_body = {
        "error": {
            "message": f"Incorrect API key provided: {KEY}",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }
    mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(status, json=error_body))
    with caplog.at_level(logging.DEBUG):
        events = run()
    assert len(events) == 1
    (error,) = events
    assert isinstance(error, Error)
    assert error.retryable is retryable
    assert fragment in error.message
    assert KEY not in error.message
    assert KEY not in caplog.text


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (httpx.ConnectError("refused"), "nicht erreichbar"),
        (httpx.ConnectTimeout("timeout"), "nicht erreichbar"),
        (httpx.ReadTimeout("timeout"), "nicht rechtzeitig"),
    ],
)
def test_network_errors(mock, exc, fragment):
    mock.post(f"{BASE}/chat/completions").mock(side_effect=exc)
    (error,) = run()
    assert isinstance(error, Error)
    assert error.retryable is True
    assert fragment in error.message


def test_connection_lost_mid_stream(mock):
    stream = TrackingStream(
        [
            f"data: {json.dumps(content('Erster '))}\n\n".encode(),
            f"data: {json.dumps(content('Teil'))}\n\n".encode(),
            b"data: never",
        ],
        fail_after=2,
    )
    mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, stream=stream))
    events = run()
    assert events[:2] == [Delta("Erster "), Delta("Teil")]
    assert isinstance(events[-1], Error)
    assert events[-1].retryable is True
    assert "unterbrochen" in events[-1].message
    assert len(events) == 3
    assert stream.closed


def test_stream_ends_without_done_or_finish_reason(mock):
    mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(sse(content("halb"), done=False))
    )
    events = run()
    assert events[0] == Delta("halb")
    assert isinstance(events[-1], Error) and events[-1].retryable


def test_mid_stream_error_chunk(mock):
    # OpenRouter: Fehler nach HTTP 200 als Chunk mit error und finish_reason "error".
    error_chunk = {
        "error": {"code": 502, "message": "Provider disconnected"},
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
    }
    mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(sse(content("Teil"), error_chunk, done=False))
    )
    events = run()
    assert events[0] == Delta("Teil")
    assert isinstance(events[-1], Error)
    assert "Provider disconnected" not in events[-1].message


def test_unexpected_exception_becomes_error(mock):
    mock.post(f"{BASE}/chat/completions").mock(side_effect=RuntimeError(KEY))
    (error,) = run()
    assert isinstance(error, Error)
    assert error.retryable is False
    assert KEY not in error.message


def test_close_closes_connection(mock):
    parts = [f"data: {json.dumps(content(str(i)))}\n\n".encode() for i in range(100)]
    stream = TrackingStream(parts)
    mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, stream=stream))
    gen = make_adapter().stream("test-model", MESSAGES)
    assert next(gen) == Delta("0")
    assert next(gen) == Delta("1")
    assert not stream.closed
    gen.close()
    assert stream.closed


# --- Modellliste und Online-Status ------------------------------------------


MODELS = {"object": "list", "data": [{"id": "gpt-a", "object": "model"}, {"id": "gpt-b"}]}


def test_list_models(mock):
    route = mock.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json=MODELS))
    assert make_adapter().list_models() == ["gpt-a", "gpt-b"]
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {KEY}"


def test_list_models_without_key(mock):
    route = mock.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json=MODELS))
    make_adapter(api_key="").list_models()
    assert "Authorization" not in route.calls.last.request.headers


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": KEY}}),
        httpx.Response(200, text="kein json"),
        httpx.ConnectError("refused"),
    ],
)
def test_list_models_errors(mock, response):
    route = mock.get(f"{BASE}/models")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    with pytest.raises(ProviderError) as info:
        make_adapter().list_models()
    assert KEY not in str(info.value)


def test_is_online_true(mock):
    mock.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json=MODELS))
    assert make_adapter().is_online() is True


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(500),
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("timeout"),
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


# --- Werkzeuge (M4a) ---------------------------------------------------------

WEATHER = ToolSpec(
    name="wetter",
    description="Wetter für einen Ort",
    parameters={"type": "object", "properties": {"ort": {"type": "string"}}, "required": ["ort"]},
)


def tool_delta(index, call_id=None, name=None, arguments=None, finish=None):
    part = {"index": index, "function": {}}
    if call_id:
        part.update(id=call_id, type="function")
    if name:
        part["function"]["name"] = name
    if arguments is not None:
        part["function"]["arguments"] = arguments
    return {"choices": [{"index": 0, "delta": {"tool_calls": [part]}, "finish_reason": finish}]}


def finish_chunk(reason):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def test_tool_definition_and_choice_in_body(mock):
    route = mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(sse(content("x", finish="stop")))
    )
    run(tools=[WEATHER], tool_choice="wetter")
    body = json.loads(route.calls.last.request.content)
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "wetter",
                "description": "Wetter für einen Ort",
                "parameters": WEATHER.parameters,
            },
        }
    ]
    assert body["tool_choice"] == {"type": "function", "function": {"name": "wetter"}}
    run(tools=[WEATHER], tool_choice="required")
    assert json.loads(route.calls.last.request.content)["tool_choice"] == "required"
    run(tools=[WEATHER])
    assert "tool_choice" not in json.loads(route.calls.last.request.content)


def test_streamed_parallel_tool_calls(mock):
    body = sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]},
        tool_delta(0, "call_a", "wetter", ""),
        tool_delta(0, arguments='{"o'),
        tool_delta(0, arguments='rt": "Kö'),
        tool_delta(1, "call_b", "wetter", '{"ort"'),
        tool_delta(0, arguments='ln"}'),
        tool_delta(1, arguments=': "Bonn"}'),
        tool_delta(2, "call_c", "uhrzeit"),
        finish_chunk("tool_calls"),
        USAGE_CHUNK,
    )
    mock.post(f"{BASE}/chat/completions").mock(return_value=sse_response(body))
    assert run(tools=[WEATHER]) == [
        ToolCallEvent("call_a", "wetter", {"ort": "Köln"}),
        ToolCallEvent("call_b", "wetter", {"ort": "Bonn"}),
        ToolCallEvent("call_c", "uhrzeit", {}),
        Usage(12, 7),
        Done("tool_calls"),
    ]


def test_tool_calls_without_index_or_id_and_stop_reason():
    # Manche kompatiblen Server: kein index, keine id, finish_reason "stop".
    parser = _ChunkParser(emit_tool_calls=True)
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"function": {"name": "wetter"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": {"ort": "Köln"}}}]}}]},
        finish_chunk("stop"),
    ]
    events = [e for c in chunks for e in parser.feed(f"data: {json.dumps(c)}")]
    events += list(parser.end_of_stream())
    call = events[0]
    assert call.id.startswith("call_")
    assert call == ToolCallEvent(call.id, "wetter", {"ort": "Köln"})
    assert events[-1] == Done("tool_calls")


@pytest.mark.parametrize("raw", ['{"ort": "Kö', '"Köln"'])
def test_invalid_tool_arguments_become_error(mock, raw):
    body = sse(tool_delta(0, "call_a", "wetter", raw), finish_chunk("tool_calls"))
    mock.post(f"{BASE}/chat/completions").mock(return_value=sse_response(body))
    events = run(tools=[WEATHER])
    assert len(events) == 1 and isinstance(events[0], Error) and events[0].retryable
    assert "ungültige Argumente" in events[0].message and "wetter" in events[0].message


def test_history_with_tool_calls_and_results(mock):
    route = mock.post(f"{BASE}/chat/completions").mock(
        return_value=sse_response(sse(content("x", finish="stop")))
    )
    history = [
        ChatMessage("user", "Wetter?"),
        ChatMessage(
            "assistant",
            "",
            tool_calls=[
                ToolCallEvent("call_a", "wetter", {"ort": "Köln"}),
                ToolCallEvent("call_b", "wetter", {"ort": "Bonn"}),
            ],
        ),
        ChatMessage("tool", "Sonne", tool_call_id="call_a", name="wetter"),
        ChatMessage("tool", "Dienst aus", tool_call_id="call_b", name="wetter", is_error=True),
        ChatMessage("tool", "verwaist", tool_call_id="call_z", name="wetter"),
    ]
    list(make_adapter().stream("test-model", history, tools=[WEATHER]))
    messages = json.loads(route.calls.last.request.content)["messages"]
    assert messages == [
        {"role": "user", "content": "Wetter?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "wetter", "arguments": '{"ort": "Köln"}'},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "wetter", "arguments": '{"ort": "Bonn"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "Sonne"},
        {"role": "tool", "tool_call_id": "call_b", "content": "Fehler: Dienst aus"},
    ]


# --- Abgekündigte Modelle ------------------------------------------------------


def test_model_not_found_names_the_model(mock):
    body = {
        "error": {
            "message": "The model `gpt-old` has been deprecated",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }
    mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(404, json=body))
    events = list(make_adapter().stream("gpt-old", [ChatMessage("user", "Hallo")]))
    (error,) = events
    assert isinstance(error, Error)
    assert "„gpt-old“" in error.message
    assert "nicht (mehr) verfügbar" in error.message
    assert error.retryable is False


def test_list_models_skips_shut_down_models(mock):
    models = {
        "object": "list",
        "data": [
            {"id": "gpt-current"},
            {"id": "gpt-retired", "shutdown_date": "2020-01-01"},
            {"id": "gpt-retired-epoch", "shutdown_date": 1577836800},
            {"id": "gpt-soon", "shutdown_date": "2999-12-31"},
            {"id": "gpt-odd", "shutdown_date": "kein Datum"},
        ],
    }
    mock.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json=models))
    assert make_adapter().list_models() == ["gpt-current", "gpt-soon", "gpt-odd"]
