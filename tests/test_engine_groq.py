import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import audio
import engines
from engines.groq import GroqEngine
from messages import UserFacingError

DEFAULT_WORDS = {
    "words": [{"word": "שלום", "start": 1.0, "end": 1.5}],
    "segments": [{"text": "שלום", "start": 1.0, "end": 1.5}],
}


def ok(payload=None):
    return (200, {}, json.dumps(payload or DEFAULT_WORDS, ensure_ascii=False).encode("utf-8"))


class FakeGroq:
    def __init__(self):
        self.requests = []
        self.script = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.requests.append({"headers": dict(self.headers), "body": body})
                status, headers, payload = outer.script.pop(0) if outer.script else ok()
                if status == 0:  # simulate a dropped connection
                    self.close_connection = True
                    return
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/audio/transcriptions"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake(monkeypatch, config_home):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-123")
    server = FakeGroq().start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def audio_file(synthetic_video, tmp_path_factory):
    return audio.extract_audio(synthetic_video, tmp_path_factory.mktemp("groq-audio") / "audio.mp3")


def make_engine(fake, sleeps, **kwargs):
    return GroqEngine(url=fake.url, sleep=sleeps.append, **kwargs)


def transcribe(engine, audio_file):
    return engine.transcribe(audio_file, language="he", progress=lambda msg: None)


def test_request_fields_model_language_verbose_json_and_word_granularity(fake, audio_file):
    transcribe(make_engine(fake, []), audio_file)
    body = fake.requests[0]["body"]
    assert b'name="model"\r\n\r\nwhisper-large-v3\r\n' in body
    assert b'name="language"\r\n\r\nhe\r\n' in body
    assert b'name="response_format"\r\n\r\nverbose_json\r\n' in body
    assert b'name="temperature"\r\n\r\n0\r\n' in body
    assert b'name="timestamp_granularities[]"\r\n\r\nword\r\n' in body


def test_custom_user_agent_sent(fake, audio_file):
    transcribe(make_engine(fake, []), audio_file)
    assert fake.requests[0]["headers"]["User-Agent"].startswith("make-shorts/")


def test_key_goes_in_authorization_header_only(fake, audio_file):
    transcribe(make_engine(fake, []), audio_file)
    request = fake.requests[0]
    assert request["headers"]["Authorization"] == "Bearer test-key-123"
    assert b"test-key-123" not in request["body"]


def test_returns_words_with_hebrew_text(fake, audio_file):
    t = transcribe(make_engine(fake, []), audio_file)
    assert [(w.text, w.start, w.end) for w in t.words] == [("שלום", 1.0, 1.5)]
    assert t.language == "he" and t.engine == "groq"
    assert t.approximate_timing is False and t.gaps == []


def test_429_honours_retry_after_max_two_retries(fake, audio_file):
    fake.script = [(429, {"Retry-After": "3"}, b"{}"), (429, {"Retry-After": "1"}, b"{}"), ok()]
    sleeps = []
    t = transcribe(make_engine(fake, sleeps), audio_file)
    assert sleeps == [3.0, 1.0]
    assert len(fake.requests) == 3
    assert len(t.words) == 1


def test_third_429_is_not_retried(fake, audio_file):
    fake.script = [(429, {"Retry-After": "1"}, b"{}")] * 3 + [ok()]
    with pytest.raises(UserFacingError) as exc:
        transcribe(make_engine(fake, []), audio_file)
    assert exc.value.key == "transcription_failed"
    assert len(fake.requests) == 3


def test_5xx_backoff_is_2_times_2_pow_attempt(fake, audio_file):
    fake.script = [(500, {}, b"{}"), (502, {}, b"{}"), ok()]
    sleeps = []
    transcribe(make_engine(fake, sleeps), audio_file)
    assert sleeps == [2.0, 4.0]


def test_other_4xx_not_retried(fake, audio_file):
    fake.script = [(400, {}, b'{"error": "bad"}'), ok()]
    with pytest.raises(UserFacingError) as exc:
        transcribe(make_engine(fake, []), audio_file)
    assert exc.value.key == "transcription_failed"
    assert len(fake.requests) == 1


def test_401_raises_api_key_rejected_immediately(fake, audio_file):
    fake.script = [(401, {}, b'{"error": "invalid key"}')]
    with pytest.raises(UserFacingError) as exc:
        transcribe(make_engine(fake, []), audio_file)
    assert exc.value.key == "api_key_rejected"
    assert "test-key-123" not in exc.value.message_he
    assert len(fake.requests) == 1


