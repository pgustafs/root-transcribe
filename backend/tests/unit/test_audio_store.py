"""Unit tests for the S3-backed meeting-audio store (boto3 faked)."""

from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import NotFoundError
from app.services.audio_store import (
    ErasureUnverified,
    S3AudioStore,
    _suffix,
    is_object_missing,
    new_audio_key,
    new_voiceprint_key,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- key building


def test_suffix_sanitizes_hostile_filenames() -> None:
    assert _suffix("meeting.webm") == "webm"
    assert _suffix("noext") == "webm"
    assert _suffix("UPPER.WAV") == "wav"
    # Path separators / oddities can never leak into the object key.
    assert _suffix("evil.name/../../x") == "x"
    assert _suffix("a.b/c!!") == "bc"
    assert _suffix("dot.") == "webm"
    # Length-capped.
    assert len(_suffix("x." + "a" * 50)) <= 8


def test_new_keys_are_namespaced_and_unique() -> None:
    ws = uuid.uuid4()
    k1 = new_audio_key(ws, "m.webm")
    k2 = new_audio_key(ws, "m.webm")
    assert k1 != k2
    assert k1.startswith(f"workspaces/{ws}/")
    assert k1.endswith(".webm")

    pid = uuid.uuid4()
    vk = new_voiceprint_key(pid, "sample.wav")
    assert vk.startswith(f"speakers/{pid}/")
    assert vk.endswith(".wav")


# ------------------------------------------------------------ fake S3 client


class _NoSuchKey(Exception):
    def __init__(self) -> None:
        super().__init__("missing")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def iter_chunks(self, chunk_size: int = 8):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]


