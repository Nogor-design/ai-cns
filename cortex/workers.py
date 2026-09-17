"""Headless CLI worker adapters.

Commands are argument arrays and execute without a shell. Full task briefs use
stdin where supported so private context is not exposed in process listings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config, ollama_client


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    worker: str
    argv: tuple[str, ...]
    uses_stdin: bool
    # Extra environment for the child process, e.g. a Cortex-owned tool config.
    env: tuple[tuple[str, str], ...] = ()

    @property
    def display(self) -> str:
        return subprocess.list2cmdline(list(self.argv))


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int
    stdout: str
    stderr: str
    usage: dict[str, object] | None


_PROBE_CACHE: dict[str, tuple[float, dict[str, str | None]]] = {}
_PROBE_LOCK = threading.Lock()
_PROBE_INFLIGHT: set[str] = set()


def available(worker: str) -> bool:
    if worker == "ollama":
        return ollama_client.available()
    return worker != "perplexity" and shutil.which(_executable(worker)) is not None


def probe_cached(worker: str, *, max_age: float = 300) -> dict[str, str | None]:
    """Non-blocking availability lookup for request paths.

    ``probe`` shells out to each CLI's auth command, which costs most of a
    second across the team. The dashboard polls every couple of seconds, so it
    reads the last known answer and refreshes in the background instead of
    paying that cost inline.
    """
    cached = _PROBE_CACHE.get(worker)
    if cached and time.monotonic() - cached[0] < max_age:
        return dict(cached[1])
    with _PROBE_LOCK:
        if worker not in _PROBE_INFLIGHT:
            _PROBE_INFLIGHT.add(worker)
            threading.Thread(
                target=_refresh_probe, args=(worker,),
                name=f"cortex-probe-{worker}", daemon=True,
            ).start()
    if cached:
        # Serve the stale answer while the refresh runs; it is almost always
        # still correct and avoids the UI flickering to "checking".
        return dict(cached[1])
    return {"availability": "checking", "note": "Checking CLI availability"}


def _refresh_probe(worker: str) -> None:
    try:
        probe(worker, max_age=0)
    except Exception:  # a probe failure must never kill the background thread
        pass
    finally:
        with _PROBE_LOCK:
            _PROBE_INFLIGHT.discard(worker)


def probe(worker: str, *, max_age: float = 300) -> dict[str, str | None]:
    cached = _PROBE_CACHE.get(worker)
    if max_age and cached and time.monotonic() - cached[0] < max_age:
        return dict(cached[1])
    if worker == "perplexity":
        result = {"availability": "manual", "note": "No local CLI configured"}
    elif not available(worker):
        result = {"availability": "missing", "note": "CLI command not found"}
    elif worker in {"codex", "claude", "grok"}:
        auth_command = {
            "codex": ["codex", "login", "status"],
            "claude": ["claude", "auth", "status"],
            "grok": ["grok", "models"],
        }[worker]
        try:
            proc = subprocess.run(
                auth_command, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=6,
            )
            combined = f"{proc.stdout}\n{proc.stderr}".lower()
            auth_failed = proc.returncode != 0 or any(
                marker in combined
                for marker in ("not authenticated", '"loggedin": false', "not logged in")
            )
            result = {
                "availability": "needs_auth" if auth_failed else "ready",
                "note": f"{worker.title()} CLI needs sign-in" if auth_failed else None,
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = {"availability": "unavailable", "note": str(exc)}
    elif worker == "opencode":
        # Credential names only; opencode never prints the keys themselves.
        try:
            proc = subprocess.run(
                [_executable("opencode"), "auth", "list"], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            signed_in = "opencode go" in f"{proc.stdout}\n{proc.stderr}".lower()
            result = {
                "availability": "ready" if signed_in else "needs_auth",
                "note": None if signed_in else "Run `opencode auth login` and add the OpenCode Go key",
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = {"availability": "unavailable", "note": str(exc)}
    else:
        result = {"availability": "ready", "note": None}
    _PROBE_CACHE[worker] = (time.monotonic(), result)
    return dict(result)


def build_command(
    *,
    worker: str,
    model: str,
    action: str,
    workspace: str | Path,
    brief_path: str | Path,
    budget: str,
    effort: str | None = None,
) -> CommandSpec:
    workspace = str(workspace)
    brief_path = str(brief_path)
    write = action == "implement"

    if worker == "codex":
        argv = ["codex", "exec"]
        if effort:
            argv.extend(["--config", f'model_reasoning_effort="{effort}"'])
        argv.extend([
            "--json", "--sandbox",
            "workspace-write" if write else "read-only", "-C", workspace,
        ])
        if model and model != "default":
            argv.extend(["--model", model])
        argv.append("-")
        return CommandSpec(worker, tuple(argv), True)

    if worker == "claude":
        # stream-json emits events as the agent works; plain "json" buffers
        # everything until exit, which makes a long run look frozen.
        argv = [
            "claude", "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits" if write else "plan",
            "--max-turns", _max_turns(budget), "--no-session-persistence",
        ]
        if model and model != "default":
            argv.extend(["--model", model])
        if effort:
            argv.extend(["--effort", effort])
        return CommandSpec(worker, tuple(argv), True)

    if worker == "gemini":
        argv = [
            "gemini", "-p", "Follow the complete task brief provided on stdin.",
            "--output-format", "json", "--approval-mode",
            "auto_edit" if write else "plan", "--sandbox",
        ]
        if model and model != "default":
            argv.extend(["--model", model])
        return CommandSpec(worker, tuple(argv), True)

    if worker == "grok":
        argv = [
            "grok", "--no-auto-update", "--prompt-file", brief_path,
            "--output-format", "json", "--permission-mode",
            "acceptEdits" if write else "plan", "--cwd", workspace,
        ]
        if model and model != "default":
            argv.extend(["--model", model])
        if effort:
            argv.extend(["--reasoning-effort", effort])
        return CommandSpec(worker, tuple(argv), False)

    if worker == "agy":
        # Antigravity's print mode takes the prompt as a flag value and has no
        # plain-text stdin mode, so the prompt points at the brief file instead
        # of carrying private context on the command line. Headless mode
        # auto-denies anything that needs a permission prompt; without
        # accept-edits that makes a run effectively read-only.
        run_dir = str(Path(brief_path).parent)
        argv = [
            _executable("agy"), "--output-format", "stream-json", "--sandbox",
            "--print-timeout", "60m", "--add-dir", run_dir,
        ]
        if write:
            argv.extend(["--mode", "accept-edits"])
        if model and model != "default":
            argv.extend(["--model", model])
        if effort:
            argv.extend(["--effort", "high" if effort == "xhigh" else effort])
        argv.append(
            f"--print=Read the complete task brief at {brief_path} with view_file "
            "and follow it. Work only inside the current workspace."
        )
        return CommandSpec(worker, tuple(argv), False)

    if worker == "opencode":
        # OpenCode Go subscription models, using the owner's own opencode login.
        # Only the opencode-go provider is accepted: other opencode providers
        # (for example Zen) bill per use.
        argv = [
            *_opencode_run_prefix(),
            "--model", opencode_go_model(model),
            "--agent", "build" if write else "plan",
            "--dir", workspace, "--file", brief_path,
        ]
        variant = {"high": "high", "xhigh": "max"}.get(str(effort or ""))
        if variant:
            argv.extend(["--variant", variant])
        return CommandSpec(worker, tuple(argv), False)

    if worker == "opencode-local":
        # Local-only by construction: the model must be an installed Ollama
        # model, and the provider config is Cortex-owned so the owner's own
        # opencode settings are never edited.
        local_model = opencode_model(model)
        config_path = write_opencode_config(local_model)
        argv = [
            *_opencode_run_prefix(),
            "--model", f"ollama/{local_model}",
            "--agent", "build" if write else "plan",
            "--dir", workspace, "--file", brief_path,
        ]
        return CommandSpec(
            worker, tuple(argv), False,
            env=(("OPENCODE_CONFIG", str(config_path)),),
        )

    if worker == "ollama":
        return CommandSpec(worker, ("ollama", "run", model or "phi4:14b"), True)

    if worker == "perplexity":
        raise WorkerError(
            "Perplexity is configured as a manual/API research worker; no CLI was found."
        )
    raise WorkerError(f"unknown worker: {worker}")


WATCHDOG_POLL_SECONDS = 3.0


class WorkerStopped(WorkerError):
    """The supervisor stopped a worker; carries what it had written so far."""

    def __init__(self, reason: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stdout = stdout
        self.stderr = stderr
        self.usage = _extract_usage(stdout) if stdout else None


def _kill_tree(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    """Stop a worker and its children; the CLIs spawn node/python helpers."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, check=False,
        )
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def execute(
    spec: CommandSpec,
    *,
    workspace: str | Path,
    brief: str,
    timeout: int,
    on_output: Callable[[str], None] | None = None,
    watchdog: Callable[[], str | None] | None = None,
) -> WorkerResult:
    """Run a worker, reporting output as it arrives.

    ``on_output`` receives incremental text while the worker is still running.
    It is the difference between a progress bar that means nothing and being
    able to see what an agent is doing; failures in the callback are ignored so
    a logging problem can never kill a run.

    ``watchdog`` is polled every few seconds while a CLI worker runs; a returned
    reason stops the worker and its child processes and raises WorkerStopped.
    """
    def emit(text: str) -> None:
        if on_output and text:
            try:
                on_output(text)
            except Exception:  # observation must never break execution
                pass

    if spec.worker == "ollama":
        try:
            response, usage = ollama_client.generate_with_usage(
                brief, model=spec.argv[-1], timeout=float(timeout), on_chunk=emit
            )
        except ollama_client.OllamaUnavailable as exc:
            raise WorkerError(str(exc)) from exc
        return WorkerResult(exit_code=0, stdout=response, stderr="", usage=usage)
    if not available(spec.worker):
        raise WorkerError(f"worker command is not available: {_executable(spec.worker)}")
    try:
        return _stream_subprocess(spec, workspace=workspace, brief=brief,
                                  timeout=timeout, emit=emit, watchdog=watchdog)
    except OSError as exc:
        raise WorkerError(str(exc)) from exc


