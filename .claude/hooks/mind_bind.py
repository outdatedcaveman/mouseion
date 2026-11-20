#!/usr/bin/env python3
"""Bind every session to Egon's shared mind (Claude Code hook; stdlib only; never blocks for more than a few seconds).

Bruno, 2026-11-20: "make sure this never happens again to any session on any AI. Make sure the mind is binding to all
activity." Canonical copy: egon/tools/mind_bind.py. Each repo carries a copy in .claude/hooks/ and wires it in
.claude/settings.json for SessionStart, UserPromptSubmit, PostToolUse and Stop.

- On the PC (mind API on 127.0.0.1:8000): do nothing; Egon's own hooks in ~/.claude/settings.json bind the session.
- Elsewhere (cloud sessions, other machines): EGON_MIND_URL = the stable remote base (e.g. https://pc.tailnet.ts.net).
  Auth: in claude.ai/code cloud sessions the network proxy injects the saved credential for that host; elsewhere set
  EGON_MIND_TOKEN. Reads: the mind's context capsule at session start and on every prompt (printed = added to context).
  Writes: every prompt, tool use and turn end goes to the mind's activity stream as this session.
- Not connected: the session is told, loudly, at start and on every prompt, so it can never again work silently
  without the mind.
"""
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = float(os.environ.get("EGON_MIND_TIMEOUT", "6"))
DOC = "egon/docs/REMOTE_MIND.md"


def _down_file(ev):
    import tempfile
    return os.path.join(tempfile.gettempdir(), f"egon_mind_down_{(ev.get('session_id') or 'x')[:40]}")


def recently_down(ev, seconds=300) -> bool:
    import time
    try:
        return time.time() - os.path.getmtime(_down_file(ev)) < seconds
    except OSError:
        return False


def mark_down(ev, down=True):
    try:
        if down:
            open(_down_file(ev), "w").close()
        elif os.path.exists(_down_file(ev)):
            os.remove(_down_file(ev))
    except OSError:
        pass


def local_mind() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.3):
            return True
    except OSError:
        return False


def req(method, base, path, body=None, params=None):
    url = base.rstrip("/") + path + ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    tok = os.environ.get("EGON_MIND_TOKEN")
    if tok:
        r.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def project(ev) -> str:
    return os.path.basename((ev.get("cwd") or os.getcwd()).rstrip("/\\")) or "unknown"


def agent() -> str:
    return os.environ.get("EGON_MCP_AGENT") or ("claude-code-cloud" if os.environ.get("CLAUDE_CODE_REMOTE") else "claude-code-remote")


def last_assistant_text(path, limit=2000) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-200:]
        for line in reversed(lines):
            o = json.loads(line)
            msg = o.get("message") or {}
            if o.get("type") == "assistant" or msg.get("role") == "assistant":
                parts = msg.get("content")
                if isinstance(parts, list):
                    t = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
                else:
                    t = str(parts or "")
                if t.strip():
                    return t.strip()[-limit:]
    except Exception:
        pass
    return ""


def record(base, ev, kind, payload):
    body = {"agent": agent(), "session": ev.get("session_id") or "", "kind": kind, "payload": payload,
            "project": project(ev)}
    try:
        req("POST", base, "/m/event", body)
    except urllib.error.HTTPError as e:
        if e.code == 404 and kind in ("user_prompt", "turn_end"):       # PC not updated yet: keep a trace anyway
            text = f"[{agent()} · {project(ev)} · {kind}] " + (payload.get("text") or "")[:3000]
            try:
                req("POST", base, "/m/thought", {"text": text})
            except Exception:
                pass
    except Exception:
        pass


def capsule(base, ev, q=""):
    try:
        st, text = req("GET", base, "/m/capsule", params={"q": q[:500], "project": project(ev), "agent": agent()})
        return text
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return f"(shared mind answered HTTP {e.code})"
    except Exception as e:
        return None if isinstance(e, (urllib.error.URLError, OSError)) else f"(shared mind error: {type(e).__name__})"
    tok = os.environ.get("EGON_MIND_TOKEN")
    if tok:                                                            # PC not updated yet: recent activity only
        try:
            st, text = req("GET", base, "/m/activity", params={"limit": 15, "k": tok})
            act = (json.loads(text) or {}).get("activity") or []
            lines = [f"- {a.get('agent') or a.get('source') or a.get('kind')}: "
                     f"{str(a.get('summary') or a.get('title') or a.get('text') or '')[:160]}" for a in act[:15]]
            return ("(The PC runs an older Egon: only recent activity is available. Pull egon and restart egon_core "
                    "for full binding.)\nRecent activity across agents (shared mind):\n" + "\n".join(lines))
        except Exception:
            pass
    return ("(The shared mind is reachable, but the PC runs an older Egon without the remote gateway. Tell Bruno: pull "
            "egon and restart egon_core, see " + DOC + ".)")


def main():
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except Exception:
        ev = {}
    name = ev.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if local_mind():
        return 0                                                       # Egon's own hooks bind PC sessions
    base = os.environ.get("EGON_MIND_URL", "").strip()
    speaks = name in ("SessionStart", "UserPromptSubmit")
    if not base:
        if speaks:
            print("⚠ SHARED MIND NOT CONNECTED: this session has no local Egon and no EGON_MIND_URL, so it cannot read "
                  "or write the shared mind. Tell Bruno at the start of your reply, and see " + DOC + ".")
        return 0
    if name == "SessionStart":
        record(base, ev, "session_start", {"source": ev.get("source"), "cwd": ev.get("cwd")})
        c = capsule(base, ev)
    elif name == "UserPromptSubmit":
        p = ev.get("prompt") or ""
        record(base, ev, "user_prompt", {"text": p[:4000]})
        c = capsule(base, ev, p)
    elif name in ("PostToolUse", "Stop") and recently_down(ev):
        return 0                                                       # do not stall every tool call on a dead link
    elif name == "PostToolUse":
        ti = ev.get("tool_input") or {}
        record(base, ev, "tool", {"tool": ev.get("tool_name"), "file": ti.get("file_path"),
                                  "command": str(ti.get("command") or "")[:300], "description": ti.get("description")})
        return 0
    elif name == "Stop":
        record(base, ev, "turn_end", {"text": last_assistant_text(ev.get("transcript_path") or "")})
        return 0
    else:
        return 0
    mark_down(ev, c is None)
    if c is None:
        print(f"⚠ SHARED MIND UNREACHABLE at {base} (PC off, or its address changed). Tell Bruno at the start of "
              f"your reply; see {DOC}.")
    elif c.strip():
        print("## Shared mind (Egon)\n" + c.strip())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)                                                    # a hook must never break the session
