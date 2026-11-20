# mouseion

## The shared mind is binding (Bruno, 2026-11-20)

Every session, on every AI, works with Egon's shared mind: the context of all other sessions is read first, and this
session's decisions, results and open questions are written back.
- **Read first.** The hook `.claude/hooks/mind_bind.py` injects the mind's capsule at session start and on every prompt.
  For more, use the egon-mind tools: `mind_context_v2`, `mind_memory_search` (on the PC: the egon-mind MCP server;
  elsewhere: the claude.ai connector or `$EGON_MIND_URL/m/mcp`).
- **Write back.** Record durable decisions, results and lessons with `mind_memory_upsert` (agent and project set).
  The hook already records prompts, tool use and turn ends as activity.
- **If the context says the shared mind is NOT CONNECTED or UNREACHABLE, tell Bruno in your first reply.** Never
  work silently without it. Setup and repair: `egon/docs/REMOTE_MIND.md`.
