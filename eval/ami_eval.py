#!/usr/bin/env python3
"""Evaluate a running Root Transcribe installation on an AMI corpus meeting.

Downloads one meeting (headset mix + the manual word annotations), uploads the
audio through the normal API, waits for the transcript and compares it with the
reference. Only the Python standard library is needed.

    python3 eval/ami_eval.py                       # ES2002a, first 10 minutes
    python3 eval/ami_eval.py --meeting IS1009b --minutes 0   # a whole meeting

Sign-in uses the same Keycloak login as the browser. The password comes from
RT_PASSWORD or a prompt; it must be your real password (the initial one that has
to be changed at first sign-in does not work here).

Scores (lower is better):
  WER    words wrong, ignoring who said them (approximate where people
         talk over each other, because the word order is then ambiguous)
  cpWER  words wrong when each word must also be under the right speaker
  DER    share of speech time given to the wrong speaker, missed or invented

The AMI Meeting Corpus is licensed CC BY 4.0 (https://groups.inf.ed.ac.uk/ami/corpus/).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import html
import itertools
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

AMI = "https://groups.inf.ed.ac.uk/ami"
AUDIO_URL = AMI + "/AMICorpusMirror/amicorpus/{m}/audio/{m}.Mix-Headset.wav"
ANNOTATIONS_URL = AMI + "/AMICorpusAnnotations/ami_public_manual_1.6.2.zip"
WORKSPACE_NAME = "AMI evaluation"
# Hesitation sounds are transcribed inconsistently by people and models alike;
# scoring them measures style, not accuracy.
FILLERS = frozenset("uh um uhm er erm eh ah hm hmm mm mhm mm-hmm uh-huh mmhmm".split())
FRAME = 0.01  # seconds, for the speaker-time score


class EvalError(Exception):
    """A problem to show the user without a traceback."""


# --------------------------------------------------------------------- download


def download(url: str, target: Path) -> Path:
    """Fetch ``url`` once into the cache. The AMI server answers 403 now and
    then, so a few attempts are normal."""
    if target.exists() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    last = ""
    for attempt in range(1, 6):
        try:
            print(f"Downloading {url}" + (f" (attempt {attempt})" if attempt > 1 else ""))
            with urllib.request.urlopen(url, timeout=120) as response, open(partial, "wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            partial.replace(target)
            return target
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(exc)
            time.sleep(2 * attempt)
    raise EvalError(f"Could not download {url}: {last}")


def trim_wav(source: Path, target: Path, seconds: float) -> float:
    """Copy the first ``seconds`` of a PCM WAV; returns the resulting duration."""
    with wave.open(str(source), "rb") as src:
        rate = src.getframerate()
        frames = src.getnframes() if seconds <= 0 else min(src.getnframes(), int(seconds * rate))
        with wave.open(str(target), "wb") as dst:
            dst.setparams(src.getparams())
            remaining = frames
            while remaining > 0:
                block = src.readframes(min(remaining, rate * 10))
                if not block:
                    break
                dst.writeframes(block)
                remaining -= len(block) // (src.getsampwidth() * src.getnchannels())
    return frames / rate


# -------------------------------------------------------------------- reference


def normalize(text: str) -> list[str]:
    """Lower-case words without punctuation or hesitation sounds."""
    text = text.lower().replace("’", "'")
    words = re.findall(r"[a-z0-9À-ɏ]+(?:['-][a-z0-9À-ɏ]+)*", text)
    return [w for w in words if w not in FILLERS]


def load_reference(annotations: Path, meeting: str, limit: float):
    """Per-speaker words ``(start, end, word)`` from the AMI manual transcript,
    cut at ``limit`` seconds (0 = everything)."""
    speakers: dict[str, list[tuple[float, float, str]]] = {}
    with zipfile.ZipFile(annotations) as archive:
        names = sorted(
            n for n in archive.namelist() if re.fullmatch(rf"words/{re.escape(meeting)}\.[A-Z]\.words\.xml", n)
        )
        if not names:
            raise EvalError(f"No word annotations for meeting {meeting} in {annotations.name}.")
        for name in names:
            speaker = name.split(".")[1]
            root = ET.fromstring(archive.read(name))
            for element in root:
                if element.tag != "w" or element.get("punc") == "true":
                    continue
                start, end = element.get("starttime"), element.get("endtime")
                if start is None or end is None or (limit > 0 and float(start) >= limit):
                    continue
                for word in normalize(element.text or ""):
                    speakers.setdefault(speaker, []).append((float(start), float(end), word))
    return speakers


def to_turns(words: list[tuple[float, float, str]], gap: float = 0.3) -> list[tuple[float, float]]:
    """Merge a speaker's word intervals into speech turns."""
    turns: list[list[float]] = []
    for start, end, _ in sorted(words):
        if turns and start - turns[-1][1] <= gap:
            turns[-1][1] = max(turns[-1][1], end)
        else:
            turns.append([start, end])
    return [(a, b) for a, b in turns]