def test_network_error_backoff_is_2_times_attempt_plus_1(fake, audio_file):
    fake.script = [(0, {}, b"")] * 4
    sleeps = []
    with pytest.raises(UserFacingError) as exc:
        transcribe(make_engine(fake, sleeps), audio_file)
    assert sleeps == [2.0, 4.0, 6.0]
    assert len(fake.requests) == 4
    assert exc.value.key == "transcription_failed"


def test_missing_key_raises_api_key_needed_with_exit_3(monkeypatch, config_home, audio_file):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(UserFacingError) as exc:
        transcribe(GroqEngine(url="http://127.0.0.1:9/none", sleep=lambda s: None), audio_file)
    assert exc.value.key == "api_key_needed"
    assert exc.value.exit_code == 3


def chunked(fake, sleeps):
    return make_engine(fake, sleeps, max_upload_bytes=45_000, chunk_margin_bytes=0)


def test_chunk_offsets_applied(fake, audio_file):
    fake.script = [ok(), ok()]
    t = transcribe(chunked(fake, []), audio_file)
    assert len(fake.requests) == 2
    assert [round(w.start, 1) for w in t.words] == [1.0, 6.0]


def test_failed_chunk_recorded_as_gap_and_others_kept(fake, audio_file):
    fake.script = [ok(), (400, {}, b"{}")]
    t = transcribe(chunked(fake, []), audio_file)
    assert t.gaps == [(5.0, 10.0)]
    assert [round(w.start, 1) for w in t.words] == [1.0]


def test_all_chunks_fail_raises(fake, audio_file):
    fake.script = [(400, {}, b"{}"), (400, {}, b"{}")]
    with pytest.raises(UserFacingError) as exc:
        transcribe(chunked(fake, []), audio_file)
    assert exc.value.key == "transcription_failed"


def test_segment_only_response_falls_back_to_interpolated_words_and_sets_approximate_timing(fake, audio_file):
    fake.script = [ok({"segments": [{"text": "שלום עולם יפה", "start": 2.0, "end": 5.0}]})]
    t = transcribe(make_engine(fake, []), audio_file)
    assert [w.text for w in t.words] == ["שלום", "עולם", "יפה"]
    assert t.words[0].start == 2.0 and t.words[-1].end == 5.0
    assert t.approximate_timing is True


def test_deferred_engines_raise_engine_not_available():
    for name in ("ivrit_local", "elevenlabs"):
        with pytest.raises(UserFacingError) as exc:
            engines.get_engine(name)
        assert exc.value.key == "engine_not_available"
    assert engines.get_engine("groq").name == "groq"


def test_unknown_engine_name_raises():
    with pytest.raises(UserFacingError) as exc:
        engines.get_engine("nonsense")
    assert exc.value.key == "engine_not_available"


def test_bare_word_timestamps_get_punctuation_back_from_segment_text(fake, audio_file):
    fake.script = [ok({
        "words": [
            {"word": "שלום", "start": 1.0, "end": 1.4},
            {"word": "עולם", "start": 1.5, "end": 1.9},
            {"word": "מה", "start": 3.0, "end": 3.2},
            {"word": "נשמע", "start": 3.3, "end": 3.7},
        ],
        "segments": [
            {"text": " שלום, עולם.", "start": 1.0, "end": 2.0},
            {"text": " מה נשמע?", "start": 3.0, "end": 3.8},
        ],
    })]
    t = transcribe(make_engine(fake, []), audio_file)
    assert [w.text for w in t.words] == ["שלום,", "עולם.", "מה", "נשמע?"]
    assert [w.start for w in t.words] == [1.0, 1.5, 3.0, 3.3]


def test_punctuation_merge_leaves_words_alone_when_segment_text_does_not_match(fake, audio_file):
    fake.script = [ok({
        "words": [{"word": "אחת", "start": 1.0, "end": 1.4}, {"word": "שתיים", "start": 1.5, "end": 1.9}],
        "segments": [{"text": " משהו אחר לגמרי.", "start": 1.0, "end": 2.0}],
    })]
    t = transcribe(make_engine(fake, []), audio_file)
    assert [w.text for w in t.words] == ["אחת", "שתיים"]
