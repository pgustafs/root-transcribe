"""Calibrated speaker-identification decisions (issue #6, wave A4).

Pure decision logic — no DB, no torch — so the false-accept/false-reject
behavior can be tested without loading speech models.

The pre-#6 pipeline embedded one longest turn per speaker and accepted the
single best cosine score over a fixed 0.55 threshold. Automatic *mis*naming is
worse than anonymity (it corrupts the meeting record, action ownership, and
downstream answers), so the gate is now:

- several clean turns per speaker (min duration, capped count), scored as the
  mean of the top per-turn similarities against each profile centroid;
- auto-assign only when the best score clears the absolute threshold AND the
  top-1/top-2 margin clears the calibrated margin;
- otherwise the speaker stays ``Speaker N`` and, if the score clears the
  suggest floor, the best candidate goes to the human review queue.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.services.speaker_directory import cosine_similarity

# How many of a speaker's best per-turn scores feed the profile score.
TOP_TURN_SCORES = 3


@dataclass(frozen=True)
class MatchDecision:
    """Outcome for one diarized speaker label."""

    # Auto-assigned profile (score + margin both cleared), else None.
    profile_id: uuid.UUID | None
    score: float
    # top-1 − top-2 score (1.0 when there is only one candidate).
    margin: float
    # Best candidate for the human review queue when not auto-assigned and the
    # score clears the suggest floor.
    suggestion_profile_id: uuid.UUID | None
    sample_turns: int


def select_turns(
    turns: list, label: str, *, min_seconds: float, max_turns: int
) -> list:
    """Pick up to ``max_turns`` of a speaker's longest turns ≥ ``min_seconds``.

    Longer turns carry more voice evidence; sub-second fragments are noise.
    """
    mine = [t for t in turns if t.speaker == label and (t.end - t.start) >= min_seconds]
    mine.sort(key=lambda t: t.end - t.start, reverse=True)
    return mine[:max_turns]


def score_profiles(
    turn_embeddings: list[list[float]],
    centroids: dict[uuid.UUID, list[float]],
) -> dict[uuid.UUID, float]:
    """Per-profile score: mean of the top per-turn cosine similarities."""
    scores: dict[uuid.UUID, float] = {}
    for profile_id, center in centroids.items():
        per_turn = sorted(
            (cosine_similarity(emb, center) for emb in turn_embeddings),
            reverse=True,
        )
        best = per_turn[:TOP_TURN_SCORES]
        scores[profile_id] = sum(best) / len(best) if best else 0.0
    return scores


def decide(
    scores: dict[uuid.UUID, float],
    *,
    threshold: float,
    margin: float,
    suggest_threshold: float,
    sample_turns: int,
) -> MatchDecision:
    """Apply the calibrated auto-assign gate to per-profile scores."""
    if not scores:
        return MatchDecision(None, 0.0, 0.0, None, sample_turns)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_id, top_score = ranked[0]
    top_margin = top_score - ranked[1][1] if len(ranked) > 1 else 1.0
    if top_score >= threshold and top_margin >= margin:
        return MatchDecision(top_id, top_score, top_margin, None, sample_turns)
    if top_score >= suggest_threshold:
        return MatchDecision(None, top_score, top_margin, top_id, sample_turns)
    return MatchDecision(None, top_score, top_margin, None, sample_turns)
