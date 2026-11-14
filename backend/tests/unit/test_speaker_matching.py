"""Unit tests for calibrated speaker matching + the biometric keyring (#6, A4)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import speaker_directory, speaker_matching

pytestmark = pytest.mark.unit


def _turn(speaker: str, start: float, end: float) -> SimpleNamespace:
    return SimpleNamespace(speaker=speaker, start=start, end=end)


# ---------------------------------------------------------------- turn picking


def test_select_turns_filters_short_and_caps_count() -> None:
    turns = [
        _turn("A", 0, 0.5),  # too short
        _turn("A", 1, 4),
        _turn("B", 4, 9),  # other speaker
        _turn("A", 10, 20),
        _turn("A", 21, 24.5),
        _turn("A", 25, 31),
    ]
    picked = speaker_matching.select_turns(turns, "A", min_seconds=2.0, max_turns=3)
    # Longest three of A's clean turns, longest first.
    assert [(t.start, t.end) for t in picked] == [(10, 20), (25, 31), (21, 24.5)]


# ------------------------------------------------------------------- decisions


def test_decide_auto_assigns_only_with_score_and_margin() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    # Clear winner: high score, wide margin → auto-assign.
    d = speaker_matching.decide(
        {a: 0.82, b: 0.30},
        threshold=0.55,
        margin=0.08,
        suggest_threshold=0.45,
        sample_turns=3,
    )
    assert d.profile_id == a and d.suggestion_profile_id is None

    # High score but two close candidates → NEVER auto-name; review queue.
    d = speaker_matching.decide(
        {a: 0.82, b: 0.79},
        threshold=0.55,
        margin=0.08,
        suggest_threshold=0.45,
        sample_turns=3,
    )
    assert d.profile_id is None
    assert d.suggestion_profile_id == a
    assert d.margin == pytest.approx(0.03)

    # Below threshold but above the suggest floor → review queue.
    d = speaker_matching.decide(
        {a: 0.50, b: 0.20},
        threshold=0.55,
        margin=0.08,
        suggest_threshold=0.45,
        sample_turns=2,
    )
    assert d.profile_id is None and d.suggestion_profile_id == a

    # Weak everywhere → anonymous, no suggestion (an uncertain guess must not
    # even reach the queue).
    d = speaker_matching.decide(
        {a: 0.30, b: 0.20},
        threshold=0.55,
        margin=0.08,
        suggest_threshold=0.45,
        sample_turns=2,
    )
    assert d.profile_id is None and d.suggestion_profile_id is None

    # Single candidate: margin defaults to 1.0 (nothing to confuse with).
    d = speaker_matching.decide(
        {a: 0.60},
        threshold=0.55,
        margin=0.08,
        suggest_threshold=0.45,
        sample_turns=1,
    )
    assert d.profile_id == a and d.margin == 1.0


def test_score_profiles_uses_mean_of_top_turn_scores() -> None:
    a = uuid.uuid4()
    centroids = {a: [1.0, 0.0]}
    embeddings = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]
    scores = speaker_matching.score_profiles(embeddings, centroids)
    # Top-3 of [1, 1, 0, 0] → mean(1, 1, 0) ≈ 0.667: bad turns dilute, not veto.
    assert scores[a] == pytest.approx(2 / 3, abs=1e-6)


# --------------------------------------------------------------------- keyring


def test_keyring_roundtrip_per_key_and_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    speaker_directory._fernet.cache_clear()
    monkeypatch.setattr(settings, "BIOMETRIC_KEYS", {"k2026": "rotate-me-secret"})

    vector = [0.25, -0.5, 1.0]
    legacy_blob = speaker_directory.encrypt_embedding(vector, key_id="legacy")
    new_blob = speaker_directory.encrypt_embedding(vector, key_id="k2026")

    assert speaker_directory.decrypt_embedding(
        legacy_blob, 3, key_id="legacy"
    ) == pytest.approx(vector)
    assert speaker_directory.decrypt_embedding(
        new_blob, 3, key_id="k2026"
    ) == pytest.approx(vector)

    # Wrong key ⇒ ValueError, never silent garbage.
    with pytest.raises(ValueError):
        speaker_directory.decrypt_embedding(legacy_blob, 3, key_id="k2026")
    with pytest.raises(ValueError):
        speaker_directory.decrypt_embedding(new_blob, 3, key_id="unknown-id")

    # The active key id drives new encryptions.
    monkeypatch.setattr(settings, "BIOMETRIC_ACTIVE_KEY_ID", "k2026")
    active_blob = speaker_directory.encrypt_embedding(vector)
    assert speaker_directory.decrypt_embedding(
        active_blob, 3, key_id="k2026"
    ) == pytest.approx(vector)
    speaker_directory._fernet.cache_clear()
