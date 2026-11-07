"""The chat analyst: a language model that answers questions by calling tools.

The loop is the standard tool-use (ReAct-style) pattern:
    user question -> model -> tool calls -> tool results -> model -> ... -> answer

What makes it safe for diligence work:
- Every number comes from a deterministic tool (see tools.py). The system prompt
  forbids the model from computing figures itself; a calculator tool covers
  simple arithmetic exactly.
- Every tool call, its input and output are written to a JSONL log. `apex replay`
  re-runs each call against the same inputs and proves the outputs are identical.
- Two backends: Claude through the Anthropic API, or any OpenAI-compatible local
  server (Ollama, vLLM, llama.cpp) for air-gapped deployments.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import __version__
from .pipeline import RunInputs, sha256_bytes
from .schema import canonical
from .tools import Toolbox
from .workspace import Workspace, open_workspace

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOOL_ROUNDS = 12


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class Backend(Protocol):
    name: str
    model: str

    def complete(self, system: str, history: list[dict], tools: list[dict]) -> AssistantTurn: ...


# ------------------------------------------------------------------ history format
#
# The agent keeps a neutral history and each backend converts it:
#   {"role": "user", "text": "..."}
#   {"role": "assistant", "text": "...", "tool_calls": [{"id", "name", "input"}]}
#   {"role": "tool", "results": [{"id", "content", "is_error"}]}

def to_anthropic_messages(history: list[dict]) -> list[dict]:
    messages = []
    for item in history:
        if item["role"] == "user":
            messages.append({"role": "user", "content": item["text"]})
        elif item["role"] == "assistant":
            blocks = [{"type": "text", "text": item["text"]}] if item["text"] else []
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                       for c in item["tool_calls"]]
            messages.append({"role": "assistant", "content": blocks})
        else:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"], "is_error": r["is_error"]}
                for r in item["results"]]})
    return messages


def to_openai_messages(system: str, history: list[dict]) -> list[dict]:
    messages = [{"role": "system", "content": system}]
    for item in history:
        if item["role"] == "user":
            messages.append({"role": "user", "content": item["text"]})
        elif item["role"] == "assistant":
            message = {"role": "assistant", "content": item["text"] or None}
            if item["tool_calls"]:
                message["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                                         for c in item["tool_calls"]]
            messages.append(message)
        else:
            messages += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in item["results"]]
    return messages


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


# ------------------------------------------------------------------ backends

class AnthropicBackend:
    """Claude via the Anthropic Python SDK (pip install anthropic). Needs ANTHROPIC_API_KEY."""

    name = "anthropic"

    def __init__(self, model: str | None = None, max_tokens: int = 4096, client=None):
        self.model = model or os.environ.get("APEX_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens
        if client is None:
            try:
                import anthropic
            except ImportError:
                raise BackendError("the Claude backend needs the anthropic package: pip install -e '.[agent]'") from None
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise BackendError("set ANTHROPIC_API_KEY to use Claude, or use --backend local")
            client = anthropic.Anthropic()
        self.client = client

    def complete(self, system: str, history: list[dict], tools: list[dict]) -> AssistantTurn:
        try:
            response = self.client.messages.create(model=self.model, max_tokens=self.max_tokens, system=system,
                                                   tools=tools, messages=to_anthropic_messages(history))
        except Exception as e:  # the SDK raises several error types; report them all the same way
            raise BackendError(f"Claude API error: {e}") from None
        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "max_tokens" and not calls:
            text += "\n[answer cut off at the token limit]"
        return AssistantTurn(text, calls)


class OpenAICompatibleBackend:
    """Any server with an OpenAI-style /chat/completions endpoint and tool calling:
    Ollama (default http://localhost:11434/v1), vLLM, llama.cpp server. Uses only the standard library."""

    name = "local"

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 timeout: int = 300):
        self.model = model or os.environ.get("APEX_LOCAL_MODEL", "qwen3:14b")
        self.base_url = (base_url or os.environ.get("APEX_LOCAL_URL", "http://localhost:11434/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("APEX_LOCAL_API_KEY")
        self.timeout = timeout
        self.counter = 0

    def complete(self, system: str, history: list[dict], tools: list[dict]) -> AssistantTurn:
        body = {"model": self.model, "messages": to_openai_messages(system, history),
                "tools": to_openai_tools(tools), "temperature": 0}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise BackendError(f"local model server returned {e.code}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BackendError(f"cannot reach the local model server at {self.base_url}: {e}") from None
        except json.JSONDecodeError:
            raise BackendError("local model server did not return JSON") from None
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise BackendError(f"unexpected response from local model server: {str(data)[:300]}") from None

        calls = []
        for raw in message.get("tool_calls") or []:
            self.counter += 1
            function = raw.get("function", {})
            arguments = function.get("arguments") or "{}"
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
                if not isinstance(parsed, dict):
                    raise ValueError
            except ValueError:
                parsed = {"__unparseable_arguments__": str(arguments)[:500]}
            calls.append(ToolCall(raw.get("id") or f"call_{self.counter}", function.get("name", ""), parsed))
        return AssistantTurn(message.get("content") or "", calls)


# ------------------------------------------------------------------ system prompt

def system_prompt(ws: Workspace) -> str:
    base = ws.result
    pending = sum(1 for r in base.reviewed if r.status == "pending")
    failing = [c.name for c in base.checks if not c.passed]
    return f"""You are Apex, a financial due diligence analyst working on {base.company}.
Ledger: {ws.months[0]} to {ws.months[-1]}. Currency: {ws.currency}. Fiscal year ends in month {base.config.fiscal_year_end_month}.
Adjustment candidates pending review: {pending}. Checks needing attention: {', '.join(failing) or 'none'}.

Rules you must follow:
1. Every number you state must come from a tool result in this conversation. Never calculate, estimate,
   round differently or recall figures yourself. For arithmetic on tool figures, call the calculator tool.
2. When a model used default or derived assumptions, name the ones that matter for the answer.
3. Adjusted EBITDA includes only reviewer-accepted adjustments. If candidates are pending, say so when it matters.
4. Cite evidence by candidate id and source row (file:row) when discussing findings.
5. If a request is missing an input with no sensible default (for example an LBO entry multiple), ask for it.
6. You cannot change data or reviewer decisions; reviewers record decisions in review.csv.
7. Tool inputs: amounts in currency units, percentages as percent numbers (9 means 9%), multiples as numbers.
8. Be concise. Lead with the answer, then the key figures, then caveats."""


# ------------------------------------------------------------------ agent

class ChatLog:
    """Append-only JSONL record of a chat session, used by `apex replay`."""

    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class Agent:
    def __init__(self, workspace: Workspace, backend: Backend, log_path: Path | None = None,
                 max_rounds: int = MAX_TOOL_ROUNDS, on_tool=None):
        self.ws = workspace
        self.backend = backend
        self.toolbox = Toolbox(workspace)
        self.system = system_prompt(workspace)
        self.history: list[dict] = []
        self.max_rounds = max_rounds
        self.on_tool = on_tool  # optional callback(name, input, is_error) for the CLI to show progress
        self.log = ChatLog(log_path)
        inputs = workspace.inputs
        files = {"gl": inputs.gl, "bank": inputs.bank, "mapping": inputs.mapping,
                 "rules": inputs.rules, "decisions": inputs.decisions}
        self.log.write({
            "type": "session", "tool": "apex-diligence", "version": __version__,
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "company": inputs.company, "backend": backend.name, "model": backend.model,
            "results_sha256": workspace.result.results_hash(),
            "inputs": {k: {"path": str(Path(p).resolve()), "sha256": sha256_bytes(Path(p).read_bytes())}
                       for k, p in files.items() if p},
        })

    def ask(self, question: str) -> str:
        self.history.append({"role": "user", "text": question})
        self.log.write({"type": "user", "text": question})
        for _ in range(self.max_rounds):
            turn = self.backend.complete(self.system, self.history, self.toolbox.specs())
            self.history.append({"role": "assistant", "text": turn.text,
                                 "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in turn.tool_calls]})
            if not turn.tool_calls:
                self.log.write({"type": "assistant", "text": turn.text})
                return turn.text
            results = []
            for call in turn.tool_calls:
                output, is_error = self.toolbox.call(call.name, call.input)
                content = canonical(output)
                results.append({"id": call.id, "content": content, "is_error": is_error})
                self.log.write({"type": "tool_call", "id": call.id, "name": call.name, "input": call.input,
                                "is_error": is_error, "output": output,
                                "output_sha256": hashlib.sha256(content.encode()).hexdigest()})
                if self.on_tool:
                    self.on_tool(call.name, call.input, is_error)
            self.history.append({"role": "tool", "results": results})
        message = (f"Stopped after {self.max_rounds} rounds of tool calls without a final answer. "
                   "Try a narrower question.")
        self.log.write({"type": "assistant", "text": message})
        return message


# ------------------------------------------------------------------ replay

def replay(log_path: Path, out_dir: Path) -> tuple[int, list[str]]:
    """Re-run every tool call in a chat log. Returns (calls checked, problems)."""
    records = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    sessions = [r for r in records if r.get("type") == "session"]
    if len(sessions) != 1 or records[0].get("type") != "session":
        return 0, ["log must start with exactly one session record"]
    session, problems = sessions[0], []
    for name, entry in session["inputs"].items():
        path = Path(entry["path"])
        if not path.exists():
            problems.append(f"input '{name}' is missing: {path}")
        elif sha256_bytes(path.read_bytes()) != entry["sha256"]:
            problems.append(f"input '{name}' has changed since the chat: {path}")
    if problems:
        return 0, problems

    paths = {k: Path(v["path"]) for k, v in session["inputs"].items()}
    ws = open_workspace(RunInputs(gl=paths["gl"], bank=paths["bank"], mapping=paths["mapping"],
                                  rules=paths.get("rules"), decisions=paths.get("decisions"),
                                  company=session["company"]), out_dir)
    if ws.result.results_hash() != session["results_sha256"]:
        problems.append("the pipeline results differ from the chat session")
    toolbox, checked = Toolbox(ws), 0
    for record in records:
        if record.get("type") != "tool_call":
            continue
        checked += 1
        recorded = hashlib.sha256(canonical(record["output"]).encode()).hexdigest()
        if recorded != record["output_sha256"]:
            problems.append(f"call {record['id']} ({record['name']}): the logged output was edited")
            continue
        output, is_error = toolbox.call(record["name"], record["input"])
        if hashlib.sha256(canonical(output).encode()).hexdigest() != record["output_sha256"] or is_error != record["is_error"]:
            problems.append(f"call {record['id']} ({record['name']}): re-running gives a different result")
    return checked, problems


def describe_call(name: str, arguments: dict) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in list(arguments.items())[:4])
    return f"{name}({shown}{', ...' if len(arguments) > 4 else ''})"


__all__ = ["Agent", "AnthropicBackend", "OpenAICompatibleBackend", "BackendError", "replay"]
