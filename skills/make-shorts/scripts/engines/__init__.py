"""Transcription engines. Only Groq exists in this version; the rest are deferred."""
from __future__ import annotations

from messages import UserFacingError

from .base import TranscriptionEngine

DEFERRED = ("ivrit_local", "elevenlabs")


def get_engine(name: str) -> TranscriptionEngine:
    if name == "groq":
        from .groq import GroqEngine

        return GroqEngine()
    raise UserFacingError("engine_not_available", engine=name)