def _stream_subprocess(
    spec: CommandSpec,
    *,
    workspace: str | Path,
    brief: str,
    timeout: int,
    emit: Callable[[str], None],
    watchdog: Callable[[], str | None] | None = None,
) -> WorkerResult:
    env = None
    if spec.env:
        env = dict(os.environ)
        env.update(dict(spec.env))
    proc = subprocess.Popen(
        list(spec.argv),
        cwd=str(workspace),
        env=env,
        stdin=subprocess.PIPE if spec.uses_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    out_parts: list[str] = []
    err_parts: list[str] = []

    def pump(stream, sink: list[str], mirror: bool) -> None:
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
                if mirror:
                    # The sink keeps raw output for parsing; only the live view
                    # gets the readable rendering.
                    shown = humanize(spec.worker, line)
                    if shown:
                        emit(shown)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, out_parts, True), daemon=True),
        # stderr is where the CLIs put progress chatter; it is captured but not
        # mirrored, so terminal spinner escape codes stay out of the live view.
        threading.Thread(target=pump, args=(proc.stderr, err_parts, False), daemon=True),
    ]
    for reader in readers:
        reader.start()

    # Feed the brief on a thread: a large prompt can exceed the pipe buffer and
    # deadlock against a worker that is still writing its own output.
    if spec.uses_stdin:
        def feed() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(brief)
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        threading.Thread(target=feed, daemon=True).start()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        try:
            proc.wait(timeout=max(0.05, min(WATCHDOG_POLL_SECONDS, remaining)))
            break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            _kill_tree(proc)
            for reader in readers:
                reader.join(timeout=5)
            emit(f"\n[cortex] worker exceeded its {timeout}s budget and was stopped.\n")
            raise WorkerError(f"worker timed out after {timeout}s")
        if watchdog is None:
            continue
        try:
            reason = watchdog()
        except Exception:  # supervision must never kill a healthy run
            reason = None
        if reason:
            _kill_tree(proc)
            for reader in readers:
                reader.join(timeout=5)
            emit(f"\n[cortex] supervisor stopped the worker: {reason}\n")
            raise WorkerStopped(reason, "".join(out_parts), "".join(err_parts))
    for reader in readers:
        reader.join(timeout=10)

    stdout = "".join(out_parts)
    return WorkerResult(
        exit_code=proc.returncode,
        stdout=stdout,
        stderr="".join(err_parts),
        usage=_extract_usage(stdout),
    )


