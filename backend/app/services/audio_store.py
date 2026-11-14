"""Storage seam for meeting audio blobs.

Meeting recordings are large (tens–hundreds of MB), so their bytes live in an
**S3-compatible object store** rather than the database or a shared PVC — both the
API (upload + authenticated playback proxy) and the Celery worker (download for
transcription) reach the store over the network, so no shared filesystem is
assumed. **Where** the bytes live is hidden behind :class:`AudioStore` so the
backend can change without touching the routes, the worker task, or the model.

The backend is :class:`S3AudioStore` (boto3 against an S3-compatible endpoint):
the Podman demo bundles SeaweedFS with its S3 API on port 8333;
``AUDIO_S3_ENDPOINT_URL`` selects the store for other deployments. A future
backend only needs to implement :class:`AudioStore`; the ``key`` it returns from
:meth:`put` is opaque to every caller.

The interface follows the codebase's sync-worker / async-API split, but boto3 is
itself synchronous, so the "async" methods run the blocking call in a thread via
``anyio.to_thread`` to avoid stalling the event loop. See ``ARCHITECTURE.md``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

import anyio

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.logging import log_operation

logger = logging.getLogger("app.services.audio_store")

#: How many abort/verify rounds erasure runs before giving up on a key. Each
#: round aborts every listed upload and re-checks it with ListParts; a writer
#: that keeps pushing parts makes erasure RAISE rather than lie (issue #37 r17).
_ABORT_ROUNDS = 3


class ErasureUnverified(RuntimeError):
    """Erasure could not be PROVEN — never that it definitely failed.

    Distinct from an ordinary store error because `delete_sync` treats a
    not-found as idempotent success: without its own type, a verification
    failure shaped like a 404 (a store with no ListMultipartUploads answers
    exactly that) would be reclassified as "already gone" and confirm the very
    erasure it could not establish (issue #37 review round 17b).
    """


@runtime_checkable
class AudioStore(Protocol):
    """A pluggable blob store for meeting audio."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store audio bytes under ``key`` and return the stored key (sync)."""
        ...

    async def get(self, key: str) -> bytes:
        """Return the stored bytes for ``key`` (async)."""
        ...

    def get_sync(self, key: str) -> bytes:
        """Return the stored bytes for ``key`` (sync, worker path)."""
        ...

    def stream_sync(self, key: str):
        """Return an iterator of byte chunks for ``key`` (playback proxy)."""
        ...

    async def delete(self, key: str) -> None:
        """Delete a blob; an already-gone key is a no-op, any other failure
        **raises** so callers can record a durable retry (async)."""
        ...

    def delete_sync(self, key: str) -> None:
        """Delete a blob; an already-gone key is a no-op, any other failure
        **raises** so callers can record a durable retry (sync, worker)."""
        ...

    def put_file(self, key: str, fileobj, content_type: str) -> str:
        """Stream a file-like object into storage (bounded memory; sync)."""
        ...

    def head(self, key: str) -> dict:
        """Object metadata: ``{"size": int, "etag": str, "content_type": str}``."""
        ...

    def get_range(self, key: str, start: int, end: int):
        """Iterator over one inclusive byte range (Range playback proxy)."""
        ...

    def download_to_path(self, key: str, path: str) -> None:
        """Stream a blob to a local file (worker; never whole-object bytes)."""
        ...

    def create_multipart(self, key: str, content_type: str) -> str:
        """Start a multipart upload; returns the upload id (sync)."""
        ...

    def upload_part(
        self, key: str, upload_id: str, part_number: int, data: bytes
    ) -> str:
        """Upload one part; returns its ETag (sync)."""
        ...

    def complete_multipart(
        self, key: str, upload_id: str, parts: list[tuple[int, str]]
    ) -> None:
        """Complete a multipart upload from ``[(part_number, etag), …]``."""
        ...

    def abort_multipart(self, key: str, upload_id: str) -> bool:
        """Abort a multipart upload and discard its parts (idempotent).

        Returns True when this call actually discarded a LIVE upload, and False
        when the upload was ALREADY GONE. That distinction is load-bearing:
        "already gone" can mean "completed", so a caller that reads it as
        "nothing is there" may delete the last database owner of an object
        that now exists (issue #37 review round 15). Use
        :func:`discard_multipart` rather than interpreting it by hand.
        """
        ...

    def list_multipart_parts(self, key: str, upload_id: str) -> list[dict]:
        """Parts currently held by one multipart upload (``[]`` when gone).

        Erasure needs this: aborting is NOT proof that the parts are gone. An
        ``UploadPart`` already in flight can land after the abort and leave its
        part behind, which AWS documents explicitly — the caller must verify
        with ListParts and abort again (issue #37 review round 17).
        """
        ...

    def iter_multipart_uploads(self, *, prefix: str = "") -> Iterator[dict]:
        """Stream in-progress multipart uploads: ``{key, upload_id, initiated}``.

        Used by the expiry sweep to reconcile uploads the DB never learned
        about (a crash between the session commit and the upload-id stamp).
        ``prefix`` narrows the listing; ownership is proven per key against
        this deployment's rows before anything is aborted.
        """
        ...


def _suffix(filename: str) -> str:
    """File extension for the object key — sanitized so a hostile filename can't
    inject path separators or unbounded text into the key."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webm"
    ext = "".join(ch for ch in ext if ch.isalnum())[:8]
    return ext or "webm"


def key_namespace() -> str:
    """The deployment's key-prefix namespace (``AUDIO_S3_KEY_PREFIX``).

    Deployments sharing a bucket MUST set disjoint prefixes — including one
    created from a database clone of another (review round 6): a clone copies
    the rows that exact-key ownership checks consult, so only a disjoint key
    namespace truly isolates their reconciliation sweeps.
    """
    prefix = (settings.AUDIO_S3_KEY_PREFIX or "").strip("/")
    return f"{prefix}/" if prefix else ""


def new_audio_key(workspace_id: uuid.UUID, filename: str) -> str:
    """Build a collision-free object key namespaced by workspace."""
    return (
        f"{key_namespace()}workspaces/{workspace_id}/"
        f"{uuid.uuid4().hex}.{_suffix(filename)}"
    )


def new_voiceprint_key(profile_id: uuid.UUID, filename: str) -> str:
    """Build a collision-free object key for a voiceprint enrollment sample."""
    return (
        f"{key_namespace()}speakers/{profile_id}/{uuid.uuid4().hex}.{_suffix(filename)}"
    )


class S3AudioStore:
    """:class:`AudioStore` backed by any S3-compatible endpoint (boto3).

    ``key`` is the S3 object key. Writes attempt bucket creation on first use
    if it is missing; the Podman deployment provisions it before app startup.
    """

    def __init__(self) -> None:
        self._client = None  # lazily built (keeps import + worker fork cheap)
        self._bucket_ready = False

    def _s3(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            if not settings.AUDIO_S3_ENDPOINT_URL:
                raise RuntimeError(
                    "AUDIO_S3_ENDPOINT_URL is not configured; the meeting-audio "
                    "object store is unavailable."
                )
            self._client = boto3.client(
                "s3",
                endpoint_url=settings.AUDIO_S3_ENDPOINT_URL,
                aws_access_key_id=settings.AUDIO_S3_ACCESS_KEY,
                aws_secret_access_key=settings.AUDIO_S3_SECRET_KEY,
                region_name=settings.AUDIO_S3_REGION,
                # Explicit short connect/read timeouts (issue #14 review): a
                # governance action must never wait on a hung object store —
                # bound the failure instead of retrying against a black hole.
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 3},
                    connect_timeout=5,
                    read_timeout=30,
                ),
            )
        return self._client

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        client = self._s3()
        bucket = settings.AUDIO_S3_BUCKET
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001 — any miss ⇒ try to create it
            try:
                client.create_bucket(Bucket=bucket)
            except Exception:  # noqa: BLE001 — race / already-exists is fine
                logger.debug("bucket %s create raced or already exists", bucket)
        self._bucket_ready = True

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self._ensure_bucket()
        with log_operation(logger, "audio.put", key=key, bytes=len(data)):
            self._s3().put_object(
                Bucket=settings.AUDIO_S3_BUCKET,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return key

    def get_sync(self, key: str) -> bytes:
        try:
            with log_operation(logger, "audio.get", key=key):
                obj = self._s3().get_object(Bucket=settings.AUDIO_S3_BUCKET, Key=key)
                return obj["Body"].read()
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise NotFoundError("Audio not found") from exc
            raise

    def stream_sync(self, key: str):
        """Yield the blob in chunks (playback proxy) — a meeting can be hundreds
        of MB, so the API must never buffer the whole file in memory."""
        try:
            obj = self._s3().get_object(Bucket=settings.AUDIO_S3_BUCKET, Key=key)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise NotFoundError("Audio not found") from exc
            raise
        logger.debug("audio.stream start", extra={"key": key})
        return obj["Body"].iter_chunks(chunk_size=256 * 1024)

    async def get(self, key: str) -> bytes:
        return await anyio.to_thread.run_sync(self.get_sync, key)

    def delete_sync(self, key: str) -> list[str]:
        """Delete ``key`` everywhere; returns the version ids purged.

        Raising on ANY doubt is the contract: the deletion sweep only
        confirms erasure (and lets owners claim it) when this returns.
        """
        try:
            with log_operation(logger, "audio.delete", key=key):
                self._s3().delete_object(Bucket=settings.AUDIO_S3_BUCKET, Key=key)
                # Erasing the object is not enough (review round 5): an
                # in-progress multipart upload holds its PARTS outside the
                # object namespace — a workspace deleted mid-upload would
                # report erasure while sensitive audio parts survive. Abort
                # every open upload for this exact key; a failure raises so
                # the sweep retries instead of confirming a partial erasure.
                raced = self._abort_uploads_for_key_sync(key)
                # On a VERSIONED bucket a plain delete only writes a delete
                # marker — every historical version of the audio survives,
                # which is not erasure. Enumerate and delete all versions and
                # markers for this exact key so "deleted" means gone
                # everywhere (review finding: version-aware erasure).
                versions = self._delete_all_versions_sync(key)
                return versions + self._confirm_erased_sync(key, raced=raced)
        except ErasureUnverified:
            # Our OWN "could not prove it" — must never be reclassified as the
            # idempotent-success case below, whatever shape it wears.
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                # Already gone ⇒ the deletion goal is met (idempotent success).
                logger.debug("audio %s already deleted", key)
                return []
            # Anything else (outage, credentials, permissions, throttling) must
            # surface — a swallowed failure here is how metadata used to claim
            # erasure that never happened (issue #6).
            raise

    def _upload_ids_for_key_sync(self, key: str) -> list[str]:
        """Ids of every in-progress multipart upload of exactly ``key``.

        A listing failure RAISES — including the ``NotImplemented``-style
        not-found some minimal stores return for ListMultipartUploads. Without
        the listing there is no way to establish that the multipart namespace is
        empty, and the object HEAD cannot stand in for it because parts are not
        objects. Treating "I can't look" as "nothing is there" is precisely how
        erasure came to confirm over stored audio (issue #37 review r17b), so a
        store that cannot be verified fails the deletion instead — visibly, as
        `delete_failed` with the key preserved.
        """
        try:
            paginator = self._s3().get_paginator("list_multipart_uploads")
            return [
                entry["UploadId"]
                for page in paginator.paginate(
                    Bucket=settings.AUDIO_S3_BUCKET, Prefix=key
                )
                for entry in page.get("Uploads", [])
                if entry["Key"] == key
            ]
        except Exception as exc:  # noqa: BLE001 — re-typed, never swallowed
            raise ErasureUnverified(
                f"cannot list multipart uploads for {key!r}: {type(exc).__name__}"
            ) from exc

    def _abort_uploads_for_key_sync(self, key: str) -> bool:
        """Erase the MULTIPART namespace for ``key``, and PROVE it is empty.

        Part of erasure (review round 5): uploaded parts live outside the
        object namespace until complete/abort, so deleting the object alone
        can leave audio behind.

        A single successful abort is not proof (issue #37 review round 17).
        AWS documents that an ``UploadPart`` already in flight can complete
        AFTER ``AbortMultipartUpload`` and leave its part stored, and requires
        callers to verify with ListParts and abort again. The final object HEAD
        cannot see that: parts are not in the object namespace, so erasure was
        able to report success while sensitive audio parts remained.

        So: abort every upload for the key, verify each with ListParts, abort
        again where parts survived, and re-list — until a round finds nothing.
        If uploads persist past ``_ABORT_ROUNDS``, RAISE: a writer is still
        pushing parts (the caller fences the session, so this converges), and
        the deletion sweep retrying is the only honest outcome.

        Returns True when an upload we listed had ALREADY GONE by the time we
        aborted it — which can mean it COMPLETED, materialising a fresh object
        at this key after we deleted it (issue #37 review round 16). The caller
        must not confirm erasure on that signal alone.
        """
        raced = False
        for _round in range(_ABORT_ROUNDS):
            upload_ids = self._upload_ids_for_key_sync(key)
            if not upload_ids:
                return raced  # PROVEN: no multipart storage for this key
            for upload_id in upload_ids:
                raced = (not self.abort_multipart(key, upload_id)) or raced
                if self.list_multipart_parts(key, upload_id):
                    # A part landed after the abort — abort what it left.
                    self.abort_multipart(key, upload_id)
        raise ErasureUnverified(
            f"multipart uploads for {key!r} still present after "
            f"{_ABORT_ROUNDS} abort rounds; refusing to report a confirmed erasure"
        )

    def _confirm_erased_sync(self, key: str, *, raced: bool) -> list[str]:
        """Prove the key is GONE before erasure may be reported as done.

        A multipart completion can materialise the object at any point during
        the sequence above — after ``delete_object`` and before the abort (the
        abort then reports "already gone"), or before the listing even ran, in
        which case there is no upload to abort and no signal at all. On an
        UNVERSIONED store the version purge does not clean that up, so
        ``delete_sync`` used to return success over an object that was still
        stored, and the lifecycle then recorded a confirmed erasure that never
        happened (issue #37 review round 16).

        So: look. If something is there, delete it again and look once more;
        still there ⇒ RAISE, because the deletion sweep retrying is the only
        honest outcome. Returns any extra version ids purged by the remedy.
        """
        if raced:
            logger.info(
                "multipart upload for %s vanished during erasure; re-checking", key
            )
        if head_or_none(self, key) is None:
            return []
        self._s3().delete_object(Bucket=settings.AUDIO_S3_BUCKET, Key=key)
        extra = self._delete_all_versions_sync(key)
        if head_or_none(self, key) is not None:
            raise ErasureUnverified(
                f"object {key!r} still present after deletion; refusing to "
                "report a confirmed erasure"
            )
        return extra

    def _delete_all_versions_sync(self, key: str) -> list[str]:
        """Purge every version + delete marker of ``key``; returns their ids.

        Review round 2 hardening: an enumeration failure is only tolerated
        when the bucket is verifiably UNVERSIONED (the API simply isn't
        supported by some stores); on a versioned bucket it raises so
        the sweep never confirms an erasure it can't prove. Per-version
        failures inside the bulk delete raise too (S3 returns HTTP 200 with
        an ``Errors`` list).
        """
        s3 = self._s3()
        deleted: list[str] = []
        try:
            paginator = s3.get_paginator("list_object_versions")
            pages = list(
                paginator.paginate(Bucket=settings.AUDIO_S3_BUCKET, Prefix=key)
            )
        except Exception as exc:  # noqa: BLE001
            if self._bucket_is_versioned():
                raise RuntimeError(
                    f"cannot enumerate versions of {key!r} on a versioned bucket"
                ) from exc
            return deleted  # unversioned store without the API — nothing hidden
        for page in pages:
            doomed = [
                {"Key": entry["Key"], "VersionId": entry["VersionId"]}
                for section in ("Versions", "DeleteMarkers")
                for entry in page.get(section, [])
                if entry.get("Key") == key
                and entry.get("VersionId")
                and entry["VersionId"] != "null"
            ]
            if not doomed:
                continue
            response = s3.delete_objects(
                Bucket=settings.AUDIO_S3_BUCKET,
                Delete={"Objects": doomed, "Quiet": True},
            )
            errors = response.get("Errors") or []
            if errors:
                first = errors[0]
                raise RuntimeError(
                    f"version deletion failed for {key!r}: "
                    f"{first.get('Code')} {first.get('Message')} "
                    f"(+{len(errors) - 1} more)"
                )
            deleted.extend(entry["VersionId"] for entry in doomed)
        return deleted

    def _bucket_is_versioned(self) -> bool:
        try:
            out = self._s3().get_bucket_versioning(Bucket=settings.AUDIO_S3_BUCKET)
        except Exception:  # noqa: BLE001 — can't tell ⇒ assume the worst
            return True
        return out.get("Status") in ("Enabled", "Suspended")

    async def delete(self, key: str) -> None:
        await anyio.to_thread.run_sync(self.delete_sync, key)

    # --- Multipart + streaming (issue #6, wave B1) --------------------------

    def put_file(self, key: str, fileobj, content_type: str) -> str:
        """Stream a file-like object into storage (boto3 handles chunking)."""
        self._ensure_bucket()
        with log_operation(logger, "audio.put_file", key=key):
            self._s3().upload_fileobj(
                fileobj,
                settings.AUDIO_S3_BUCKET,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        return key

    def head(self, key: str) -> dict:
        try:
            meta = self._s3().head_object(Bucket=settings.AUDIO_S3_BUCKET, Key=key)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise NotFoundError("Audio not found") from exc
            raise
        return {
            "size": int(meta["ContentLength"]),
            "etag": (meta.get("ETag") or "").strip('"'),
            "content_type": meta.get("ContentType") or "application/octet-stream",
        }

    def download_to_path(self, key: str, path: str) -> None:
        try:
            with (
                log_operation(logger, "audio.download_file", key=key),
                open(path, "wb") as fh,
            ):
                self._s3().download_fileobj(settings.AUDIO_S3_BUCKET, key, fh)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise NotFoundError("Audio not found") from exc
            raise

    def get_range(self, key: str, start: int, end: int):
        try:
            obj = self._s3().get_object(
                Bucket=settings.AUDIO_S3_BUCKET,
                Key=key,
                Range=f"bytes={start}-{end}",
            )
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise NotFoundError("Audio not found") from exc
            raise
        return obj["Body"].iter_chunks(chunk_size=256 * 1024)

    def create_multipart(self, key: str, content_type: str) -> str:
        self._ensure_bucket()
        with log_operation(logger, "audio.multipart_create", key=key):
            out = self._s3().create_multipart_upload(
                Bucket=settings.AUDIO_S3_BUCKET, Key=key, ContentType=content_type
            )
        return out["UploadId"]

    def upload_part(
        self, key: str, upload_id: str, part_number: int, data: bytes
    ) -> str:
        out = self._s3().upload_part(
            Bucket=settings.AUDIO_S3_BUCKET,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
        )
        return (out.get("ETag") or "").strip('"')

    def complete_multipart(
        self, key: str, upload_id: str, parts: list[tuple[int, str]]
    ) -> None:
        with log_operation(logger, "audio.multipart_complete", key=key):
            self._s3().complete_multipart_upload(
                Bucket=settings.AUDIO_S3_BUCKET,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": n, "ETag": etag} for n, etag in sorted(parts)
                    ]
                },
            )

    def iter_multipart_uploads(self, *, prefix: str = "") -> Iterator[dict]:
        """Stream every in-progress multipart upload under ``prefix``.

        A generator (review round 5): the boto paginator lazily follows the
        key/upload-id continuation markers, so the consumer sees EVERY upload
        with memory bounded by one page — no truncation cap that could
        permanently starve entries past it under sustained load.
        """
        paginator = self._s3().get_paginator("list_multipart_uploads")
        kwargs: dict = {"Bucket": settings.AUDIO_S3_BUCKET}
        if prefix:
            kwargs["Prefix"] = prefix
        for page in paginator.paginate(**kwargs):
            for entry in page.get("Uploads", []):
                yield {
                    "key": entry["Key"],
                    "upload_id": entry["UploadId"],
                    "initiated": entry.get("Initiated"),
                }

    def list_multipart_parts(self, key: str, upload_id: str) -> list[dict]:
        try:
            response = self._s3().list_parts(
                Bucket=settings.AUDIO_S3_BUCKET, Key=key, UploadId=upload_id
            )
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise
            if _is_not_found(exc) or "NoSuchUpload" in str(exc):
                return []  # the upload is gone ⇒ so are its parts
            raise
        return list(response.get("Parts", []))

    def abort_multipart(self, key: str, upload_id: str) -> bool:
        try:
            self._s3().abort_multipart_upload(
                Bucket=settings.AUDIO_S3_BUCKET, Key=key, UploadId=upload_id
            )
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise
            if _is_not_found(exc) or "NoSuchUpload" in str(exc):
                return False  # already gone — the CALLER must reconcile
            raise
        return True


class MultipartDisposition(StrEnum):
    """What became of a multipart upload we tried to discard."""

    #: A live upload was aborted. Nothing was ever stored at the key.
    DISCARDED = "discarded"
    #: We cannot prove the key is empty — the upload had already completed, or
    #: it vanished between our probe and our abort. The caller MUST leave a
    #: durable tombstone so the deletion sweep drives the key to a confirmed
    #: end state instead of leaving an object nothing points at.
    NEEDS_TOMBSTONE = "needs_tombstone"


def head_or_none(store, key: str) -> dict | None:
    """``store.head`` that answers "definitely not there", or re-raises.

    Only a genuine not-found may mean "absent" (issue #37 review round 11): a
    timeout, 403 or 5xx says NOTHING about whether the object exists, and
    treating it as absence would destroy or duplicate real data. An unknown
    outcome propagates instead, leaving the caller's request retryable without
    repeating the side effect.
    """
    try:
        return store.head(key)
    except Exception as exc:  # noqa: BLE001 — narrowed immediately below
        if is_object_missing(exc):
            return None
        raise


def discard_multipart(store, key: str, upload_id: str) -> MultipartDisposition:
    """Abort a multipart upload WITHOUT ever orphaning a completed object.

    Probe-then-abort is racy on its own (issue #37 review round 15): a stale
    completion can land between the HEAD and the abort, the abort then reports
    "already gone", and a caller that reads that as "nothing was there" deletes
    the last database owner of an object that now exists — unreachable by the
    reconciler, which only looks at keys some row still claims.

    So the abort's "already gone" is treated as a QUESTION, not an answer: probe
    again, and fall back to a tombstone whenever absence cannot be proven. Every
    caller that destroys an upload — cancellation and the expiry sweep alike —
    goes through here, so the ordering is fixed in one place.

    Raises whatever the store raised on an INDETERMINATE probe: nothing may be
    destroyed on a guess.
    """
    if head_or_none(store, key) is not None:
        # Already complete: aborting would only report NoSuchUpload.
        return MultipartDisposition.NEEDS_TOMBSTONE
    if store.abort_multipart(key, upload_id):
        # An UploadPart already in flight can land AFTER the abort and leave
        # its part stored (AWS documents this). Verify, and abort what a late
        # writer left behind (issue #37 review round 17).
        if store.list_multipart_parts(key, upload_id):
            store.abort_multipart(key, upload_id)
            if store.list_multipart_parts(key, upload_id):
                # A writer is still pushing. Cancellation must not fail over
                # that, but it must not pretend the parts are gone either:
                # hand the key to the deletion pipeline, whose abort/verify
                # loop runs behind a fence and converges.
                return MultipartDisposition.NEEDS_TOMBSTONE
        return MultipartDisposition.DISCARDED
    # The upload id was consumed by SOMETHING between our probe and our abort.
    # If that was a completion the object is there now; if it was an earlier
    # abort or expiry, nothing is. Look again — and when the second probe still
    # says "absent", fail CLOSED: a tombstone over an empty key costs one
    # confirmed-deletion cycle, while a missed one leaks audio forever.
    return MultipartDisposition.NEEDS_TOMBSTONE


def _is_not_found(exc: Exception) -> bool:
    """True when a boto3 error is a 404 / NoSuchKey."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in {"404", "NoSuchKey", "NotFound"}


def is_object_missing(exc: BaseException) -> bool:
    """Whether ``exc`` proves an object is ABSENT, as opposed to unknown.

    Callers that skip an expensive/irreversible step when an object already
    exists must not treat a timeout, 403 or 5xx as "absent" — that would repeat
    the side effect (issue #37 review round 11). Only a definite not-found
    counts.

    ``NotFoundError`` FIRST: that is what :meth:`S3AudioStore.head` actually
    raises for a missing object, so omitting it made every real S3 completion
    re-raise instead of storing the object — a break the in-memory test store
    (which raises ``KeyError``) hid completely (review round 12).
    """
    if isinstance(exc, NotFoundError | KeyError):
        return True
    return isinstance(exc, Exception) and _is_not_found(exc)


_audio_store: AudioStore | None = None


def get_audio_store() -> AudioStore:
    """Return the process-wide :class:`AudioStore` (FastAPI dependency / worker)."""
    global _audio_store
    if _audio_store is None:
        _audio_store = S3AudioStore()
    return _audio_store
