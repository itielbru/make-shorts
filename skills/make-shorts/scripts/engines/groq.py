"""Groq cloud transcription (Whisper large-v3).

Audio is uploaded as small mp3 chunks. Retries follow a fixed policy; a chunk
that still fails becomes a recorded gap rather than failing the whole run. The
key comes only from config/environment and never appears in a message.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from audio import MAX_UPLOAD_BYTES, plan_chunks, probe_duration, shift_words, split_audio
from config import load_secret
from messages import UserFacingError
from transcript import Transcript, Word, interpolate_words

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3"
# Groq's WAF rejects Python's default User-Agent with a 403.
USER_AGENT = "make-shorts/1.0 (+claude-code; python-urllib)"
MAX_ATTEMPTS = 4
MAX_429_RETRIES = 2
MAX_RETRY_AFTER_S = 120  # a longer wait means the daily quota is used up


_EDGE = re.compile(r"^[\W_]+|[\W_]+$")


def _norm(token: str) -> str:
    return _EDGE.sub("", token).casefold()


def merge_punctuation(words: list[Word], segments: list[dict]) -> list[Word]:
    """Word timestamps from Whisper-style APIs are often bare tokens; punctuation lives only in
    the segment text. Put it back where a segment token matches the word, so sentence ends survive."""
    merged = list(words)
    for seg in segments:
        tokens = str(seg.get("text", "")).split()
        lo, hi = float(seg["start"]) - 0.05, float(seg["end"]) + 0.05
        idx = [i for i, w in enumerate(merged) if lo <= w.start < hi]
        j = 0
        for token in tokens:
            wanted = _norm(token)
            if not wanted or j >= len(idx):
                continue
            for k in range(j, min(j + 3, len(idx))):  # small resync window
                if _norm(merged[idx[k]].text) == wanted:
                    w = merged[idx[k]]
                    merged[idx[k]] = Word(token, w.start, w.end)
                    j = k + 1
                    break
    return merged


class _ChunkFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _multipart(fields: list[tuple[str, str]], file_name: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            "Content-Type: audio/mpeg\r\n\r\n"
        ).encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class GroqEngine:
    name = "groq"

    def __init__(
        self,
        url: str = GROQ_URL,
        sleep: Callable[[float], None] = time.sleep,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
        chunk_margin_bytes: int = 64_000,
        timeout: float = 300,
    ) -> None:
        self.url = url
        self.sleep = sleep
        self.max_upload_bytes = max_upload_bytes
        self.chunk_margin_bytes = chunk_margin_bytes
        self.timeout = timeout

    def transcribe(
        self, audio: Path, *, language: str, progress: Callable[[str], None]
    ) -> Transcript:
        key = load_secret("GROQ_API_KEY")
        if not key:
            raise UserFacingError("api_key_needed", provider="Groq")

        size = audio.stat().st_size
        if size <= self.max_upload_bytes:
            chunks = [(0.0, 0.0)]
            files = [audio]
        else:
            chunks = plan_chunks(size, probe_duration(audio), self.max_upload_bytes - self.chunk_margin_bytes)
            files = split_audio(audio, chunks, audio.parent / "chunks")

        words: list[Word] = []
        gaps: list[tuple[float, float]] = []
        approximate = False
        last_reason = "unknown"
        for index, (file, (start, end)) in enumerate(zip(files, chunks), start=1):
            progress(f"מתמלל חלק {index} מתוך {len(files)}...")
            try:
                chunk_words, chunk_approx = self._transcribe_chunk(file, key, language)
            except _ChunkFailed as failure:
                last_reason = failure.reason
                gaps.append((start, end))
                progress(f"חלק {index} נכשל ({failure.reason}). ממשיך עם השאר.")
                continue
            words.extend(shift_words(chunk_words, start))
            approximate = approximate or chunk_approx

        if len(gaps) == len(files):
            raise UserFacingError("transcription_failed", provider="Groq", reason=last_reason)
        return Transcript(words=words, language=language, engine=self.name, gaps=gaps, approximate_timing=approximate)

    def _transcribe_chunk(self, file: Path, key: str, language: str) -> tuple[list[Word], bool]:
        fields = [
            ("model", MODEL),
            ("language", language),
            ("response_format", "verbose_json"),
            ("temperature", "0"),
            ("timestamp_granularities[]", "word"),
            ("timestamp_granularities[]", "segment"),
        ]
        body, content_type = _multipart(fields, "audio.mp3", file.read_bytes())
        payload = self._post_with_retries(body, content_type, key)
        return self._parse(payload)

    def _post_with_retries(self, body: bytes, content_type: str, key: str) -> dict:
        retries_429 = 0
        for attempt in range(MAX_ATTEMPTS):
            request = urllib.request.Request(
                self.url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": content_type,
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                code = err.code
                if code == 401:
                    raise UserFacingError("api_key_rejected", provider="Groq") from None
                if code == 429 and retries_429 < MAX_429_RETRIES and attempt < MAX_ATTEMPTS - 1:
                    wait = self._retry_after(err)
                    if wait > MAX_RETRY_AFTER_S:
                        raise _ChunkFailed("המכסה של Groq מלאה כרגע, כדאי לנסות שוב מאוחר יותר") from None
                    retries_429 += 1
                    self.sleep(wait)
                    continue
                if 500 <= code < 600 and attempt < MAX_ATTEMPTS - 1:
                    self.sleep(2.0 * 2**attempt)
                    continue
                raise _ChunkFailed(f"HTTP {code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt < MAX_ATTEMPTS - 1:
                    self.sleep(2.0 * (attempt + 1))
                    continue
                raise _ChunkFailed("בעיית רשת") from None
            except ValueError:
                raise _ChunkFailed("תשובה לא תקינה") from None
        raise _ChunkFailed("unknown")

    @staticmethod
    def _retry_after(err: urllib.error.HTTPError) -> float:
        try:
            return float(err.headers.get("Retry-After", "2"))
        except (TypeError, ValueError):
            return 2.0

    @staticmethod
    def _parse(payload: dict) -> tuple[list[Word], bool]:
        raw_words = payload.get("words")
        if raw_words:
            words = [
                Word(str(w["word"]).strip(), float(w["start"]), float(w["end"]))
                for w in raw_words
                if str(w.get("word", "")).strip()
            ]
            if payload.get("segments"):
                words = merge_punctuation(words, payload["segments"])
            return words, False
        words = []
        for segment in payload.get("segments") or []:
            words.extend(interpolate_words(str(segment.get("text", "")), float(segment["start"]), float(segment["end"])))
        return words, True
