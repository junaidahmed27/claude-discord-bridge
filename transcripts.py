"""Read Claude Code session transcripts from ~/.claude/projects.

A session is one .jsonl file under
    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl
where <encoded-cwd> is the absolute working directory with '/' → '-'.

Each line is a JSON event with a "type" field. The interesting events for
rendering a human-readable transcript are:

- type=="user":      a user prompt OR a tool_result (when content is a list of
                     blocks instead of a bare string)
- type=="assistant": assistant turn — content is a list of blocks
                     (text / thinking / tool_use)
- type=="system":    runtime metadata (skipped)
- type=="last-prompt", "queue-operation", "attachment": orchestration noise
                     (skipped)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECTS_ROOT = Path.home() / ".claude" / "projects"


@dataclass
class SessionInfo:
    session_id: str
    file: Path
    project_dir: str       # decoded back to /path/with/slashes
    project_label: str     # last 2 components for display
    mtime: float
    size_bytes: int
    message_count: int = 0
    first_user_prompt: str = ""

    @property
    def short_id(self) -> str:
        return self.session_id.split("-", 1)[0]


def _decode_project_dir(encoded: str) -> str:
    """Reverse the '/' → '-' encoding done by Claude Code.

    There's ambiguity (real '-' vs encoded '/'), so callers should treat the
    result as best-effort for display, not as a path to open.
    """
    if encoded.startswith("-"):
        return "/" + encoded[1:].replace("-", "/")
    return encoded.replace("-", "/")


def list_sessions(limit: int = 20, project_filter: Optional[str] = None) -> List[SessionInfo]:
    """Return up to `limit` sessions sorted by most-recent file activity.

    project_filter is matched as a case-insensitive substring against the
    decoded project directory.
    """
    if not PROJECTS_ROOT.is_dir():
        return []

    candidates: List[SessionInfo] = []
    for proj_dir in PROJECTS_ROOT.iterdir():
        if not proj_dir.is_dir():
            continue
        decoded = _decode_project_dir(proj_dir.name)
        if project_filter and project_filter.lower() not in decoded.lower():
            continue
        label = "/".join(decoded.rstrip("/").split("/")[-2:]) or decoded
        for f in proj_dir.iterdir():
            if not f.is_file() or f.suffix != ".jsonl":
                continue
            try:
                stat = f.stat()
            except OSError:
                continue
            candidates.append(
                SessionInfo(
                    session_id=f.stem,
                    file=f,
                    project_dir=decoded,
                    project_label=label,
                    mtime=stat.st_mtime,
                    size_bytes=stat.st_size,
                )
            )

    candidates.sort(key=lambda s: s.mtime, reverse=True)
    head = candidates[:limit]

    # Cheap preview: stream until we have a message count + first user prompt.
    for s in head:
        s.message_count, s.first_user_prompt = _quick_summary(s.file)
    return head


def _quick_summary(path: Path) -> Tuple[int, str]:
    """Count user/assistant events and grab the first user prompt as a preview."""
    count = 0
    first_prompt = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t not in ("user", "assistant"):
                    continue
                count += 1
                if not first_prompt and t == "user":
                    text = _extract_user_text(ev)
                    if text:
                        first_prompt = text
    except OSError:
        pass
    return count, first_prompt


def _extract_user_text(ev: Dict[str, Any]) -> str:
    """Pull a plain-text prompt out of a user event, ignoring tool_result rows."""
    msg = ev.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        # Tool results have content as list-of-blocks; only count blocks that are
        # actual text (not tool_result).
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return (block.get("text") or "").strip()
    return ""


def find_session(prefix: str) -> Optional[SessionInfo]:
    """Resolve a (case-insensitive) prefix to a single SessionInfo.

    Returns None if there's no match or if multiple sessions match — the caller
    decides how to surface that to the user.
    """
    if not prefix:
        return None
    prefix_lower = prefix.lower()
    matches: List[SessionInfo] = []
    if not PROJECTS_ROOT.is_dir():
        return None
    for proj_dir in PROJECTS_ROOT.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.iterdir():
            if f.suffix != ".jsonl":
                continue
            if f.stem.lower().startswith(prefix_lower):
                decoded = _decode_project_dir(proj_dir.name)
                label = "/".join(decoded.rstrip("/").split("/")[-2:]) or decoded
                try:
                    stat = f.stat()
                except OSError:
                    continue
                matches.append(SessionInfo(
                    session_id=f.stem,
                    file=f,
                    project_dir=decoded,
                    project_label=label,
                    mtime=stat.st_mtime,
                    size_bytes=stat.st_size,
                ))
    if len(matches) != 1:
        return None
    info = matches[0]
    info.message_count, info.first_user_prompt = _quick_summary(info.file)
    return info


def render_transcript(info: SessionInfo, *, include_thinking: bool = False) -> str:
    """Render a .jsonl session into a readable Markdown transcript."""
    parts: List[str] = []
    parts.append(f"# Session `{info.session_id}`")
    parts.append("")
    parts.append(f"- **Project:** `{info.project_dir}`")
    parts.append(f"- **File:** `{info.file}`")
    parts.append(f"- **Last activity:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info.mtime))}")
    parts.append(f"- **Size:** {info.size_bytes:,} bytes")
    parts.append("")
    parts.append("---")
    parts.append("")

    try:
        with info.file.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                rendered = _render_event(ev, include_thinking=include_thinking)
                if rendered:
                    parts.append(rendered)
    except OSError as e:
        parts.append(f"_Could not read file: {e}_")

    return "\n".join(parts) + "\n"


def _render_event(ev: Dict[str, Any], *, include_thinking: bool) -> str:
    t = ev.get("type")
    ts = ev.get("timestamp", "")
    if t == "user":
        text = _extract_user_text(ev)
        if text:
            return f"## 🧑 User · {ts}\n\n{text}\n"
        # Tool results land here too; render them compactly.
        return _render_user_tool_results(ev)
    if t == "assistant":
        return _render_assistant(ev, ts, include_thinking=include_thinking)
    return ""  # skip system, queue-operation, last-prompt, attachment, etc.


def _render_user_tool_results(ev: Dict[str, Any]) -> str:
    msg = ev.get("message") or {}
    content = msg.get("content") or []
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tr_id = block.get("tool_use_id", "?")
            tr_content = block.get("content")
            if isinstance(tr_content, list):
                texts = [b.get("text", "") for b in tr_content if isinstance(b, dict) and b.get("type") == "text"]
                tr_content = "\n".join(texts)
            tr_content = str(tr_content or "")
            if len(tr_content) > 2000:
                tr_content = tr_content[:2000] + f"\n…(truncated, full length {len(tr_content)} chars)"
            chunks.append(f"### 🛠️ Tool result `{tr_id[:8]}`\n\n```\n{tr_content}\n```\n")
    return "\n".join(chunks)


def _render_assistant(ev: Dict[str, Any], ts: str, *, include_thinking: bool) -> str:
    msg = ev.get("message") or {}
    content = msg.get("content") or []
    model = msg.get("model", "")
    if not isinstance(content, list):
        return ""
    parts: List[str] = [f"## 🤖 Assistant ({model}) · {ts}\n"]
    has_anything = False
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text + "\n")
                has_anything = True
        elif bt == "thinking":
            if include_thinking:
                text = (block.get("thinking") or "").strip()
                if text:
                    parts.append(f"<details><summary>💭 thinking</summary>\n\n{text}\n\n</details>\n")
                    has_anything = True
        elif bt == "tool_use":
            name = block.get("name", "?")
            tu_id = block.get("id", "?")
            tu_input = block.get("input")
            try:
                rendered_input = json.dumps(tu_input, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                rendered_input = str(tu_input)
            if len(rendered_input) > 1500:
                rendered_input = rendered_input[:1500] + f"\n…(truncated)"
            parts.append(
                f"### 🔧 Tool call `{name}` ({tu_id[:8]})\n\n```json\n{rendered_input}\n```\n"
            )
            has_anything = True
    return "\n".join(parts) if has_anything else ""
