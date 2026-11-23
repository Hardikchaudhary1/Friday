"""Language-model backends. Each one turns (system prompt, user prompt) into text.

- ClaudeCliBackend runs the Claude Code CLI in print mode, so it is billed to a
  Claude subscription rather than to the API.
- AnthropicApiBackend calls the Messages API with the official SDK.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class LLMError(RuntimeError):
    pass


def find_claude_cli(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    # The Claude desktop app bundles the CLI in a versioned folder.
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = glob.glob(os.path.join(local, "Claude-Data", "*", "claude-code", "*", "*", "claude.exe"))
    candidates += glob.glob(os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


class ClaudeCliBackend:
    name = "claude_cli"

    def __init__(self, path: str = "", model: str = "", timeout: int = 180):
        self.path = find_claude_cli(path)
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        return self.path is not None

    def complete(self, system: str, prompt: str) -> str:
        if not self.path:
            raise LLMError("Claude CLI not found")
        cmd = [
            self.path, "-p",
            "--output-format", "text",
            "--tools", "",                 # no file or shell access: text in, text out
            "--strict-mcp-config",         # don't load any MCP servers
            "--no-session-persistence",
            "--system-prompt", system,
        ]
        if self.model:
            cmd += ["--model", self.model]
        # Run from an empty temp folder so no project files or CLAUDE.md are picked up.
        with tempfile.TemporaryDirectory(prefix="friday-") as cwd:
            try:
                proc = subprocess.run(
                    cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                    timeout=self.timeout, cwd=cwd,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise LLMError(f"Claude CLI timed out after {self.timeout}s") from exc
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            raise LLMError(f"Claude CLI failed (exit {proc.returncode}): {err}")
        return out


    def stream(self, system: str, prompt: str):
        """Yields text as Claude writes it, so speech can start before the answer is finished."""
        import json

        if not self.path:
            raise LLMError("Claude CLI not found")
        cmd = [self.path, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--system-prompt", system]
        if self.model:
            cmd += ["--model", self.model]
        with tempfile.TemporaryDirectory(prefix="friday-") as cwd:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", cwd=cwd,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc.stdin.write(prompt)
            proc.stdin.close()
            got_text = False
            error = ""
            try:
                for line in proc.stdout:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("type") == "stream_event":
                        delta = msg["event"].get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            got_text = True
                            yield delta["text"]
                    elif msg.get("type") == "result" and msg.get("is_error"):
                        error = str(msg.get("result", ""))[:300]
            finally:
                try:
                    proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if not got_text:
                err = error or (proc.stderr.read() if proc.stderr else "")[:300]
                raise LLMError(f"Claude CLI failed (exit {proc.returncode}): {err.strip()}")


class AnthropicApiBackend:
    name = "anthropic_api"

    def __init__(self, model: str = "claude-opus-5-5", timeout: int = 180, low_effort: bool = False):
        self.model = model
        self.timeout = timeout
        self.effort = "low" if low_effort else "medium"

    def available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def complete(self, system: str, prompt: str) -> str:
        import anthropic

        client = anthropic.Anthropic(timeout=self.timeout)
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                output_config={"effort": self.effort},
                # If the main model declines, the API retries on a suitable fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Anthropic API unreachable: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic API rate limit hit") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise LLMError("The model declined to write the briefing")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMError("Anthropic API returned no text")
        return text

    def stream(self, system: str, prompt: str):
        yield self.complete(system, prompt)


def build_backends(cfg, purpose: str = "briefing") -> list:
    """purpose="chat" is for spoken questions: a faster model, because you're waiting for the answer."""
    timeout = int(cfg.get("llm.timeout_seconds", 180))
    if purpose == "chat":
        cli_model = cfg.get("llm.chat_cli_model", "haiku")
        api_model = cfg.get("llm.chat_api_model", "claude-haiku-5-5")
        timeout = min(timeout, 45)
    else:
        # Sonnet writes the briefing about twice as fast as the default model, at similar quality.
        cli_model = cfg.get("llm.claude_cli_model", "sonnet")
        api_model = cfg.get("llm.api_model", "claude-opus-5-5")
    backends = []
    for name in cfg.get("llm.order", ["claude_cli", "anthropic_api", "template"]):
        if name == "claude_cli":
            backends.append(ClaudeCliBackend(cfg.get("llm.claude_cli_path", ""), cli_model, timeout))
        elif name == "anthropic_api":
            backends.append(AnthropicApiBackend(api_model, timeout, low_effort=purpose == "chat"))
    return backends
