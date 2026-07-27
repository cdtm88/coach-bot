"""Persona loading.

CHAT-02: the persona is a versioned system prompt file, seeded from the source
coaching conversation. Changing the file changes behaviour without a code
deploy — so it is read from disk per turn, not imported once.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PERSONA_PATH = Path(os.environ.get("PERSONA_PATH") or REPO / "prompts" / "persona.md")
SEED_PATH = REPO / "docs" / "seed" / "coaching-conversation.md"


class PersonaMissing(RuntimeError):
    """The persona file is absent. P01 cannot run without it."""


def load(path: Path | None = None) -> str:
    """Read the persona prompt.

    Read per call rather than cached, because CHAT-02's acceptance is that
    editing the file changes behaviour with no deploy. The file is small and the
    read is dwarfed by the model call it feeds.
    """
    target = path or PERSONA_PATH
    try:
        return target.read_text().strip()
    except FileNotFoundError as exc:
        raise PersonaMissing(
            f"{target} is missing. CHAT-02 requires a versioned persona file, "
            f"seeded from {SEED_PATH}."
        ) from exc


def is_seeded(path: Path | None = None) -> bool:
    """False while the persona is still the un-filled scaffold.

    The scaffold ships with the repository so P01 is buildable and testable, but
    it is not the athlete's coach. Open item 9 tracks replacing it.
    """
    return "TO BE SEEDED" not in load(path)
