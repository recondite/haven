"""LLM access via shell-out to the `claude` CLI.

Reuses Claude Code's existing auth on the laptop — no separate API key needed.
Designed so a single import switch can later swap to the Anthropic Python SDK
if/when a workspace API key is provisioned.

Windows note: `claude` is typically installed by npm as a `.cmd` shim. We resolve
the full path via `shutil.which`, and run it through synchronous `subprocess.run`
inside `asyncio.to_thread` because `asyncio.create_subprocess_exec` does not handle
`.cmd`/`.bat` shims reliably on Windows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from haven import config

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.S)


def cli_path() -> str | None:
    """Resolve the path to the `claude` CLI shim (`claude.cmd` on Windows).

    Order:
      1. HAVEN_CLAUDE_CLI env var (explicit override)
      2. shutil.which on PATH
      3. Common Windows install locations
    """
    explicit = os.environ.get("HAVEN_CLAUDE_CLI")
    if explicit and os.path.isfile(explicit):
        return explicit

    found = shutil.which("claude")
    if found:
        return found

    candidates: list[str] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates += [
            os.path.join(appdata, "npm", "claude.cmd"),
            os.path.join(appdata, "npm", "claude.ps1"),
            os.path.join(appdata, "npm", "claude"),
        ]
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates += [
            os.path.join(localappdata, "Programs", "claude", "claude.cmd"),
            os.path.join(localappdata, "Programs", "claude", "claude.exe"),
            os.path.join(localappdata, "AnthropicClaude", "claude.exe"),
        ]
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates += [
            os.path.join(userprofile, ".local", "bin", "claude.cmd"),
            os.path.join(userprofile, ".local", "bin", "claude.exe"),
            os.path.join(userprofile, ".local", "bin", "claude"),
            os.path.join(userprofile, "AppData", "Roaming", "npm", "claude.cmd"),
        ]

    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _find_node_exe() -> str | None:
    """Locate node.exe on Windows. shutil.which sometimes misses it depending on
    how the Node.js installer set up the user environment, so probe known fixed
    locations as a fallback.
    """
    explicit = os.environ.get("HAVEN_NODE_EXE")
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which("node")
    if found:
        return found
    candidates = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ]
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(os.path.join(localappdata, "Programs", "nodejs", "node.exe"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _resolve_cli_js_from_shim(shim_path: str) -> str | None:
    """Parse the npm `claude.cmd` shim to extract the actual cli.js path it points at."""
    try:
        text = Path(shim_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # The shim line typically looks like:  "%_prog%"  "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*
    m = re.search(r'"%dp0%\\?([^"]+\.(?:js|mjs|cjs))"', text, re.IGNORECASE)
    if not m:
        # Fallback: any quoted .js path
        m = re.search(r'"([^"\r\n]+\.(?:js|mjs|cjs))"', text)
        if not m:
            return None
        candidate = m.group(1)
    else:
        dp0 = os.path.dirname(shim_path) + os.sep
        candidate = dp0 + m.group(1)
    candidate = os.path.normpath(candidate)
    return candidate if os.path.isfile(candidate) else None


def node_entry_path() -> tuple[str, str] | None:
    """Resolve a (node.exe, cli.js) pair so we can bypass the .cmd shim.

    The cmd.exe shim is fragile under uvicorn hot-reload signal handling on
    Windows — it traps Ctrl+C and aborts mid-call. Calling node directly with
    the underlying JS entry sidesteps the shim entirely.
    """
    if os.name != "nt":
        return None
    node_exe = _find_node_exe()
    if not node_exe:
        return None

    # First choice: parse the actual .cmd shim to find what JS it points to.
    shim = cli_path()
    if shim and shim.lower().endswith(".cmd"):
        js = _resolve_cli_js_from_shim(shim)
        if js:
            return node_exe, js

    # Fallback: probe well-known locations under %APPDATA%\npm.
    appdata = os.environ.get("APPDATA")
    if appdata:
        cc_dir = os.path.join(
            appdata, "npm", "node_modules", "@anthropic-ai", "claude-code"
        )
        for rel in (
            "cli.js",
            "src/cli.js",
            "dist/cli.js",
            "bin/cli.js",
            "index.js",
            "dist/index.js",
        ):
            p = os.path.join(cc_dir, rel.replace("/", os.sep))
            if os.path.isfile(p):
                return node_exe, p

    return None


def cli_available() -> bool:
    return cli_path() is not None or node_entry_path() is not None


def _extract_json(text: str) -> str:
    """Strip markdown fences and recover the JSON payload."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    if text.startswith("{") and text.rstrip().endswith("}"):
        return text
    # Last resort: find the outermost {...} block in the response.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _run_claude_sync(prompt: str, model: str, timeout: float) -> str:
    """Invoke the `claude` CLI synchronously. On Windows, prefer calling node directly
    with the underlying JS entry point to bypass cmd.exe's signal-trap behavior that
    kills in-flight subprocesses when uvicorn reloads.
    """
    import tempfile

    # Prefer node-direct on Windows; fall back to the .cmd shim or the binary on POSIX.
    node_pair = node_entry_path()
    if node_pair:
        node_exe, entry_js = node_pair
        cmd = [node_exe, entry_js, "--print", "--model", model]
    else:
        path = cli_path()
        if path is None:
            raise RuntimeError("`claude` CLI not found on PATH — install Claude Code first.")
        is_windows_shim = os.name == "nt" and path.lower().endswith((".cmd", ".bat"))
        if is_windows_shim:
            cmd = ["cmd", "/c", path, "--print", "--model", model]
        else:
            cmd = [path, "--print", "--model", model]

    # Windows: detach the subprocess from our console's signal group so that
    # uvicorn hot-reloads (which send SIGINT/SIGBREAK to the parent's console)
    # don't kill in-flight LLM calls. CREATE_NO_WINDOW hides any spawned cmd window.
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    # Write prompt to a temp file and pipe it via stdin redirection. This is more
    # reliable than subprocess `input=` for Windows .cmd shims, which sometimes
    # close their stdin before all bytes are written.
    fd, tmp = tempfile.mkstemp(prefix="haven-prompt-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        with open(tmp, "rb") as stdin_file:
            result = subprocess.run(
                cmd,
                stdin=stdin_file,
                capture_output=True,
                timeout=timeout,
                creationflags=creationflags,
            )
    except FileNotFoundError as e:
        raise RuntimeError(f"claude CLI not executable: {e}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI timed out after {timeout}s")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0:
        msg = (
            f"claude exited {result.returncode}\n"
            f"  stderr={stderr.strip()[:600] or '(empty)'}\n"
            f"  stdout={stdout.strip()[:600] or '(empty)'}"
        )
        raise RuntimeError(msg)
    return stdout


async def claude_call(
    prompt: str,
    model: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Run `claude --print --model X` with `prompt` on stdin, return stdout."""
    return await asyncio.to_thread(
        _run_claude_sync,
        prompt,
        model or config.LLM_MODEL,
        timeout,
    )


async def claude_json(
    prompt: str,
    model: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """Call `claude` and parse the response as JSON. Strips markdown fences."""
    raw = await claude_call(prompt, model, timeout)
    cleaned = _extract_json(raw)
    return json.loads(cleaned)