def humanize(worker: str, line: str) -> str | None:
    """Turn one line of a worker's event stream into something worth reading.

    The CLIs are run with structured output so usage and results can be
    captured, but raw JSON is not a live view a person can follow. This renders
    the parts that show progress -- what the agent said, and which tools it
    reached for -- and drops protocol noise. Returns None to show nothing.
    """
    stripped = line.strip()
    if not stripped:
        return None
    if stripped[0] not in "{[":
        return line  # already prose (ollama, or a CLI that ignored the format)
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return line
    if not isinstance(event, dict):
        return line

    if worker == "claude":
        kind = event.get("type")
        if kind == "assistant":
            return _render_content(event.get("message", {}).get("content"))
        if kind == "result":
            if event.get("is_error"):
                reason = event.get("terminal_reason") or event.get("subtype") or "error"
                return f"\n[{reason}]\n"
            return None  # the assistant text has already been shown
        return None

    if worker == "agy":
        update = event.get("step_update")
        if isinstance(update, dict):
            if update.get("step_type") == "agent_response" and update.get("text_delta"):
                return str(update["text_delta"])
            if update.get("step_type") == "tool" and update.get("state") == "ACTIVE":
                info = update.get("tool_info") or {}
                name = update.get("tool_name") or "tool"
                return f"  → {name}({_tool_hint(info.get('parameters'))})\n"
        result = event.get("result")
        if isinstance(result, dict) and result.get("error"):
            return f"\n[{result.get('status') or 'error'}] {result['error']}\n"
        return None

    if worker.startswith("opencode"):
        if event.get("type") == "error":
            error = event.get("error")
            detail = error.get("data", error) if isinstance(error, dict) else error
            return f"\n[error] {str(detail)[:400]}\n"
        part = event.get("part")
        if isinstance(part, dict):
            if part.get("type") == "text" and part.get("text"):
                text = str(part["text"])
                return text if text.endswith("\n") else text + "\n"
            if part.get("type") == "tool":
                state = part.get("state") or {}
                return f"  → {part.get('tool') or 'tool'}({_tool_hint(state.get('input'))})\n"
        return None

    if worker == "codex":
        item = event.get("item")
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text if text.endswith("\n") else text + "\n"
            command = item.get("command")
            if isinstance(command, str) and command.strip():
                return f"  $ {command.strip()[:160]}\n"
        return None

    return line


