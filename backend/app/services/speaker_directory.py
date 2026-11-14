"""Speaker-embedding math + at-rest encryption for the voiceprint directory.

Two concerns, both dependency-light (stdlib + ``cryptography``) so they unit test
without torch/numpy:

- **Encryption** — voiceprints are biometric data, so the embedding vector is
  stored Fernet-encrypted under a keyring: each row records the ``key_id`` that
  encrypted it. The default ``legacy`` key derives from ``settings.SECRET_KEY``
  (the same secret ``enforce_secrets`` refuses to leave at its placeholder in
  prod); ``BIOMETRIC_KEYS``/``BIOMETRIC_ACTIVE_KEY_ID`` add rotatable named keys
  (``tasks.reencrypt_voiceprints``). A database dump alone never yields usable
  biometrics.
- **Matching** — cosine similarity, centroid, and threshold selection in pure
  Python (embeddings are a few hundred dims; numpy is unnecessary here and would
  pull the heavy ML stack into the API/test env). The worker converts pyannote's
  numpy embedding to ``list[float]`` before calling in.

See ARCHITECTURE.md.
"""

from __future__ import annotations

import base64
import hashlib
import math
import struct
from collections.abc import Sequence
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


@lru_cache(maxsize=8)
def _fernet(key_id: str = "legacy") -> Fernet:
    """Fernet for one biometric keyring entry (issue #6).

    ``legacy`` derives from ``SECRET_KEY`` (the pre-#6 scheme, kept so existing
    prints stay readable); any other id must exist in ``BIOMETRIC_KEYS``. A key
    entry may be a ready-made urlsafe-base64 Fernet key or an arbitrary
    passphrase (derived via sha256, like legacy). Because every voiceprint row
    records its ``key_id``, adding a new active key never invalidates old
    prints — rotation is an explicit re-encrypt (tasks.reencrypt_voiceprints).
    """
    if key_id == "legacy":
        material = settings.SECRET_KEY
    else:
        try:
            material = settings.BIOMETRIC_KEYS[key_id]
        except KeyError as exc:
            raise ValueError(f"unknown biometric key id {key_id!r}") from exc
    if len(material) == 44 and material.endswith("="):
        try:  # looks like a ready-made Fernet key — use it verbatim
            return Fernet(material.encode("utf-8"))
        except ValueError:
            pass
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(key)


def active_key_id() -> str:
    """The keyring id new voiceprints are encrypted under."""
    return settings.BIOMETRIC_ACTIVE_KEY_ID


def encrypt_embedding(vector: Sequence[float], *, key_id: str | None = None) -> bytes:
    """Pack a float vector and Fernet-encrypt it under the (active) key."""
    packed = struct.pack(f"{len(vector)}f", *vector)
    return _fernet(key_id or active_key_id()).encrypt(packed)


def decrypt_embedding(blob: bytes, dims: int, *, key_id: str = "legacy") -> list[float]:
    """Decrypt + unpack a stored voiceprint into a float vector.

    Raises :class:`ValueError` if the blob can't be decrypted (wrong/rotated key
    or corruption) so the caller can skip the stale print rather than crash.
    """
    try:
        packed = _fernet(key_id).decrypt(blob)
    except (InvalidToken, ValueError) as exc:  # rotated key / unknown id
        raise ValueError("voiceprint could not be decrypted") from exc
    return list(struct.unpack(f"{dims}f", packed))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors in [-1, 1] (0 if degenerate)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    """Mean vector of one profile's voiceprints (its comparison anchor)."""
    if not vectors:
        return []
    dims = len(vectors[0])
    acc = [0.0] * dims
    for vec in vectors:
        for i in range(dims):
            acc[i] += vec[i]
    n = len(vectors)
    return [v / n for v in acc]
