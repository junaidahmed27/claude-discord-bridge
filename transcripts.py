"""Read Claude Code and Codex session transcripts.

Two on-disk layouts are handled:

- Claude Code: one .jsonl per session under
    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl
  Events have type=="user"|"assistant"|..., role+content live under .message.

- OpenAI Codex CLI: one .jsonl per session under
    ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
  Events have type=="session_meta"|"response_item"|...; user/assistant turns
  are response_items where payload.type=="message" with role + content blocks
  of type input_text / output_text. payload.type=="reasoning" is the codex
  equivalent of claude's "thinking" block.

Public functions take a `kind` parameter ("claude" or "codex") and dispatch
to the right internal helpers via PARSERS.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

# Kept for backwards compatibility — old code referenced PROJECTS_ROOT directly.
PROJECTS_ROOT = CLAUDE_PROJECTS_ROOT


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
    latest_user_prompt: str = ""  # most recent user turn, useful for "what is this conversation currently about"

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


# ---------------------------------------------------------------------------
# Claude Code session parser
# ---------------------------------------------------------------------------

def _claude_list(limit: int, project_filter: Optional[str]) -> List[SessionInfo]:
    if not CLAUDE_PROJECTS_ROOT.is_dir():
        return []

    candidates: List[SessionInfo] = []
    for proj_dir in CLAUDE_PROJECTS_ROOT.iterdir():
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
    for s in head:
        s.message_count, s.first_user_prompt, s.latest_user_prompt = _claude_quick_summary(s.file)
    return head


def _claude_quick_summary(path: Path) -> Tuple[int, str, str]:
    """One forward pass over the .jsonl. Returns (msg_count, first_user, last_user)."""
    count = 0
    first_prompt = ""
    last_prompt = ""
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
                if t == "user":
                    text = _extract_user_text(ev)
                    if text:
                        if not first_prompt:
                            first_prompt = text
                        last_prompt = text
    except OSError:
        pass
    return count, first_prompt, last_prompt


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


def _claude_find(prefix: str) -> Optional[SessionInfo]:
    if not prefix:
        return None
    prefix_lower = prefix.lower()
    matches: List[SessionInfo] = []
    if not CLAUDE_PROJECTS_ROOT.is_dir():
        return None
    for proj_dir in CLAUDE_PROJECTS_ROOT.iterdir():
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
    info.message_count, info.first_user_prompt, info.latest_user_prompt = _claude_quick_summary(info.file)
    return info


def _claude_render(info: SessionInfo, *, include_thinking: bool = False) -> str:
    parts: List[str] = []
    parts.append(f"# Session `{info.session_id}` (Claude)")
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


# ---------------------------------------------------------------------------
# Codex session parser
# ---------------------------------------------------------------------------

# Codex rollout filenames look like rollout-2026-05-16T15-40-13-019e324d-b46a-72f0-a4aa-412da1787287.jsonl
# UUID has the standard 8-4-4-4-12 hex pattern.
_CODEX_UUID_RE = re.compile(r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def _codex_session_id_from_path(p: Path) -> Optional[str]:
    m = _CODEX_UUID_RE.search(p.name)
    return m.group("uuid") if m else None


def _codex_iter_files() -> Iterable[Path]:
    if not CODEX_SESSIONS_ROOT.is_dir():
        return []
    return CODEX_SESSIONS_ROOT.rglob("*.jsonl")


# Codex injects bookkeeping user messages wrapped in XML-ish tags like
# <environment_context>, <apps_instructions>, <skills_instructions>. These
# aren't real user prompts and shouldn't be shown.
_CODEX_INTERNAL_TAGS = (
    "<environment_context",
    "<apps_instructions",
    "<skills_instructions",
    "<permissions_instructions",
    "<permissions instructions",
    "<task-notification",
    "<system",
)


def _codex_is_internal_user_text(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(tag) for tag in _CODEX_INTERNAL_TAGS)


def _codex_extract_user_text(payload: Dict[str, Any]) -> str:
    """Return real user text, skipping codex's internal context-injection messages."""
    content = payload.get("content") or []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "input_text":
                text = (block.get("text") or "").strip()
                if text and not _codex_is_internal_user_text(text):
                    return text
    return ""


