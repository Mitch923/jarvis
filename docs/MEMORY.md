# Long-term memory

Short notes in `data/memory.json` (plain JSON, max `MEMORY_MAX_NOTES`, 300 chars each) are added to the agent's prompt in every chat. Save with `!remember`, or just tell the agent "remember that…". Secret-looking strings (API keys) are refused. **If a task has already read web or GitHub content, the agent must ask you (✅/✖️) before saving or deleting a note**, so a malicious page can't plant a permanent instruction.

## Repo-scoped notes (Spec 3)

Notes are now namespaced by repository:

- **Global notes** (`repo: null`) — preferences, facts, style guides that apply everywhere.
- **Repo-scoped notes** (`repo: "owner/name"`) — project-specific conventions, API keys (refs only), build quirks, etc.

### Commands

| Command | Description |
|---------|-------------|
| `!remember <text>` | Save a global note. |
| `!remember repo:owner/name <text>` | Save a note scoped to that repository. |
| `!memory` | List global + current-repo notes (default). |
| `!memory repo` | List notes for the current repo (uses `SELF_REPO`). |
| `!memory repo:owner/name` | List notes for a specific repo. |
| `!memory all` | List every note across all repos. |
| `!forget <id>` | Delete a note by ID (shown in `!memory`). |

### Semantic recall

Each note gets a 384-dimension embedding (via `sentence-transformers` / `all-MiniLM-L6-v2`, ~90 MB, runs locally). The agent can recall relevant notes by meaning, not just keyword match:

- `recall(query_text, repo=None, top_k=5, threshold=0.35)` — search one namespace (global or a specific repo).
- `recall_combined(query_text, current_repo, top_k=5, threshold=0.35)` — search global + current repo, merged and deduplicated.

The agent automatically includes relevant global + current-repo notes in its context at the start of each chat task. For maintenance jobs (self-review, implement), memory is not injected.

### Embedding approach & tradeoffs

| Approach | Pros | Cons |
|----------|------|------|
| **Local sentence-transformers** (chosen) | Zero latency after first load, no API costs, works offline, privacy-preserving | ~90 MB download on first run, CPU inference (~50 ms/note on modern CPU) |
| Provider embeddings (OpenAI, etc.) | No local model, potentially higher quality | Network latency (~200-500 ms), API costs, rate limits, requires internet |

The local model is loaded **lazily** (first time embeddings are needed) so startup stays fast. Embeddings for new notes are generated in a background thread — `!remember` returns immediately. On startup, legacy notes without embeddings are backfilled in the background.

### Migration

On first load after upgrading:
1. Existing notes get `repo: null` (global) and `embedding: []`.
2. A background thread generates embeddings for all notes missing them.
3. The memory file is updated atomically as embeddings complete.

No manual migration step needed — just restart the bot.