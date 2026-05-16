"""Discord → local Claude Code bridge.

Listens for messages in allowlisted channels from allowlisted users and spawns
a non-interactive `claude -p "<message>"` session on this workstation. The
session's stdout becomes the bot's reply. Long output is attached as a file.

Security model: only users whose Discord ID is in `allowed_user_ids` AND
messages posted in a channel whose ID is in `allowed_channel_ids` can trigger a
session. Anything else is silently ignored — no DM fallback, no bypass.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import discord
import psutil

import transcripts as transcripts_mod  # local module: ~/.claude-discord/transcripts.py

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DB_PATH = Path(__file__).resolve().parent / "history.db"
DISCORD_MSG_LIMIT = 2000  # Discord's hard cap per message.
EDIT_THROTTLE_S = 2.0     # Don't edit a Discord message more than once per ~2s.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)-18s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("claude-discord")


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        logger.error("Config not found at %s. Copy config.example.json → config.json and fill it in.", CONFIG_PATH)
        sys.exit(2)
    with CONFIG_PATH.open() as f:
        raw = json.load(f)
    # Strip the _comment_* documentation keys.
    cfg = {k: v for k, v in raw.items() if not k.startswith("_comment_")}
    required = ("bot_token", "allowed_channel_ids", "allowed_user_ids", "working_directory", "claude_path")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        logger.error("Missing required config keys: %s", ", ".join(missing))
        sys.exit(2)
    # Normalize ID lists to sets of strings — Discord IDs are 64-bit ints but
    # round-tripping through JSON loses precision unless treated as strings.
    cfg["allowed_channel_ids"] = {str(x) for x in cfg["allowed_channel_ids"]}
    cfg["allowed_user_ids"] = {str(x) for x in cfg["allowed_user_ids"]}
    cfg.setdefault("session_timeout_s", 1800)
    cfg.setdefault("max_concurrent_sessions", 1)
    cfg.setdefault("trigger_prefix", "")
    if not Path(cfg["claude_path"]).is_file():
        logger.warning("claude binary not found at %s — invocations will fail.", cfg["claude_path"])
    if not Path(cfg["working_directory"]).is_dir():
        logger.warning("working_directory %s does not exist — claude will refuse to run.", cfg["working_directory"])
    return cfg


CONFIG = load_config()
SEMAPHORE = asyncio.Semaphore(int(CONFIG["max_concurrent_sessions"]))
# Map message_id → asyncio.Task so we can support cancel via reaction.
RUNNING: Dict[int, asyncio.Task] = {}
# Map job_id → (author_display, prompt_preview, started_at) for /status reporting.
ACTIVE_JOBS: Dict[str, tuple[str, str, float]] = {}
# Map job_id → live asyncio.subprocess.Process so /cancel can kill it.
ACTIVE_PROCS: Dict[str, asyncio.subprocess.Process] = {}
# Upper bound on rows returned by /history. The DB itself is unbounded.
HISTORY_MAX = 25


# ---------------------------------------------------------------------------
# Persistent history (SQLite)
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    """Open a fresh per-call connection. SQLite handles concurrent readers/writers
    fine for our tiny write rate; per-call connections sidestep cross-thread
    issues without needing a lock."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    conn = _db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                job_id        TEXT PRIMARY KEY,
                author        TEXT NOT NULL,
                preview       TEXT NOT NULL,
                returncode    INTEGER NOT NULL,
                elapsed       REAL NOT NULL,
                started_at    REAL NOT NULL,
                finished_at   REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_finished ON history(finished_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def db_insert_sync(entry: Dict[str, Any]) -> None:
    conn = _db()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO history
                (job_id, author, preview, returncode, elapsed, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["job_id"],
                entry["author"],
                entry["preview"],
                entry["returncode"],
                entry["elapsed"],
                entry["started_at"],
                entry["finished_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def db_recent_sync(n: int) -> List[Dict[str, Any]]:
    conn = _db()
    try:
        cur = conn.execute(
            """
            SELECT job_id, author, preview, returncode, elapsed, started_at, finished_at
            FROM history
            ORDER BY finished_at DESC
            LIMIT ?
            """,
            (n,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def db_count_sync() -> int:
    conn = _db()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM history")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


db_init()


def is_allowed(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    if str(message.channel.id) not in CONFIG["allowed_channel_ids"]:
        return False
    if str(message.author.id) not in CONFIG["allowed_user_ids"]:
        logger.warning("Unallowlisted user %s (%s) tried to trigger.", message.author, message.author.id)
        return False
    return True


def extract_prompt(message: discord.Message) -> str | None:
    text = (message.content or "").strip()
    prefix = CONFIG["trigger_prefix"]
    if prefix:
        if not text.startswith(prefix):
            return None
        text = text[len(prefix):].strip()
    return text or None


async def run_claude(
    prompt: str,
    job_id: str,
    *,
    resume_id: Optional[str] = None,
    cwd_override: Optional[str] = None,
) -> tuple[int, str, str]:
    """Run `claude -p "<prompt>"` as a subprocess. Returns (returncode, stdout, stderr).

    When resume_id is provided, the invocation becomes
        claude -p --resume <resume_id> "<prompt>"
    and cwd is set to cwd_override so the resume can find the session file under
    ~/.claude/projects/<encoded-cwd>/<id>.jsonl.
    """
    cmd = [CONFIG["claude_path"], "-p"]
    if resume_id:
        cmd += ["--resume", resume_id]
    cmd.append(prompt)
    cwd = cwd_override or CONFIG["working_directory"]
    env = os.environ.copy()
    # Force a stable working dir and unbuffered output so we get progressive lines.
    env["PYTHONUNBUFFERED"] = "1"

    logger.info(
        "[%s] spawn: %s%s (cwd=%s)",
        job_id, " ".join(cmd[:2]),
        f" --resume {resume_id[:8]}" if resume_id else "",
        cwd,
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ACTIVE_PROCS[job_id] = proc
    try:
        try:
            timeout = CONFIG["session_timeout_s"] or None
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[%s] timeout after %ss — killing subprocess", job_id, CONFIG["session_timeout_s"])
            proc.kill()
            await proc.wait()
            return -1, "", f"Session exceeded timeout of {CONFIG['session_timeout_s']}s."
        # returncode == -SIGKILL (negative on Unix when killed by signal) → cancelled by user.
        if proc.returncode is not None and proc.returncode < 0:
            return proc.returncode, (
                stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            ), f"Process terminated by signal {abs(proc.returncode)} (likely cancelled)."
        return proc.returncode or 0, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")
    finally:
        ACTIVE_PROCS.pop(job_id, None)


async def reply_with_output(channel: discord.abc.Messageable, job_id: str, prompt: str, returncode: int, stdout: str, stderr: str) -> None:
    """Send the result back to Discord. Long output → file attachment."""
    body = stdout.strip() or "(no stdout)"
    status_emoji = "✅" if returncode == 0 else "❌"

    header = f"{status_emoji} **Job `{job_id}`** (exit {returncode})\n> {prompt[:200]}"
    files = []
    if stderr.strip():
        files.append(discord.File(io.BytesIO(stderr.encode("utf-8")), filename=f"{job_id}.stderr.log"))

    # If the body + header fits in one message, send inline. Otherwise attach.
    inline = f"{header}\n```\n{body}\n```"
    if len(inline) <= DISCORD_MSG_LIMIT and "\n" not in body[:DISCORD_MSG_LIMIT]:
        await channel.send(inline, files=files)
        return

    files.insert(0, discord.File(io.BytesIO(body.encode("utf-8")), filename=f"{job_id}.stdout.md"))
    await channel.send(header + "\n_(output attached)_", files=files)


def _format_elapsed(secs: float) -> str:
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def _parse_etime_to_seconds(etime: str) -> int:
    """Parse `ps` ELAPSED column ([[dd-]hh:]mm:ss) into seconds."""
    days, rest = (0, etime)
    if "-" in etime:
        d, rest = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in rest.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        h, m, s = 0, 0, parts[0]
    return days * 86400 + h * 3600 + m * 60 + s


def _lsof_cwd(pid: int) -> str:
    """Best-effort cwd lookup via lsof; returns '?' if unavailable.

    Hardcodes /usr/sbin/lsof because LaunchAgents on macOS get a minimal PATH
    that excludes /usr/sbin, so `subprocess.run(["lsof", ...])` fails silently.
    """
    import shutil
    import subprocess
    lsof = "/usr/sbin/lsof"
    if not Path(lsof).exists():
        lsof = shutil.which("lsof") or ""
    if not lsof:
        return "?"
    try:
        out = subprocess.run(
            [lsof, "-a", "-d", "cwd", "-Fn", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        # lsof -F prints field-prefixed lines; cwd path lines start with "n".
        for line in out.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except Exception as e:
        logger.debug("lsof failed for pid %s: %s", pid, e)
    return "?"


def _encode_cwd(cwd: str) -> str:
    """Mirror Claude Code's '/' → '-' encoding for ~/.claude/projects/<dir>."""
    if not cwd or cwd == "?":
        return ""
    if cwd.startswith("/"):
        return "-" + cwd[1:].replace("/", "-")
    return cwd.replace("/", "-")


def _project_recent_mtime(cwd: str) -> Optional[float]:
    """Most recent .jsonl mtime under ~/.claude/projects/<encoded-cwd>/.

    Used as the 'is something happening in this project' signal. Returns None
    if the project dir doesn't exist or has no transcripts.
    """
    encoded = _encode_cwd(cwd)
    if not encoded:
        return None
    proj = Path.home() / ".claude" / "projects" / encoded
    if not proj.is_dir():
        return None
    best: Optional[float] = None
    try:
        for f in proj.iterdir():
            if f.suffix != ".jsonl":
                continue
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if best is None or m > best:
                best = m
    except OSError:
        return None
    return best


def find_workstation_claude_sessions(exclude_pids: Set[int]) -> List[Dict[str, Any]]:
    """Find every interactive `claude` CLI session on this workstation.

    Uses `ps` directly (psutil's iterator misses some long-lived TTY processes
    on macOS). Filters to processes whose command starts with the bare word
    `claude` and that have a real TTY. Bot-spawned PIDs in exclude_pids are
    dropped so they aren't double-counted with the Bot-spawned section.

    Activity classification per row:
      busy   — project's jsonl was written < 5 s ago (model is producing)
      recent — project's jsonl was written < 60 s ago (just finished a turn)
      idle   — older, the claude is sitting at its prompt
      unknown — no transcript found at all
    """
    import subprocess
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,etime=,tty=,%cpu=,ucomm=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        logger.warning("`ps` invocation failed: %s", e)
        return []

    rows: List[Dict[str, Any]] = []
    now = time.time()
    # Cache project-dir mtimes so multiple PIDs in the same cwd share one scan.
    cwd_mtime_cache: Dict[str, Optional[float]] = {}

    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        pid_s, etime, tty, cpu_s, ucomm, command = parts
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid in exclude_pids:
            continue
        if ucomm not in ("claude", "claude.exe"):
            continue
        if not tty or not tty.startswith("ttys"):
            continue
        if command.startswith("/Applications/"):
            continue
        try:
            elapsed_s = _parse_etime_to_seconds(etime)
        except Exception:
            elapsed_s = 0
        try:
            cpu_pct = float(cpu_s)
        except ValueError:
            cpu_pct = 0.0

        cwd = _lsof_cwd(pid)
        if cwd not in cwd_mtime_cache:
            cwd_mtime_cache[cwd] = _project_recent_mtime(cwd)
        recent_mtime = cwd_mtime_cache[cwd]
        if recent_mtime is None:
            activity_age: Optional[float] = None
            status = "unknown"
        else:
            activity_age = now - recent_mtime
            # CPU-active is a strong "this PID is doing something" signal that
            # works even when several claudes share a cwd.
            if cpu_pct > 1.0 or activity_age < 5:
                status = "busy"
            elif activity_age < 60:
                status = "recent"
            else:
                status = "idle"

        rows.append({
            "pid": pid,
            "tty": tty,
            "started": now - elapsed_s,
            "elapsed_s": elapsed_s,
            "cpu_pct": cpu_pct,
            "cwd": cwd,
            "command": command,
            "status": status,
            "activity_age_s": activity_age,
        })
    rows.sort(key=lambda r: (r["status"] != "busy", r["status"] != "recent", -r["started"]))
    return rows


async def handle_status(message: discord.Message) -> None:
    """Respond to '<prefix>status' with both bot-spawned and workstation-wide sessions."""
    max_slots = int(CONFIG["max_concurrent_sessions"])
    in_use = len(ACTIVE_JOBS)
    free = max_slots - in_use
    now = time.time()

    parts: List[str] = []

    # Section 1: bot-spawned jobs.
    if ACTIVE_JOBS:
        bot_lines = [
            f"  • `{jid}` — _{author}_ · `{preview}` · {_format_elapsed(now - started)}"
            for jid, (author, preview, started) in sorted(ACTIVE_JOBS.items(), key=lambda kv: kv[1][2])
        ]
        parts.append(
            f"🤖 **Bot-spawned: {in_use}** ({in_use}/{max_slots} slot(s) used)\n"
            + "\n".join(bot_lines)
        )
    else:
        parts.append(f"🤖 **Bot-spawned: 0** ({free}/{max_slots} slots free)")

    # Section 2: every other claude CLI on the workstation.
    exclude = {proc.pid for proc in ACTIVE_PROCS.values() if proc.pid is not None}
    try:
        others = await asyncio.to_thread(find_workstation_claude_sessions, exclude)
    except Exception as e:
        logger.exception("workstation scan failed")
        others = []
        parts.append(f"🖥️ **Other sessions: scan failed** — `{type(e).__name__}: {e}`")

    if others:
        # Indicator legend: 🟢 busy now · 🟡 just finished · 💤 idle waiting · ❓ unknown
        status_icon = {"busy": "🟢", "recent": "🟡", "idle": "💤", "unknown": "❓"}
        running_count = sum(1 for o in others if o["status"] == "busy")
        idle_count = sum(1 for o in others if o["status"] == "idle")
        other_lines = []
        for o in others:
            cwd = o["cwd"]
            if len(cwd) > 50:
                cwd = "…" + cwd[-48:]
            age = o["activity_age_s"]
            if age is None:
                activity = "no transcript"
            elif age < 60:
                activity = f"{int(age)}s ago"
            elif age < 3600:
                activity = f"{int(age // 60)}m ago"
            elif age < 86400:
                activity = f"{int(age // 3600)}h ago"
            else:
                activity = f"{int(age // 86400)}d ago"
            other_lines.append(
                f"  {status_icon[o['status']]} PID `{o['pid']}` · tty `{o['tty']}` · "
                f"CPU `{o['cpu_pct']:.1f}%` · last activity `{activity}` · cwd `{cwd}`"
            )
        parts.append(
            f"🖥️ **Other workstation sessions: {len(others)}** "
            f"({running_count} running, {idle_count} idle)\n"
            + "\n".join(other_lines)
            + "\n_🟢 running now · 🟡 just finished · 💤 idle · ❓ unknown_"
        )
    else:
        parts.append("🖥️ **Other workstation sessions: 0**")

    body = "\n\n".join(parts)
    if len(body) > DISCORD_MSG_LIMIT:
        body = body[: DISCORD_MSG_LIMIT - 20] + "\n…(truncated)"
    await message.reply(body, mention_author=False)


async def handle_cancel(message: discord.Message, arg: str) -> None:
    """Cancel one (or all) active sessions.

    Forms:
      <prefix>cancel              → if exactly one job is active, kill it
      <prefix>cancel <job_id>     → kill that specific job
      <prefix>cancel all          → kill every active job
    """
    arg = arg.strip().lower()

    if not ACTIVE_PROCS:
        await message.reply("💤 No active sessions to cancel.", mention_author=False)
        return

    targets: list[str]
    if arg in ("", "all"):
        if arg == "" and len(ACTIVE_PROCS) > 1:
            ids = ", ".join(f"`{j}`" for j in ACTIVE_PROCS)
            await message.reply(
                f"More than one session running ({ids}). Specify which: `{CONFIG['trigger_prefix']}cancel <job_id>` or `{CONFIG['trigger_prefix']}cancel all`.",
                mention_author=False,
            )
            return
        targets = list(ACTIVE_PROCS.keys())
    else:
        if arg not in ACTIVE_PROCS:
            await message.reply(f"❓ No active session with id `{arg}`.", mention_author=False)
            return
        targets = [arg]

    killed: list[str] = []
    for jid in targets:
        proc = ACTIVE_PROCS.get(jid)
        if proc is None or proc.returncode is not None:
            continue
        try:
            proc.kill()
            killed.append(jid)
            logger.info("[%s] cancelled by %s via Discord", jid, message.author)
        except ProcessLookupError:
            logger.warning("[%s] proc already gone", jid)

    if not killed:
        await message.reply("⚠️ Nothing to kill — sessions already exited.", mention_author=False)
        return
    body = "🛑 Cancelled: " + ", ".join(f"`{j}`" for j in killed)
    await message.reply(body, mention_author=False)


async def handle_history(message: discord.Message, arg: str) -> None:
    """Show the most recent N completed jobs from the SQLite history."""
    try:
        n = int(arg) if arg.strip() else 10
    except ValueError:
        n = 10
    n = max(1, min(n, HISTORY_MAX))

    rows = await asyncio.to_thread(db_recent_sync, n)
    if not rows:
        await message.reply("📭 No completed sessions in history yet.", mention_author=False)
        return

    total = await asyncio.to_thread(db_count_sync)
    now = time.time()
    lines = []
    for entry in rows:
        rc = int(entry["returncode"])
        icon = "✅" if rc == 0 else ("🛑" if rc < 0 else "❌")
        ago = int(now - entry["finished_at"])
        if ago < 60:
            ago_s = f"{ago}s ago"
        elif ago < 3600:
            ago_s = f"{ago // 60}m ago"
        elif ago < 86400:
            ago_s = f"{ago // 3600}h ago"
        else:
            ago_s = f"{ago // 86400}d ago"
        lines.append(
            f"{icon} `{entry['job_id']}` · {entry['elapsed']:.1f}s · {ago_s} · _{entry['author']}_ · `{entry['preview']}`"
        )
    body = (
        f"📜 Last **{len(rows)}** of {total} session(s) (newest first):\n"
        + "\n".join(lines)
    )
    if len(body) > DISCORD_MSG_LIMIT:
        body = body[: DISCORD_MSG_LIMIT - 20] + "\n…(truncated)"
    await message.reply(body, mention_author=False)


async def handle_sessions(message: discord.Message, arg: str) -> None:
    """List Claude Code sessions stored on this workstation."""
    parts = arg.strip().split()
    limit = 10
    proj_filter: Optional[str] = None
    for p in parts:
        if p.isdigit():
            limit = max(1, min(int(p), 30))
        else:
            proj_filter = p
    sessions = await asyncio.to_thread(transcripts_mod.list_sessions, limit, proj_filter)
    if not sessions:
        await message.reply("📭 No sessions found.", mention_author=False)
        return
    now = time.time()
    lines: List[str] = []
    for s in sessions:
        ago = _format_elapsed(now - s.mtime)
        # Show the most recent user prompt — what the session is currently about,
        # not how it started.
        preview = (s.latest_user_prompt or s.first_user_prompt).replace("\n", " ")
        if len(preview) > 70:
            preview = preview[:70] + "…"
        lines.append(
            f"`{s.short_id}` · {s.project_label} · {s.message_count} msgs · {ago} ago · _{preview or '(no user prompt)'}_"
        )
    body = (
        f"📚 **{len(sessions)}** session(s), newest activity first (use `{CONFIG['trigger_prefix']}transcript <id>` to fetch):\n"
        + "\n".join(lines)
    )
    if len(body) > DISCORD_MSG_LIMIT:
        body = body[: DISCORD_MSG_LIMIT - 20] + "\n…(truncated)"
    await message.reply(body, mention_author=False)


async def handle_transcript(message: discord.Message, arg: str) -> None:
    """Render one session as a markdown file and post it as an attachment."""
    parts = arg.strip().split()
    if not parts:
        await message.reply(
            f"Usage: `{CONFIG['trigger_prefix']}transcript <id_prefix> [thinking]`\n"
            f"Run `{CONFIG['trigger_prefix']}sessions` to see IDs.",
            mention_author=False,
        )
        return
    prefix = parts[0]
    include_thinking = any(p.lower() in ("thinking", "+thinking", "full") for p in parts[1:])

    info = await asyncio.to_thread(transcripts_mod.find_session, prefix)
    if info is None:
        await message.reply(
            f"❓ No session id starts with `{prefix}`, or the prefix matches more than one. "
            f"Use a longer prefix or run `{CONFIG['trigger_prefix']}sessions`.",
            mention_author=False,
        )
        return

    rendered = await asyncio.to_thread(
        transcripts_mod.render_transcript, info, include_thinking=include_thinking
    )
    # Header preview goes inline; full transcript becomes a file attachment.
    header = (
        f"📜 Session `{info.short_id}…` · _{info.project_label}_ · {info.message_count} msgs · "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(info.mtime))}"
    )
    if include_thinking:
        header += " · _includes thinking_"
    if info.first_user_prompt:
        preview = info.first_user_prompt.replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        header += f"\n> {preview}"

    file = discord.File(io.BytesIO(rendered.encode("utf-8")), filename=f"{info.session_id}.md")
    await message.reply(header, file=file, mention_author=False)


async def _execute_and_reply(
    message: discord.Message,
    prompt: str,
    *,
    resume_id: Optional[str] = None,
    cwd_override: Optional[str] = None,
    preview_override: Optional[str] = None,
) -> None:
    """Acquire a slot, run claude (optionally resuming a session), post the reply.

    Shared between fresh sessions (handle_message) and resumed ones
    (handle_resume) so the ack-edit pipeline + history-write logic doesn't drift.
    """
    job_id = uuid.uuid4().hex[:8]
    preview = preview_override or prompt[:200]
    logger.info("[%s] %s: %r", job_id, message.author, preview)

    label_extra = f" (resume {resume_id[:8]})" if resume_id else ""
    ack = await message.reply(
        f"🤖 starting `{job_id}`{label_extra} — _waiting for a free slot..._",
        mention_author=False,
    )
    started = time.time()

    async with SEMAPHORE:
        await ack.edit(content=f"🤖 `{job_id}`{label_extra} running... (started <t:{int(started)}:R>)")
        ACTIVE_JOBS[job_id] = (str(message.author), preview[:80], started)
        try:
            returncode, stdout, stderr = await run_claude(
                prompt, job_id, resume_id=resume_id, cwd_override=cwd_override,
            )
        except FileNotFoundError as e:
            await ack.edit(content=f"❌ `{job_id}` failed: claude binary not found ({e}). Fix `claude_path` in config.")
            logger.exception("[%s] claude binary missing", job_id)
            return
        except Exception as e:
            await ack.edit(content=f"❌ `{job_id}` errored: `{type(e).__name__}: {e}`")
            logger.exception("[%s] unexpected error", job_id)
            return
        finally:
            ACTIVE_JOBS.pop(job_id, None)

    elapsed = time.time() - started
    finished_at = time.time()
    await ack.edit(
        content=f"🤖 `{job_id}`{label_extra} finished in {elapsed:.1f}s — see reply below.",
    )
    await reply_with_output(message.channel, job_id, preview, returncode, stdout, stderr)
    logger.info("[%s] done in %.1fs | rc=%d | stdout=%d chars", job_id, elapsed, returncode, len(stdout))
    try:
        await asyncio.to_thread(db_insert_sync, {
            "job_id": job_id,
            "author": str(message.author),
            "preview": preview[:80].replace("\n", " "),
            "returncode": returncode,
            "elapsed": elapsed,
            "started_at": started,
            "finished_at": finished_at,
        })
    except Exception:
        logger.exception("[%s] failed to write history row", job_id)


async def handle_resume(message: discord.Message, arg: str) -> None:
    """Continue an existing Claude session with one follow-up turn.

    Form: <prefix>resume <id_prefix> <message>
    Looks up the session, then runs `claude -p --resume <id> "<message>"` in
    the session's original cwd so claude can find the on-disk transcript.
    """
    parts = arg.strip().split(None, 1)
    if len(parts) < 2:
        await message.reply(
            f"Usage: `{CONFIG['trigger_prefix']}resume <id_prefix> <message>`\n"
            f"Run `{CONFIG['trigger_prefix']}sessions` to find session IDs.",
            mention_author=False,
        )
        return
    prefix, followup = parts
    info = await asyncio.to_thread(transcripts_mod.find_session, prefix)
    if info is None:
        await message.reply(
            f"❓ No session id starts with `{prefix}`, or the prefix matches more than one. "
            f"Use a longer prefix or run `{CONFIG['trigger_prefix']}sessions`.",
            mention_author=False,
        )
        return
    await _execute_and_reply(
        message,
        followup,
        resume_id=info.session_id,
        cwd_override=info.project_dir,
        preview_override=f"[resume {info.short_id}] {followup}",
    )


async def handle_message(message: discord.Message) -> None:
    prompt = extract_prompt(message)
    if not prompt:
        return
    # Reserved sub-commands run instead of spawning a Claude session.
    stripped = prompt.strip()
    head, _, tail = stripped.partition(" ")
    cmd = head.lower().lstrip("/")
    if cmd == "status":
        await handle_status(message)
        return
    if cmd == "cancel":
        await handle_cancel(message, tail)
        return
    if cmd == "history":
        await handle_history(message, tail)
        return
    if cmd == "sessions":
        await handle_sessions(message, tail)
        return
    if cmd in ("transcript", "tx"):
        await handle_transcript(message, tail)
        return
    if cmd == "resume":
        await handle_resume(message, tail)
        return
    if cmd == "help":
        await message.reply(
            "**Commands** (with prefix `" + CONFIG["trigger_prefix"] + "`)\n"
            "• `<prompt>` — run a fresh Claude Code session\n"
            "• `resume <id_prefix> <message>` — continue an existing session with one follow-up\n"
            "• `status` — bot-spawned + every workstation Claude session\n"
            "• `cancel [job_id|all]` — kill one or all bot-spawned sessions\n"
            "• `history [n]` — last N (default 10) bot jobs (persists in SQLite)\n"
            "• `sessions [n] [project_filter]` — every Claude session on disk (default 10)\n"
            "• `transcript <id_prefix> [thinking]` — fetch one session as a markdown file\n"
            "• `help` — this message",
            mention_author=False,
        )
        return
    await _execute_and_reply(message, prompt)


def _set_intents() -> discord.Intents:
    intents = discord.Intents.default()
    # Required to read message content. Must also be toggled on in the
    # Developer Portal → Bot → Privileged Gateway Intents.
    intents.message_content = True
    return intents


class ClaudeBot(discord.Client):
    async def on_ready(self) -> None:  # type: ignore[override]
        logger.info(
            "Connected as %s (id=%s). Allowed channels=%s users=%s. Prefix=%r.",
            self.user, getattr(self.user, "id", "?"),
            sorted(CONFIG["allowed_channel_ids"]),
            sorted(CONFIG["allowed_user_ids"]),
            CONFIG["trigger_prefix"],
        )

    async def on_message(self, message: discord.Message) -> None:  # type: ignore[override]
        if not is_allowed(message):
            return
        # Fire-and-track so concurrent messages queue against the semaphore
        # rather than blocking each other in the on_message handler.
        task = asyncio.create_task(handle_message(message))
        RUNNING[message.id] = task
        try:
            await task
        finally:
            RUNNING.pop(message.id, None)


def main() -> None:
    bot = ClaudeBot(intents=_set_intents())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _graceful(*_: object) -> None:
        logger.info("Shutdown signal received; closing connection.")
        asyncio.ensure_future(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _graceful)

    try:
        loop.run_until_complete(bot.start(CONFIG["bot_token"]))
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bot.close())
        loop.close()


if __name__ == "__main__":
    main()