def _codex_extract_assistant_text(payload: Dict[str, Any]) -> str:
    content = payload.get("content") or []
    pieces: List[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("output_text", "text"):
                t = (block.get("text") or "").strip()
                if t:
                    pieces.append(t)
    return "\n\n".join(pieces)


def _codex_extract_session_meta(path: Path) -> Tuple[str, str]:
    """Read the first line of a codex rollout and pull (cwd, originator)."""
    cwd = ""
    originator = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "session_meta":
                    payload = ev.get("payload") or {}
                    cwd = payload.get("cwd") or ""
                    originator = payload.get("originator") or ""
                    break
    except OSError:
        pass
    return cwd, originator


def _codex_quick_summary(path: Path) -> Tuple[int, str, str]:
    count = 0
    first_prompt = ""
    last_prompt = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "response_item":
                    continue
                payload = ev.get("payload") or {}
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                count += 1
                if role == "user":
                    text = _codex_extract_user_text(payload)
                    if text:
                        if not first_prompt:
                            first_prompt = text
                        last_prompt = text
    except OSError:
        pass
    return count, first_prompt, last_prompt


def _codex_session_info(path: Path) -> Optional[SessionInfo]:
    sid = _codex_session_id_from_path(path)
    if not sid:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    cwd, _ = _codex_extract_session_meta(path)
    label = "/".join(cwd.rstrip("/").split("/")[-2:]) if cwd else "(unknown)"
    return SessionInfo(
        session_id=sid,
        file=path,
        project_dir=cwd or "(unknown)",
        project_label=label or "(unknown)",
        mtime=stat.st_mtime,
        size_bytes=stat.st_size,
    )


def _codex_list(limit: int, project_filter: Optional[str]) -> List[SessionInfo]:
    candidates: List[SessionInfo] = []
    for f in _codex_iter_files():
        info = _codex_session_info(f)
        if info is None:
            continue
        if project_filter and project_filter.lower() not in info.project_dir.lower():
            continue
        candidates.append(info)
    candidates.sort(key=lambda s: s.mtime, reverse=True)
    head = candidates[:limit]
    for s in head:
        s.message_count, s.first_user_prompt, s.latest_user_prompt = _codex_quick_summary(s.file)
    return head


def _codex_find(prefix: str) -> Optional[SessionInfo]:
    if not prefix:
        return None
    prefix_lower = prefix.lower()
    matches: List[SessionInfo] = []
    for f in _codex_iter_files():
        sid = _codex_session_id_from_path(f)
        if sid and sid.lower().startswith(prefix_lower):
            info = _codex_session_info(f)
            if info is not None:
                matches.append(info)
    if len(matches) != 1:
        return None
    info = matches[0]
    info.message_count, info.first_user_prompt, info.latest_user_prompt = _codex_quick_summary(info.file)
    return info


def _codex_render(info: SessionInfo, *, include_thinking: bool = False) -> str:
    parts: List[str] = []
    parts.append(f"# Session `{info.session_id}` (Codex)")
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
                rendered = _codex_render_event(ev, include_thinking=include_thinking)
                if rendered:
                    parts.append(rendered)
    except OSError as e:
        parts.append(f"_Could not read file: {e}_")

    return "\n".join(parts) + "\n"


def _codex_render_event(ev: Dict[str, Any], *, include_thinking: bool) -> str:
    if ev.get("type") != "response_item":
        return ""
    payload = ev.get("payload") or {}
    ts = ev.get("timestamp", "")
    ptype = payload.get("type")
    if ptype == "reasoning":
        if not include_thinking:
            return ""
        text = ""
        for block in payload.get("summary", []) or []:
            if isinstance(block, dict) and block.get("type") == "summary_text":
                text += (block.get("text") or "") + "\n"
        text = text.strip()
        return f"<details><summary>💭 reasoning · {ts}</summary>\n\n{text}\n\n</details>\n" if text else ""
    if ptype != "message":
        return ""
    role = payload.get("role")
    if role == "user":
        text = _codex_extract_user_text(payload)
        return f"## 🧑 User · {ts}\n\n{text}\n" if text else ""
    if role == "assistant":
        text = _codex_extract_assistant_text(payload)
        return f"## 🤖 Assistant · {ts}\n\n{text}\n" if text else ""
    return ""  # developer / system roles intentionally hidden


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

PARSERS: Dict[str, Dict[str, Callable]] = {
    "claude": {"list": _claude_list, "find": _claude_find, "render": _claude_render},
    "codex":  {"list": _codex_list,  "find": _codex_find,  "render": _codex_render},
}


def list_sessions(kind: str = "claude", limit: int = 20, project_filter: Optional[str] = None) -> List[SessionInfo]:
    parser = PARSERS.get(kind)
    if parser is None:
        raise ValueError(f"unknown session kind {kind!r}; expected one of {list(PARSERS)}")
    return parser["list"](limit, project_filter)


def find_session(kind: str, prefix: str) -> Optional[SessionInfo]:
    parser = PARSERS.get(kind)
    if parser is None:
        return None
    return parser["find"](prefix)


def render_transcript(kind: str, info: SessionInfo, *, include_thinking: bool = False) -> str:
    parser = PARSERS.get(kind)
    if parser is None:
        return f"_Unknown session kind: {kind}_"
    return parser["render"](info, include_thinking=include_thinking)


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