class _FakeS3:
    def get_bucket_versioning(self, Bucket):  # noqa: N803 — boto3 casing
        return {}  # unversioned (the demo-store shape)

    def get_paginator(self, name):  # noqa: ARG002
        class _Empty:
            def paginate(self, **_kw):
                return []

        return _Empty()

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.bucket_exists = False
        self.created_buckets: list[str] = []

    def head_bucket(self, Bucket: str) -> None:  # noqa: N803 (boto3 API shape)
        if not self.bucket_exists:
            raise _NoSuchKey()

    def create_bucket(self, Bucket: str) -> None:  # noqa: N803
        self.bucket_exists = True
        self.created_buckets.append(Bucket)

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
        self.objects[Key] = (Body, ContentType)

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _NoSuchKey()
        return {"Body": _Body(self.objects[Key][0])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _NoSuchKey()
        body, content_type = self.objects[Key]
        return {
            "ContentLength": len(body),
            "ETag": '"etag"',
            "ContentType": content_type,
        }


@pytest.fixture
def store() -> S3AudioStore:
    s = S3AudioStore()
    s._client = _FakeS3()  # bypass boto3 construction entirely
    return s


def test_put_creates_bucket_once_and_stores(store: S3AudioStore) -> None:
    store.put("k1", b"audio", "audio/webm")
    store.put("k2", b"more", "audio/wav")
    fake: _FakeS3 = store._client  # type: ignore[assignment]
    assert fake.created_buckets == ["meeting-audio"]  # ensure-bucket cached
    assert fake.objects["k1"] == (b"audio", "audio/webm")


def test_get_sync_roundtrip_and_not_found(store: S3AudioStore) -> None:
    store.put("k", b"bytes", "audio/wav")
    assert store.get_sync("k") == b"bytes"
    with pytest.raises(NotFoundError):
        store.get_sync("missing")


def test_stream_sync_chunks_and_not_found(store: S3AudioStore) -> None:
    store.put("k", b"0123456789abcdef", "audio/wav")
    chunks = list(store.stream_sync("k"))
    assert b"".join(chunks) == b"0123456789abcdef"
    with pytest.raises(NotFoundError):
        store.stream_sync("missing")


def test_delete_sync_is_idempotent(store: S3AudioStore) -> None:
    store.put("k", b"x", "audio/wav")
    store.delete_sync("k")
    store.delete_sync("k")  # already gone: no raise
    with pytest.raises(NotFoundError):
        store.get_sync("k")


class _AccessDenied(Exception):
    def __init__(self) -> None:
        super().__init__("denied")
        self.response = {"Error": {"Code": "AccessDenied"}}


def test_delete_sync_not_found_is_success(store: S3AudioStore) -> None:
    fake: _FakeS3 = store._client  # type: ignore[assignment]

    def _missing(Bucket, Key):  # noqa: N803
        raise _NoSuchKey()

    fake.delete_object = _missing
    store.delete_sync("missing")  # classified as already-gone: no raise


def test_delete_sync_raises_on_store_failure(store: S3AudioStore) -> None:
    """An outage / permission error must surface — a swallowed delete failure is
    how metadata ends up claiming erasure that never happened."""
    store.put("k", b"x", "audio/wav")
    fake: _FakeS3 = store._client  # type: ignore[assignment]

    def _boom(Bucket, Key):  # noqa: N803
        raise _AccessDenied()

    fake.delete_object = _boom
    with pytest.raises(_AccessDenied):
        store.delete_sync("k")


async def test_async_wrappers_delegate(
    store: S3AudioStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async wrappers delegate to the sync implementations.

    ``anyio.to_thread.run_sync`` is patched to run inline: the contract under
    test is the DELEGATION, not AnyIO's thread offload — and one reviewer
    environment saw the real offload deadlock under its pytest plugin stack.
    """
    import anyio.to_thread

    async def _inline(func, *args, **kwargs):
        kwargs.pop("abandon_on_cancel", None)
        kwargs.pop("limiter", None)
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", _inline)
    store.put("k", b"x", "audio/wav")
    assert await store.get("k") == b"x"
    await store.delete("k")
    with pytest.raises(NotFoundError):
        await store.get("k")


class _MultipartFakeS3(_FakeS3):
    """Fake with one in-progress multipart upload for key "k" plus a sibling
    key that shares the prefix — erasure must abort only exact-key uploads."""

    def __init__(self) -> None:
        super().__init__()
        self.aborted: list[tuple[str, str]] = []
        self.uploads = [
            {"Key": "k", "UploadId": "u1"},
            {"Key": "k-sibling", "UploadId": "u2"},
        ]

    def get_paginator(self, name):
        if name == "list_multipart_uploads":
            uploads = self.uploads

            class _Pages:
                def paginate(self, **_kw):
                    return [{"Uploads": uploads}]

            return _Pages()
        return super().get_paginator(name)

    def abort_multipart_upload(self, Bucket, Key, UploadId):  # noqa: N803
        self.aborted.append((Key, UploadId))
        self.uploads = [u for u in self.uploads if u["UploadId"] != UploadId]

    def list_parts(self, Bucket, Key, UploadId):  # noqa: N803
        """An aborted upload holds nothing — the ordinary case."""
        live = any(u["UploadId"] == UploadId for u in self.uploads)
        return {"Parts": [{"PartNumber": 1}] if live else []}


def test_delete_sync_aborts_in_progress_multipart_uploads() -> None:
    """Erasure includes the PARTS namespace (review round 5): deleting a key
    whose multipart upload is still open must abort that upload — otherwise a
    workspace deleted mid-upload reports erasure while audio parts survive."""
    store = S3AudioStore()
    fake = _MultipartFakeS3()
    store._client = fake
    store.put("k", b"x", "audio/wav")

    store.delete_sync("k")

    assert fake.aborted == [("k", "u1")]  # exact key only — sibling untouched
    assert "k" not in fake.objects


class _LateUploadPartFakeS3(_MultipartFakeS3):
    """An ``UploadPart`` that lands AFTER the abort (issue #37 review r17).

    AWS documents this explicitly: aborting does not stop a part request that is
    already in flight, so the part can be stored afterwards and the upload stays
    listed. The object HEAD cannot see it — parts are not in the object
    namespace — so erasure reported success over audio that was still there.
    """

    def __init__(self, *, late_parts: int = 1) -> None:
        super().__init__()
        #: How many more times a part will land right after an abort.
        self.late_parts = late_parts
        #: upload id -> parts currently stored.
        self.parts: dict[str, list[dict]] = {"u1": [{"PartNumber": 1}]}
        self.list_parts_calls = 0

    def abort_multipart_upload(self, Bucket, Key, UploadId):  # noqa: N803
        self.aborted.append((Key, UploadId))
        self.parts.pop(UploadId, None)
        self.uploads = [u for u in self.uploads if u["UploadId"] != UploadId]
        if Key == "k" and self.late_parts > 0:
            # The in-flight UploadPart completes now: the part is stored and
            # the upload is live again.
            self.late_parts -= 1
            self.parts[UploadId] = [{"PartNumber": 2}]
            self.uploads = [*self.uploads, {"Key": Key, "UploadId": UploadId}]

    def list_parts(self, Bucket, Key, UploadId):  # noqa: N803
        self.list_parts_calls += 1
        return {"Parts": self.parts.get(UploadId, [])}


def test_a_part_landing_after_the_abort_is_not_mistaken_for_erasure() -> None:
    """One successful abort is NOT proof the multipart namespace is empty.

    The part that lands after it is invisible to the final object HEAD, so
    erasure used to confirm while sensitive audio parts remained. It now
    verifies with ListParts and aborts again until a listing comes back empty.
    """
    store = S3AudioStore()
    fake = _LateUploadPartFakeS3()
    store._client = fake
    store.put("k", b"x", "audio/wav")

    store.delete_sync("k")

    # Aborted more than once — the second round cleaned up the late part.
    assert len(fake.aborted) >= 2
    assert fake.list_parts_calls >= 1
    assert fake.parts.get("u1", []) == []  # no parts left anywhere
    assert fake.uploads == [{"Key": "k-sibling", "UploadId": "u2"}]


def test_erasure_refuses_while_a_writer_keeps_pushing_parts() -> None:
    """A client that keeps uploading makes erasure RAISE, never confirm.

    The deletion sweep retries (and the worker fences the session first, so in
    production this converges); confirming here would record an erasure of
    audio that is still being written.
    """
    store = S3AudioStore()
    fake = _LateUploadPartFakeS3(late_parts=99)
    store._client = fake
    store.put("k", b"x", "audio/wav")

    with pytest.raises(ErasureUnverified, match="still present after"):
        store.delete_sync("k")


def test_erasure_refuses_when_the_multipart_listing_is_unavailable() -> None:
    """ "I can't look" is not "nothing is there" (issue #37 review r17b).

    A store without ListMultipartUploads used to be tolerated as not-found, and
    erasure returned success — but the object HEAD cannot stand in for it, since
    parts are not objects. An unverifiable store now fails the deletion
    visibly (`delete_failed`, key preserved) instead of confirming an erasure
    nobody established.
    """

    class _NoListing(_FakeS3):
        def get_paginator(self, name):
            if name == "list_multipart_uploads":
                raise _NoSuchKey()
            return super().get_paginator(name)

    store = S3AudioStore()
    store._client = _NoListing()
    store.put("k", b"x", "audio/wav")

    with pytest.raises(ErasureUnverified, match="cannot list multipart uploads"):
        store.delete_sync("k")


class _RacingCompletionFakeS3(_MultipartFakeS3):
    """The interleaving that made erasure LIE (issue #37 review round 16).

    The object is deleted, and then — before the abort lands — the multipart
    upload COMPLETES, materialising a fresh object at the same key. The abort
    therefore reports "already gone" (NoSuchUpload). This store is UNVERSIONED,
    so the version purge cleans nothing up.
    """

    def __init__(self, *, resurrect_forever: bool = False) -> None:
        super().__init__()
        #: When True the store keeps re-creating the object, standing in for a
        #: remedy that cannot succeed — erasure must then REFUSE to confirm.
        self.resurrect_forever = resurrect_forever

    def abort_multipart_upload(self, Bucket, Key, UploadId):  # noqa: N803
        self.aborted.append((Key, UploadId))
        self.uploads = [u for u in self.uploads if u["UploadId"] != UploadId]
        if Key == "k":
            # The completion landed first: the upload id is spent, and its
            # object now exists.
            self.objects[Key] = (b"resurrected-audio", "audio/wav")
            raise _NoSuchUpload()

    def delete_object(self, Bucket, Key):  # noqa: N803
        super().delete_object(Bucket, Key)
        if self.resurrect_forever and Key == "k":
            self.objects[Key] = (b"resurrected-audio", "audio/wav")


class _NoSuchUpload(Exception):
    def __init__(self) -> None:
        super().__init__("NoSuchUpload")
        self.response = {"Error": {"Code": "NoSuchUpload"}}


def test_a_completion_racing_erasure_does_not_survive_it() -> None:
    """Erasure must not confirm over an object that materialised mid-delete.

    `delete_object` runs, the racing completion stores the object, and the
    abort reports "already gone". On an unversioned store nothing downstream
    removes it — `delete_sync` used to return success and the lifecycle marked
    the audio deleted while it was still stored.
    """
    store = S3AudioStore()
    fake = _RacingCompletionFakeS3()
    store._client = fake
    store.put("k", b"x", "audio/wav")

    store.delete_sync("k")

    assert fake.aborted == [("k", "u1")]
    assert "k" not in fake.objects, "the resurrected object survived erasure"


def test_erasure_refuses_to_confirm_what_it_cannot_remove() -> None:
    """When the remedy cannot succeed, erasure RAISES rather than lying.

    The deletion sweep retries (and eventually dead-letters as
    `delete_failed`, key preserved) — which is the honest outcome. Reporting
    success here is how metadata came to claim an erasure that never happened.
    """
    store = S3AudioStore()
    fake = _RacingCompletionFakeS3(resurrect_forever=True)
    store._client = fake
    store.put("k", b"x", "audio/wav")

    with pytest.raises(ErasureUnverified, match="still present after deletion"):
        store.delete_sync("k")
    assert "k" in fake.objects  # and the caller is told, rather than misled


# --------------------------------- the absence signal callers actually depend on


def test_head_missing_object_is_recognised_as_absence(store: S3AudioStore) -> None:
    """`is_object_missing` must recognise what THIS store actually raises.

    The upload-completion probe skips the multipart completion only when the
    object is provably absent. `S3AudioStore.head` converts a 404 into
    ``NotFoundError`` — and while `is_object_missing` knew only the raw boto
    shape (and the in-memory store's ``KeyError``), every REAL S3 completion
    re-raised instead of storing the object. The fake-store tests hid it
    completely, so this one goes through S3AudioStore (issue #37 review r12).
    """
    with pytest.raises(NotFoundError) as excinfo:
        store.head("workspaces/w/never-uploaded.webm")
    assert is_object_missing(excinfo.value) is True

    # A present object heads normally — no absence signal at all.
    store.put("workspaces/w/there.webm", b"audio", "audio/webm")
    assert store.head("workspaces/w/there.webm")["size"] == 5


def test_unknown_store_failures_are_never_read_as_absence(
    store: S3AudioStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout/403/5xx says NOTHING about existence — treating it as absence
    would re-run the completion over an object the store may already hold."""

    class _Throttled(Exception):
        def __init__(self) -> None:
            super().__init__("slow down")
            self.response = {"Error": {"Code": "SlowDown"}}

    def _boom(**_kwargs):
        raise _Throttled()

    monkeypatch.setattr(store._client, "head_object", _boom)
    with pytest.raises(_Throttled) as excinfo:
        store.head("workspaces/w/unknown.webm")
    assert is_object_missing(excinfo.value) is False
