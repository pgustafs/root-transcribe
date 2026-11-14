"""Unit tests for parsing the STT provider response."""

from __future__ import annotations

import pytest

from app.services.transcription import _parse_response

pytestmark = pytest.mark.unit


def test_parse_word_timestamps() -> None:
    data = {
        "text": "hello world",
        "language": "en",
        "duration": 1.5,
        "words": [
            {"word": "hello", "start": 0.0, "end": 0.5},
            {"word": "world", "start": 0.6, "end": 1.0},
        ],
    }
    result = _parse_response(data)
    assert result.text == "hello world"
    assert result.language == "en"
    assert result.duration == 1.5
    assert [w.text for w in result.words] == ["hello", "world"]
    assert result.words[0].start == 0.0
    assert result.words[1].end == 1.0
    assert result.has_word_timestamps is True


def test_parse_falls_back_to_segments_without_words() -> None:
    data = {
        "text": "hi there",
        "segments": [
            {"text": "hi", "start": 0.0, "end": 0.4},
            {"text": "there", "start": 0.4, "end": 0.9},
        ],
    }
    result = _parse_response(data)
    assert [w.text for w in result.words] == ["hi", "there"]
    # Segment-derived stand-ins are flagged so the worker uses per-turn STT.
    assert result.has_word_timestamps is False


def test_parse_tolerates_missing_fields() -> None:
    result = _parse_response({"text": "x"})
    assert result.words == []
    assert result.language is None
    assert result.duration is None


def test_parse_object_style_words() -> None:
    class W:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class Resp:
        text = "a b"
        language = "sv"
        duration = 2.0
        words = [W("a", 0.0, 0.3), W("b", 0.3, 0.6)]
        segments = None

    result = _parse_response(Resp())
    assert [w.text for w in result.words] == ["a", "b"]
    assert result.language == "sv"


def test_dedicated_stt_endpoint_uses_only_its_key(monkeypatch):
    import app.services.transcription as transcription

    captured = {}
    client = object()

    def make_client(**kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(
        transcription.settings, "STT_BASE_URL", "https://stt.example/v1"
    )
    monkeypatch.setattr(transcription.settings, "STT_API_KEY", "speech-key")
    monkeypatch.setattr(transcription, "OpenAI", make_client)
    assert transcription.TranscriptionService()._sync() is client
    assert captured["base_url"] == "https://stt.example/v1"
    assert captured["api_key"] == "speech-key"


def test_missing_stt_endpoint_does_not_fall_back_to_chat(monkeypatch):
    from app.services.transcription import TranscriptionService, settings

    monkeypatch.setattr(settings, "STT_BASE_URL", "")
    with pytest.raises(ValueError, match="STT_BASE_URL"):
        TranscriptionService()._sync()


@pytest.mark.parametrize("language", [None, "en", "sv"])
@pytest.mark.parametrize("from_file", [False, True])
def test_transcription_request_language(language, from_file, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from openai import NOT_GIVEN

    from app.services.transcription import TranscriptionService

    create = Mock(return_value={"text": "hello", "language": "en"})
    service = TranscriptionService()
    service._client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create))
    )
    if from_file:
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"audio")
        service.transcribe_file_sync(str(audio), "audio.wav", language=language)
    else:
        service.transcribe_sync(b"audio", "turn.wav", language=language)
    sent = create.call_args.kwargs["language"]
    if language is None:
        assert sent is NOT_GIVEN  # no language field on the HTTP request
    else:
        assert sent == language
