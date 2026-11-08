"""The contract every transcription engine follows."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from transcript import Transcript


class TranscriptionEngine(Protocol):
    name: str

    def transcribe(
        self, audio: Path, *, language: str, progress: Callable[[str], None]
    ) -> Transcript:
        """Return words with start/end seconds. The language hint is always given."""
        ...
