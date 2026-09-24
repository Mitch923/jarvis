"""Memory/notes tools."""
from smolagents import tool

from tools.common import RunState, make_safe, Config
from memory import Memory


def create_notes_tools(cfg: Config, state: RunState, approve, memory: Memory, friction=None):
    """Create memory tools (remember, forget)."""
    if cfg.approval_mode == "none":
        # No approval needed in none mode
        def confirm_if_tainted(title: str, detail: str) -> bool:
            return True
    else:
        def confirm_if_tainted(title: str, detail: str) -> bool:
            # Web pages, files and PR text can try to plant "memories". If this task has read any
            # external content, the human gets the final say.
            if state.tainted and cfg.approval_mode != "none":
                return approve(title, detail + "\n_This task read external content, so I'm double-checking._")
            return True

    _safe = make_safe(friction.record if friction else None)
    tools = []

    @tool
    @_safe
    def remember(note: str) -> str:
        """Save one short note to long-term memory (kept across chats). Only when the owner asks you to remember something or states a lasting preference.

        Args:
            note: One short, self-contained sentence.
        """
        if not confirm_if_tainted("Save a memory", f"> {note[:300]}"):
            return "DENIED: the user declined this action. Do not retry it; tell the user and ask what they'd like instead."
        try:
            return f"Saved as note #{memory.add(note, source='agent')}."
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {type(e).__name__}: {e}"

    @tool
    @_safe
    def forget(note_id: int) -> str:
        """Delete a note from long-term memory by its id.

        Args:
            note_id: The number in [brackets] next to the note.
        """
        if not confirm_if_tainted("Delete a memory", f"Note #{int(note_id)}"):
            return "DENIED: the user declined this action. Do not retry it; tell the user and ask what they'd like instead."
        try:
            return f"Forgot note #{int(note_id)}." if memory.remove(int(note_id)) else f"ERROR: no note #{int(note_id)}."
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {type(e).__name__}: {e}"

    tools += [remember, forget]
    return tools