def _render_content(content: object) -> str | None:
    """Render an Anthropic content array as readable lines."""
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text + "\n")
        elif block.get("type") == "tool_use":
            parts.append(f"  → {block.get('name') or 'tool'}({_tool_hint(block.get('input'))})\n")
    return "".join(parts) or None


def _tool_hint(payload: object) -> str:
    """A short, non-sensitive label for what a tool call is touching."""
    if not isinstance(payload, dict):
        return ""
    for key in (
        "file_path", "path", "pattern", "command", "url", "description",
        "AbsolutePath", "DirectoryPath", "CommandLine", "filePath",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:90]
    return ""


def extract_text(output: str) -> str:
    """Pull the human-readable answer out of a worker's structured output.

    Every CLI is run with a JSON output format so usage telemetry can be
    captured, which means raw stdout is an envelope, not prose. Storing the
    envelope as the run's response put a wall of JSON in front of the person
    who has to review the work; the full transcript stays in the run log.
    """
    if not output or not output.strip():
        return output
    events = _json_objects(output)
    if not events:
        return output
    # opencode streams text parts; the answer is all of them in order.
    parts = [
        str(event["part"].get("text") or "")
        for event in events
        if isinstance(event.get("part"), dict) and event["part"].get("type") == "text"
    ]
    if any(part.strip() for part in parts):
        return "\n".join(part for part in parts if part.strip())
    # Claude and Gemini report a single terminal object; Codex emits a stream
    # of items and the last agent message is the answer.
    for event in reversed(events):
        nested = event.get("result")
        if isinstance(nested, dict):  # Antigravity
            if str(nested.get("response") or "").strip():
                return str(nested["response"])
            if nested.get("error"):
                return str(nested["error"])
        for key in ("result", "response", "text", "content"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
        item = event.get("item")
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            if item["text"].strip():
                return item["text"]
    errors = [
        str(event.get("error"))
        for event in events
        if isinstance(event.get("error"), (str, dict))
    ]
    return errors[-1] if errors else output


def _json_objects(output: str) -> list[dict[str, object]]:
    """Parse output as a JSON object, or as a stream of JSON lines."""
    objects: list[dict[str, object]] = []
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass
    for line in output.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def _extract_usage(output: str) -> dict[str, object] | None:
    objects: list[dict[str, object]] = []
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            objects.append(parsed)
    except json.JSONDecodeError:
        for line in output.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                objects.append(parsed)
    finishes = [
        data["part"]["tokens"] for data in objects
        if isinstance(data.get("part"), dict)
        and data["part"].get("type") == "step-finish"
        and isinstance(data["part"].get("tokens"), dict)
    ]
    if finishes:  # opencode reports tokens (and, for paid providers, cost) per step
        totals: dict[str, object] = {
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0,
        }
        cost = 0.0
        for data in objects:
            part = data.get("part")
            if not (isinstance(part, dict) and part.get("type") == "step-finish"):
                continue
            tokens = part.get("tokens")
            if not isinstance(tokens, dict):
                continue
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            totals["input_tokens"] = int(totals["input_tokens"]) + int(tokens.get("input") or 0)
            totals["output_tokens"] = int(totals["output_tokens"]) + int(tokens.get("output") or 0)
            totals["reasoning_tokens"] = int(totals["reasoning_tokens"]) + int(tokens.get("reasoning") or 0)
            totals["cached_tokens"] = int(totals["cached_tokens"]) + int(cache.get("read") or 0)
            if isinstance(part.get("cost"), (int, float)):
                cost += float(part["cost"])
        if cost:
            totals["cost_usd"] = round(cost, 6)
        return totals
    for data in reversed(objects):
        nested = data.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("usage"), dict):
            return nested["usage"]  # Antigravity
        for key in ("stats", "usage", "modelUsage"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
    return None


def _max_turns(budget: str) -> str:
    """Turn ceiling per budget.

    A "turn" is one model step, and an agentic review spends most of them on
    tool calls before it has anything to say. The previous ceilings (2-10) cut
    runs off mid-investigation: a real review of nine commits stopped after
    four turns with stop_reason=tool_use and was recorded as a failure. These
    values are budgets, not targets -- a run that finishes early still costs
    only what it used.
    """
    return {"local": "12", "small": "25", "medium": "50", "large": "90"}.get(
        budget, "25"
    )


DEFAULT_OPENCODE_MODEL = "qwen3-coder:30b"
DEFAULT_OPENCODE_GO_MODEL = "opencode-go/deepseek-v4.1-flash"
OPENCODE_GO_PROVIDER = "opencode-go"


def _opencode_run_prefix() -> list[str]:
    # The message goes first: --file takes a list and would swallow it.
    # --pure skips the owner's external plugins, keeping unattended runs lean.
    return [
        _executable("opencode"), "run",
        "Follow the complete task brief in the attached file.",
        "--format", "json", "--pure",
    ]


def opencode_go_model(model: str | None) -> str:
    """Normalise an OpenCode Go model id, refusing any other provider."""
    value = str(model or "").strip()
    if not value or value == "default":
        return DEFAULT_OPENCODE_GO_MODEL
    if "/" not in value:
        return f"{OPENCODE_GO_PROVIDER}/{value}"
    if value.split("/", 1)[0] != OPENCODE_GO_PROVIDER:
        raise WorkerError(
            f"the opencode worker only uses OpenCode Go models; got {value!r}"
        )
    return value


def opencode_model(model: str | None) -> str:
    """Normalise an opencode model to a bare Ollama name, refusing cloud models."""
    value = str(model or "").strip()
    if not value or value == "default":
        return DEFAULT_OPENCODE_MODEL
    if value.startswith("ollama/"):
        return value.split("/", 1)[1]
    provider = value.split("/", 1)[0] if "/" in value else ""
    if provider and ":" not in provider and provider not in _LOCAL_NAMESPACES:
        raise WorkerError(
            f"opencode is limited to local Ollama models; {value!r} looks like a cloud model"
        )
    return value


# Ollama model namespaces that are local copies, not cloud providers.
_LOCAL_NAMESPACES = frozenset({"trading-hub", "reecdev"})


def write_opencode_config(model: str) -> Path:
    """Write the Cortex-owned opencode config that exposes one local model."""
    target = config.db_path().parent / "opencode" / "opencode.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    host = config.ollama_host().rstrip("/")
    document = {
        "$schema": "https://opencode.ai/config.json",
        # Local-only: no session sharing and no self-update from unattended runs.
        "share": "disabled",
        "autoupdate": False,
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama (local)",
                "options": {"baseURL": f"{host}/v1"},
                "models": {model: {"name": model}},
            }
        },
    }
    text = json.dumps(document, indent=2)
    if not target.exists() or target.read_text(encoding="utf-8") != text:
        target.write_text(text, encoding="utf-8")
    return target


def _executable(worker: str) -> str:
    if worker.startswith("opencode") and os.name == "nt":
        # npm installs a PowerShell/cmd shim; run the real binary instead so
        # no script host reinterprets the arguments.
        shim = shutil.which("opencode")
        if shim:
            real = Path(shim).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            if real.exists():
                return str(real)
    if worker.startswith("opencode"):
        return "opencode"
    return worker