# ---------------------------------------------------------------------- metrics


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    if not ref or not hyp:
        return len(ref) + len(hyp)
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i]
        for j, h in enumerate(hyp, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return previous[-1]


def best_assignment(cost: list[list[float]]) -> tuple[float, list[int]]:
    """Cheapest one-to-one pairing of rows to columns of a square matrix."""
    n = len(cost)
    if n > 9:  # 10! pairings is too many; meetings this size are not expected
        raise EvalError(f"Too many speakers to score ({n}).")
    best, best_perm = float("inf"), list(range(n))
    for perm in itertools.permutations(range(n)):
        total = sum(cost[i][perm[i]] for i in range(n))
        if total < best:
            best, best_perm = total, list(perm)
    return best, best_perm


def cp_wer(ref: dict[str, list[str]], hyp: dict[str, list[str]]) -> tuple[float, dict[str, str]]:
    """Speaker-attributed word error rate under the best speaker pairing."""
    ref_ids, hyp_ids = list(ref), list(hyp)
    n = max(len(ref_ids), len(hyp_ids))
    rows = [ref[r] for r in ref_ids] + [[] for _ in range(n - len(ref_ids))]
    cols = [hyp[h] for h in hyp_ids] + [[] for _ in range(n - len(hyp_ids))]
    cost = [[float(edit_distance(r, h)) for h in cols] for r in rows]
    errors, perm = best_assignment(cost)
    total = sum(len(words) for words in ref.values())
    mapping = {
        hyp_ids[perm[i]]: ref_ids[i]
        for i in range(len(ref_ids))
        if perm[i] < len(hyp_ids)
    }
    return errors / max(total, 1), mapping


def der(ref: dict[str, list[tuple[float, float]]], hyp: dict[str, list[tuple[float, float]]], duration: float):
    """Diarization error rate on a 10 ms grid, no forgiveness collar."""
    frames = int(duration / FRAME) + 1

    def grid(turns: dict[str, list[tuple[float, float]]]):
        ids = list(turns)
        active = [0] * frames  # bitmask of speakers per frame
        for bit, speaker in enumerate(ids):
            for start, end in turns[speaker]:
                for f in range(max(0, int(start / FRAME)), min(frames, int(end / FRAME))):
                    active[f] |= 1 << bit
        return ids, active

    ref_ids, ref_grid = grid(ref)
    hyp_ids, hyp_grid = grid(hyp)
    n = max(len(ref_ids), len(hyp_ids), 1)
    overlap = [[0.0] * n for _ in range(n)]
    total = missed = false_alarm = matched_slots = 0
    for r, h in zip(ref_grid, hyp_grid):
        nr, nh = bin(r).count("1"), bin(h).count("1")
        total += nr
        missed += max(0, nr - nh)
        false_alarm += max(0, nh - nr)
        matched_slots += min(nr, nh)
        if r and h:
            for i in range(len(ref_ids)):
                if r >> i & 1:
                    for j in range(len(hyp_ids)):
                        if h >> j & 1:
                            overlap[i][j] += 1
    best, _ = best_assignment([[-value for value in row] for row in overlap])
    correct = min(-best, matched_slots)
    confusion = matched_slots - correct
    scale = max(total, 1)
    return {
        "der": (missed + false_alarm + confusion) / scale,
        "missed": missed / scale,
        "false_alarm": false_alarm / scale,
        "confusion": confusion / scale,
    }


# -------------------------------------------------------------------------- API


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """Signed-in API client: Keycloak browser login (PKCE) without a browser."""

    def __init__(self, app_url: str) -> None:
        self.app_url = app_url.rstrip("/")
        handlers: list = [NoRedirect()]
        if urllib.parse.urlsplit(self.app_url).hostname in ("localhost", "127.0.0.1", "::1"):
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.access = ""
        self.refresh = ""
        self.expires = 0.0
        self.token_url = ""
        self.client_id = ""

    def _open(self, request: urllib.request.Request, timeout: float = 60):
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error  # the caller inspects status and body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EvalError(f"Cannot reach {request.full_url.split('?')[0]}: {exc}") from None

    def _token_request(self, fields: dict[str, str]) -> None:
        data = urllib.parse.urlencode({"client_id": self.client_id, **fields}).encode()
        response = self._open(urllib.request.Request(self.token_url, data=data))
        body = response.read().decode("utf-8", "replace")
        if response.status != 200:
            raise EvalError(f"Sign-in was refused when requesting a token (HTTP {response.status}).")
        tokens = json.loads(body)
        self.access = tokens["access_token"]
        self.refresh = tokens.get("refresh_token", "")
        self.expires = time.monotonic() + float(tokens.get("expires_in", 60)) - 30

    def login(self, username: str, password: str) -> None:
        config = self.json("GET", "/config", authenticated=False)
        base = f"{config['keycloak_url'].rstrip('/')}/realms/{config['keycloak_realm']}/protocol/openid-connect"
        self.client_id = config["keycloak_client_id"]
        self.token_url = base + "/token"
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        redirect_uri = self.app_url + "/"
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid",
                "state": secrets.token_urlsafe(8),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        page = self._open(urllib.request.Request(f"{base}/auth?{query}"))
        # Keycloak marks its cookies Secure even on http://localhost. Browsers
        # send them back there; Python's cookie jar would not, so pass them on.
        cookies = "; ".join(
            value.split(";", 1)[0] for name, value in page.headers.items() if name.lower() == "set-cookie"
        )
        form = re.search(r'<form[^>]*\baction="([^"]+)"', page.read().decode("utf-8", "replace"))
        if page.status != 200 or not form:
            raise EvalError(f"The sign-in page did not load as expected (HTTP {page.status}).")
        data = urllib.parse.urlencode({"username": username, "password": password}).encode()
        result = self._open(
            urllib.request.Request(html.unescape(form.group(1)), data=data, headers={"Cookie": cookies})
        )
        location = result.headers.get("Location", "")
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).get("code", [""])[0]
        if result.status not in (302, 303) or not code:
            if result.status in (302, 303) or "UPDATE_PASSWORD" in location:
                raise EvalError(
                    "Sign-in needs a step this script cannot do (for example changing the "
                    "initial password). Sign in once in the browser, then run this again."
                )
            raise EvalError("Sign-in failed: wrong user name or password.")
        self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}
        )

    def _headers(self) -> dict[str, str]:
        if self.token_url and time.monotonic() >= self.expires:
            self._token_request({"grant_type": "refresh_token", "refresh_token": self.refresh})
        return {"Authorization": f"Bearer {self.access}"}

    def json(self, method: str, path: str, body=None, *, authenticated: bool = True, raw: bytes | None = None,
             content_type: str = "application/json", timeout: float = 60):
        headers = self._headers() if authenticated else {}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(f"{self.app_url}/api/v1{path}", data=data, headers=headers, method=method)
        response = self._open(request, timeout=timeout)
        text = response.read().decode("utf-8", "replace")
        if response.status >= 400:
            try:
                detail = json.loads(text).get("detail", text)
            except (ValueError, AttributeError):
                detail = text[:200]
            raise EvalError(f"{method} {path} failed (HTTP {response.status}): {detail}")
        return json.loads(text) if text else None

    def upload(self, workspace_id: str, wav: Path, title: str, language: str):
        boundary = uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in (("title", title), ("participant_consent", "true"), ("requested_language", language))
        ]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{wav.name}"\r\n'
            "Content-Type: audio/wav\r\n\r\n".encode()
        )
        parts += [wav.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
        return self.json(
            "POST",
            f"/workspaces/{workspace_id}/recordings",
            raw=b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=600,
        )


# ------------------------------------------------------------------------- main


def run(args: argparse.Namespace) -> int:
    cache = Path(args.cache).expanduser()
    meeting = args.meeting
    limit = args.minutes * 60.0
    source = download(AUDIO_URL.format(m=meeting), cache / f"{meeting}.Mix-Headset.wav")
    annotations = download(ANNOTATIONS_URL, cache / "ami_public_manual_1.6.2.zip")
    label = f"{meeting}-{args.minutes:g}min" if limit > 0 else f"{meeting}-full"
    clip = cache / f"{label}.wav"
    duration = trim_wav(source, clip, limit)
    reference = load_reference(annotations, meeting, limit)
    ref_words = sum(len(words) for words in reference.values())
    print(f"Reference: {duration / 60:.1f} min, {len(reference)} speakers, {ref_words} words.")
    if ref_words == 0:
        raise EvalError("The reference has no words in this part of the meeting; use more minutes.")

    client = Client(args.url)
    if os.environ.get("RT_TOKEN"):
        client.access, client.expires = os.environ["RT_TOKEN"], float("inf")
    else:
        password = os.environ.get("RT_PASSWORD") or getpass.getpass(f"Password for {args.username}: ")
        client.login(args.username, password)
    print(f"Signed in to {client.app_url}.")

    workspaces = client.json("GET", "/workspaces/mine")
    workspace = next((w for w in workspaces if w["name"] == WORKSPACE_NAME), None) or client.json(
        "POST", "/workspaces", {"name": WORKSPACE_NAME, "description": "Recordings uploaded by eval/ami_eval.py"}
    )
    started = time.monotonic()
    title = f"AMI {label} {time.strftime('%Y-%m-%d %H:%M')}"
    recording = client.upload(workspace["id"], clip, title, args.language)
    path = f"/workspaces/{workspace['id']}/recordings/{recording['id']}"
    print(f"Uploaded as “{title}”. Waiting for the transcript (limit {args.timeout // 60} min)...")
    step = None
    while True:
        detail = client.json("GET", path)
        if detail["status"] in ("completed", "failed", "cancelled"):
            break
        if detail.get("current_step") != step:
            step = detail.get("current_step")
            print(f"  {time.monotonic() - started:5.0f}s  {detail['status']}: {step or '-'}")
        if time.monotonic() - started > args.timeout:
            raise EvalError(f"No transcript after {args.timeout // 60} minutes; the recording is still {detail['status']}.")
        time.sleep(float(os.environ.get("RT_POLL_SECONDS", "5")))
    elapsed = time.monotonic() - started
    if detail["status"] != "completed":
        raise EvalError(f"Transcription {detail['status']}: {detail.get('error') or detail.get('error_code') or 'no reason given'}")

    segments = detail["segments"]
    hyp_by_speaker: dict[str, list[str]] = {}
    hyp_turns: dict[str, list[tuple[float, float]]] = {}
    for segment in segments:
        speaker = segment["speaker_label"]
        hyp_by_speaker.setdefault(speaker, []).extend(normalize(segment["text"]))
        hyp_turns.setdefault(speaker, []).append((segment["start_ms"] / 1000, segment["end_ms"] / 1000))
    hyp_all = [word for segment in segments for word in normalize(segment["text"])]
    # Reference order = turn by turn, like the transcript. Where people talk
    # over each other the order is still ambiguous, so WER is approximate there;
    # cpWER does not depend on it.
    ref_turn_words = []
    for words in reference.values():
        ordered = sorted(words)
        for start, end in to_turns(ordered):
            ref_turn_words.append((start, [w for s, e, w in ordered if start <= s and e <= end + 1e-6]))
    ref_all = [word for _, turn in sorted(ref_turn_words, key=lambda t: t[0]) for word in turn]
    ref_by_speaker = {speaker: [w for _, _, w in sorted(words)] for speaker, words in reference.items()}

    wer = edit_distance(ref_all, hyp_all) / len(ref_all)
    cpwer, mapping = cp_wer(ref_by_speaker, hyp_by_speaker)
    speaker_time = der({s: to_turns(w) for s, w in reference.items()}, hyp_turns, duration)
    result = {
        "meeting": meeting,
        "minutes": round(duration / 60, 2),
        "language": args.language,
        "reference_speakers": len(reference),
        "detected_speakers": len(hyp_by_speaker),
        "reference_words": len(ref_all),
        "transcript_words": len(hyp_all),
        "wer": round(wer, 4),
        "cpwer": round(cpwer, 4),
        **{key: round(value, 4) for key, value in speaker_time.items()},
        "processing_seconds": round(elapsed, 1),
        "realtime_factor": round(elapsed / duration, 3),
        "speaker_mapping": mapping,
        "recording_id": recording["id"],
    }
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{label}-{stamp}.json").write_text(json.dumps(result, indent=2) + "\n")
    (out / f"{label}-{stamp}.transcript.txt").write_text(
        "".join(f"[{s['start_ms'] / 1000:7.1f}] {s['speaker_label']}: {s['text']}\n" for s in segments)
    )
    (out / f"{label}.reference.txt").write_text(
        "".join(f"{speaker}: {' '.join(words)}\n" for speaker, words in ref_by_speaker.items())
    )

    print(f"""
Result for {meeting} ({duration / 60:.1f} min, language {args.language})
  Speakers          {len(hyp_by_speaker)} detected, {len(reference)} in the reference
  Words             {len(hyp_all)} transcribed, {len(ref_all)} in the reference
  WER               {wer:6.1%}   words wrong, ignoring speakers
  cpWER             {cpwer:6.1%}   words wrong or under the wrong speaker
  DER               {speaker_time['der']:6.1%}   speech time: missed {speaker_time['missed']:.1%}, invented {speaker_time['false_alarm']:.1%}, wrong speaker {speaker_time['confusion']:.1%}
  Processing time   {elapsed:.0f}s ({elapsed / duration:.2f}x the audio length)
Details saved in {out}/. The recording stays in the “{WORKSPACE_NAME}” workspace.""")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Score a running Root Transcribe installation on an AMI meeting.")
    parser.add_argument("--url", default="http://localhost:8095", help="application address (default: %(default)s)")
    parser.add_argument("--username", default="admin@example.com", help="sign-in name (default: %(default)s)")
    parser.add_argument("--meeting", default="ES2002a", help="AMI meeting id (default: %(default)s)")
    parser.add_argument("--minutes", type=float, default=10, help="use the first N minutes; 0 = the whole meeting (default: %(default)s)")
    parser.add_argument("--language", default="en", choices=("en", "auto"), help="language sent with the upload (default: %(default)s)")
    parser.add_argument("--timeout", type=int, default=3600, help="seconds to wait for the transcript (default: %(default)s)")
    parser.add_argument("--cache", default="~/.cache/root-transcribe-eval", help="where downloads are kept (default: %(default)s)")
    parser.add_argument("--output", default="eval/results", help="where results are written (default: %(default)s)")
    try:
        return run(parser.parse_args())
    except EvalError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
