"""The chat agent loop, both backends' wire formats, the chat log and replay.

No real model is called: a scripted backend, a fake Anthropic client and a
fake OpenAI-compatible HTTP server stand in, so the tests are exact and offline.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from apex_diligence.agent import (Agent, AnthropicBackend, AssistantTurn, BackendError, OpenAICompatibleBackend,
                                  ToolCall, replay)
from apex_diligence.cli import main
from apex_diligence.workspace import open_workspace


class ScriptedBackend:
    """Plays back fixed turns and records what the agent sent it."""

    name, model = "scripted", "test"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def complete(self, system, history, tools):
        self.seen.append([dict(h) for h in history])
        return self.turns.pop(0)


@pytest.fixture
def ws(demo_inputs, tmp_path):
    return open_workspace(demo_inputs, tmp_path)


def test_agent_runs_tools_then_answers(ws, tmp_path):
    backend = ScriptedBackend([
        AssistantTurn("Let me check.", [ToolCall("t1", "get_financials", {"period": "ltm"}),
                                        ToolCall("t2", "lbo_model", {"entry_multiple": 6})]),
        AssistantTurn("LTM adjusted EBITDA is 10,441,177.00 and the IRR is 19.79%."),
    ])
    log = tmp_path / "chat.jsonl"
    agent = Agent(ws, backend, log_path=log)
    answer = agent.ask("What is EBITDA and the IRR at 6x?")
    assert answer.startswith("LTM adjusted EBITDA")

    results = backend.seen[1][-1]["results"]
    assert [r["id"] for r in results] == ["t1", "t2"]
    assert json.loads(results[0]["content"])["adjusted_ebitda"] == "10441177.00"
    assert json.loads(results[1]["content"])["returns"]["irr_pct"] == "19.79"

    records = [json.loads(l) for l in log.read_text().splitlines()]
    assert [r["type"] for r in records] == ["session", "user", "tool_call", "tool_call", "assistant"]
    assert "Every number you state must come from a tool result" in agent.system


def test_tool_errors_go_back_to_the_model(ws):
    backend = ScriptedBackend([
        AssistantTurn("", [ToolCall("t1", "lbo_model", {"multiple": 6})]),
        AssistantTurn("", [ToolCall("t2", "lbo_model", {"entry_multiple": 6})]),
        AssistantTurn("Fixed it."),
    ])
    assert Agent(ws, backend).ask("LBO please") == "Fixed it."
    first = backend.seen[1][-1]["results"][0]
    assert first["is_error"] and "unknown field" in first["content"]


def test_agent_stops_runaway_loops(ws):
    backend = ScriptedBackend([AssistantTurn("", [ToolCall(f"t{i}", "run_checks", {})]) for i in range(3)])
    assert Agent(ws, backend, max_rounds=3).ask("loop").startswith("Stopped after 3 rounds")


def test_replay_verifies_and_catches_tampering(ws, tmp_path):
    log = tmp_path / "chat.jsonl"
    backend = ScriptedBackend([
        AssistantTurn("", [ToolCall("t1", "cash_flow_13_week", {}), ToolCall("t2", "export_model",
                                                                               {"model": "lbo", "inputs": {"entry_multiple": 6}}),
                           ToolCall("t3", "lbo_model", {"bad": 1})]),
        AssistantTurn("done"),
    ])
    Agent(ws, backend, log_path=log).ask("forecast and export")
    checked, problems = replay(log, tmp_path / "replay")
    assert checked == 3 and problems == []

    lines = log.read_text().splitlines()
    record = json.loads(lines[2])
    record["output"]["ending_cash_week_13"] = "99999999.99"
    lines[2] = json.dumps(record)
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("\n".join(lines) + "\n")
    _, problems = replay(tampered, tmp_path / "replay2")
    assert any("edited" in p for p in problems)


def test_anthropic_backend_wire_format(ws):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(stop_reason="tool_use", content=[
                SimpleNamespace(type="text", text="Checking."),
                SimpleNamespace(type="tool_use", id="toolu_1", name="get_financials", input={"period": "FY2025"})])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done.")])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    agent = Agent(ws, AnthropicBackend(model="claude-test", client=client))
    assert agent.ask("FY2025?") == "Done."
    second = calls[1]
    assert second["model"] == "claude-test" and second["tools"][0]["input_schema"]["type"] == "object"
    assistant, results = second["messages"][1], second["messages"][2]
    assert assistant["content"][1] == {"type": "tool_use", "id": "toolu_1", "name": "get_financials",
                                       "input": {"period": "FY2025"}}
    block = results["content"][0]
    assert results["role"] == "user" and block["type"] == "tool_result" and block["tool_use_id"] == "toolu_1"
    assert block["is_error"] is False and json.loads(block["content"])["period"] == "FY2025"


def test_anthropic_backend_reports_api_errors(ws):
    def create(**kwargs):
        raise RuntimeError("overloaded")
    backend = AnthropicBackend(client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    with pytest.raises(BackendError, match="overloaded"):
        Agent(ws, backend).ask("hi")


class FakeOpenAIServer:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                data = json.dumps(outer.replies.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def test_local_backend_against_fake_server(ws):
    server = FakeOpenAIServer([
        {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "nwc_analysis", "arguments": "{\"peg_months\": 6}"}},
            {"id": "c2", "type": "function", "function": {"name": "run_checks", "arguments": "not json"}}]}}]},
        {"choices": [{"message": {"content": "The peg is 2,659,356.18."}}]},
    ])
    try:
        backend = OpenAICompatibleBackend(model="qwen-test", base_url=server.url, api_key="k")
        assert Agent(ws, backend).ask("peg?") == "The peg is 2,659,356.18."
    finally:
        server.close()
    first, second = server.requests
    assert first["path"] == "/v1/chat/completions" and first["auth"] == "Bearer k"
    assert first["body"]["messages"][0]["role"] == "system" and first["body"]["temperature"] == 0
    assert first["body"]["tools"][0]["type"] == "function"
    tool_messages = [m for m in second["body"]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2"]
    assert "peg" in json.loads(tool_messages[0]["content"])
    assert "unknown field" in json.loads(tool_messages[1]["content"])["error"]  # bad JSON reported, not crashed


def test_local_backend_unreachable(ws):
    backend = OpenAICompatibleBackend(base_url="http://127.0.0.1:9/v1", timeout=2)
    with pytest.raises(BackendError, match="cannot reach"):
        Agent(ws, backend).ask("hi")


def test_cli_tool_and_replay(tmp_path, capsys):
    assert main(["tool", "--demo", "--out", str(tmp_path), "calculator", "--input", '{"expression": "2*3.5"}']) == 0
    assert '"result": "7.00"' in capsys.readouterr().out
    assert main(["tool", "--demo", "--out", str(tmp_path), "lbo_model", "--input", "{}"]) == 1
    assert main(["tool"]) == 0
    assert "cash_flow_13_week" in capsys.readouterr().out


def test_cli_chat_without_key_explains(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["chat", "--demo", "--out", str(tmp_path), "--ask", "hi"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
