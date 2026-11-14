"""Unit tests for speaker-embedding math + voiceprint encryption."""

from __future__ import annotations

import pytest

from app.services.speaker_directory import (
    centroid,
    cosine_similarity,
    decrypt_embedding,
    encrypt_embedding,
)

pytestmark = pytest.mark.unit


def test_cosine_similarity() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)
    # Degenerate inputs never raise.
    assert cosine_similarity([0, 0], [1, 1]) == 0.0
    assert cosine_similarity([1], [1, 2]) == 0.0


def test_centroid_is_the_mean() -> None:
    assert centroid([[0.0, 0.0], [2.0, 4.0]]) == [1.0, 2.0]
    assert centroid([]) == []


def test_encrypt_decrypt_roundtrip() -> None:
    vec = [0.1, -0.2, 0.3, 0.4]
    blob = encrypt_embedding(vec)
    assert isinstance(blob, bytes)
    # Ciphertext must not leak the raw floats.
    assert b"0.1" not in blob
    out = decrypt_embedding(blob, dims=len(vec))
    assert out == pytest.approx(vec, abs=1e-6)


def test_decrypt_bad_blob_raises_valueerror() -> None:
    with pytest.raises(ValueError):
        decrypt_embedding(b"not-a-fernet-token", dims=4)
