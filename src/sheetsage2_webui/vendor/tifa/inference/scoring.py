"""Tensor MLM preparation and scoring, followed by whole-word path DP.

Levenshtein profiles are built at the G2P encoding boundary. Their columns
keep complete candidate identities; common anchors never release a choice.
Only the forward/backward DP below reads numeric data on the CPU.
"""

import math
from typing import Literal

import numpy as np
import torch
from torch import Tensor

from lib.path_traversal import compact_sequences
from lib.vocabulary import MASK_TOKEN, SPACE_TOKEN


def _starts(indices: Tensor) -> Tensor:
    """First source position for each positive ID, with capacity-sized output."""
    B, P = indices.shape
    positions = torch.arange(P, device=indices.device).expand(B, P)
    initial = indices.new_full((B, P + 1), P)
    return initial.scatter_reduce(1, indices, positions, reduce="amin")


def prepare_scoring(
        paths: Tensor,
        words: Tensor,
        candidates: Tensor,
        *,
        unit: Literal["levenshtein", "word"] = "levenshtein",
) -> tuple[Tensor, Tensor, Tensor]:
    """Return templates, segments and source mapping, all int64 [B,P].

    paths [B,P,C] holds complete candidate columns, with zero for aligned
    gaps or padding. words [B,P] gives w+1; zero denotes padding.
    candidates [B,W,C] distinguishes empty/absent candidates.

    segments numbers contiguous MASK runs in the compacted template.
    mapping assigns each divergent source-grid row to its scoring segment.
    A candidate's contribution comprises its nonzero tokens in those rows.
    Empty contributions remain represented by mapping and candidate validity.
    """
    if unit not in ("levenshtein", "word"):
        raise ValueError(f"Unknown scoring unit: {unit}")
    B, P, C = paths.shape
    positions = torch.arange(P, device=paths.device).expand(B, P)
    valid = torch.cat([candidates.new_zeros(B, 1, C), candidates], dim=1)
    valid = valid.gather(1, words.unsqueeze(-1).expand_as(paths))
    occupied = (paths != 0).any(dim=-1)
    shared = (((paths == paths[..., :1]) & (paths != 0)) | ~valid).all(dim=-1)
    divergent = occupied & ~shared
    if unit == "word":
        ambiguous = words.new_zeros(B, candidates.shape[1] + 1)
        ambiguous = ambiguous.scatter_add(1, words, divergent.long())
        divergent = (ambiguous.gather(1, words) > 0) & occupied

    begin = occupied & (
        (positions == 0)
        | (words != words.roll(1, -1))
        | (divergent != divergent.roll(1, -1))
        | ~occupied.roll(1, -1)
    )
    regions = begin.long().cumsum(dim=-1).masked_fill(~occupied, 0)
    counts = paths.new_zeros(B, P + 1, C).scatter_add(
        1, regions.unsqueeze(-1).expand_as(paths),
        ((paths != 0) & divergent.unsqueeze(-1)).long(),
    )
    capacity = counts.amax(dim=-1).gather(1, regions)
    source_starts = _starts(regions).gather(1, regions).clamp_max(P - 1)
    keep = occupied & (~divergent | (positions - source_starts < capacity))
    template = torch.where(divergent, MASK_TOKEN, paths[..., 0]).masked_fill(~keep, 0)
    masked_tokens, = compact_sequences(template)
    masked = masked_tokens == MASK_TOKEN
    segments = (masked & ((positions == 0) | ~masked.roll(1, -1))).long().cumsum(dim=-1)
    segments = segments.masked_fill(~masked, 0)

    preceding = keep.long().cumsum(dim=-1) - keep.long()
    destinations = preceding.gather(1, source_starts).clamp_max(P - 1)
    mapping = segments.gather(1, destinations).masked_fill(~divergent, 0)
    return masked_tokens, segments, mapping


def score_fragments(
        log_probs: Tensor,
        paths: Tensor,
        words: Tensor,
        segments: Tensor,
        mapping: Tensor,
) -> tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
    """Compute all fragment/offset costs and SPACE suffixes as tensors.

    Fragment f>0 belongs to one word and segment. Costs [B,P+1,C,P+1]
    retain input-derived capacity: the last axis is the segment offset.
    Descriptors [B,P+1,2] contain word and segment IDs; zero marks padding.
    No tensor values determine Python control flow or allocation sizes.
    """
    B, P, C = paths.shape
    position = torch.arange(P, device=paths.device).expand(B, P)
    active = mapping > 0
    first = active & (
        (position == 0) | (mapping != mapping.roll(1, -1)) | (words != words.roll(1, -1))
    )
    fragments = first.long().cumsum(dim=-1).masked_fill(~active, 0)
    owner = words.new_zeros(B, P + 1).scatter_reduce(
        1, fragments, words.masked_fill(~active, 0), reduce="amax",
    )
    segment = mapping.new_zeros(B, P + 1).scatter_reduce(
        1, fragments, mapping, reduce="amax",
    )
    descriptors = torch.stack([owner, segment], dim=-1)
    real = (paths != 0) & active.unsqueeze(-1)
    lengths = paths.new_zeros(B, P + 1, C).scatter_add(
        1, fragments.unsqueeze(-1).expand_as(paths), real.long(),
    )
    prefix = real.long().cumsum(dim=1) - real.long()
    source_starts = _starts(fragments).gather(1, fragments).clamp_max(P - 1)
    ordinal = prefix - prefix.gather(1, source_starts.unsqueeze(-1).expand_as(paths))

    segment_lengths = segments.new_zeros(B, P + 1).scatter_add(1, segments, (segments > 0).long())
    segment_starts = _starts(segments)
    offsets = torch.arange(P + 1, device=paths.device)
    locations = (
        segment_starts.gather(1, mapping).unsqueeze(-1).unsqueeze(-1)
        + ordinal.unsqueeze(-1) + offsets
    ).clamp_max(P - 1)
    batch = torch.arange(B, device=paths.device).view(B, 1, 1, 1)
    values = log_probs[batch, locations, paths.unsqueeze(-1)]
    values = values.masked_fill(~real.unsqueeze(-1), 0)
    costs = values.new_zeros(B, P + 1, C, P + 1).scatter_add(
        1, fragments.unsqueeze(-1).unsqueeze(-1).expand_as(values), values,
    )
    capacity = segment_lengths.gather(1, segment).unsqueeze(-1)
    fits = offsets <= (capacity - lengths).unsqueeze(-1)
    costs = costs.masked_fill(~fits, -torch.inf)

    q = torch.arange(P, device=paths.device)
    locations = (segment_starts.unsqueeze(-1) + q).clamp_max(P - 1)
    space = log_probs[
        torch.arange(B, device=paths.device).view(B, 1, 1), locations, SPACE_TOKEN,
    ]
    space = space.masked_fill(q >= segment_lengths.unsqueeze(-1), 0)
    suffix = space.flip(-1).cumsum(-1).flip(-1)
    tails = torch.cat([suffix, suffix.new_zeros(B, P + 1, 1)], dim=-1)
    return descriptors, lengths, costs, tails, segment_lengths


def _advance(state, pieces, choice, descriptors, lengths, costs, tails, capacity):
    """Process every fragment of a complete candidate before returning a state."""
    segment, used = state
    score = 0.0
    for fragment in pieces:
        current = int(descriptors[fragment, 1])
        if current != segment:
            score += float(tails[segment, used])
            segment, used = current, 0
        length = int(lengths[fragment, choice])
        if used + length > capacity[segment]:
            return None
        score += float(costs[fragment, choice, used])
        used += length
    return (segment, used), score


def _select_sample(valid, descriptors, lengths, costs, tails, capacity):
    """Whole-word Viterbi and conditional scores with deterministic ties.

    F[i+1,y] = max(F[i,x] + A[i,c,x]) over complete-candidate transitions.
    H[W,(s,n)] = R[s,n]; H[i,x] = max(A[i,c,x] + H[i+1,y]).
    Q[i,c] = max_x(F[i,x] + A[i,c,x] + H[i+1,y]).
    Closing a segment adds its SPACE suffix exactly once.
    """
    forward = [{(0, 0): 0.0}]
    ranks = {(0, 0): 0}
    parents, layers = [], []
    for w, row in enumerate(valid):
        pieces = np.flatnonzero(descriptors[:, 0] == w + 1)
        candidates = (np.flatnonzero(row) + 1).tolist() or [0]
        following, parent, order, edges = {}, {}, {}, []
        for source, value in forward[-1].items():
            for c in candidates:
                transition = (
                    _advance(source, pieces, c - 1, descriptors, lengths, costs, tails, capacity)
                    if c > 0 else (source, 0.0)
                )
                if transition is None:
                    continue
                target, cost = transition
                edges.append((source, target, c, cost))
                total = value + cost
                key = (ranks[source], c)
                if (target not in following or total > following[target]
                        or (total == following[target] and key < order[target])):
                    following[target] = total
                    parent[target] = (source, c)
                    order[target] = key
        if not following:
            raise ValueError("No complete pronunciation path fits the scoring template.")
        ranks = {state: rank for rank, state in enumerate(sorted(order, key=order.get))}
        forward.append(following)
        parents.append(parent)
        layers.append(edges)

    terminal = {state: float(tails[state[0], state[1]]) for state in forward[-1]}
    state = min(forward[-1], key=lambda x: (-(forward[-1][x] + terminal[x]), ranks[x]))
    choices = np.zeros(len(valid), dtype=np.int64)
    for i in range(len(valid) - 1, -1, -1):
        state, choices[i] = parents[i][state]

    scores = np.full(valid.shape, -np.inf)
    backward = terminal
    for i in range(len(valid) - 1, -1, -1):
        previous = {state: -math.inf for state in forward[i]}
        for source, target, c, cost in layers[i]:
            suffix = cost + backward[target]
            previous[source] = max(previous[source], suffix)
            if c > 0:
                scores[i, c - 1] = max(scores[i, c - 1], forward[i][source] + suffix)
        backward = previous
    return choices, scores


@torch.no_grad()
def select_paths(
        log_probs: Tensor,
        paths: Tensor,
        words: Tensor,
        candidates: Tensor,
        segments: Tensor,
        mapping: Tensor,
) -> tuple[Tensor, Tensor]:
    """Return joint choices [B,W] and conditional candidate scores [B,W,C].

    Numerical scoring stays on the input device. CPU access begins at
    the DP boundary. Scores fix one word's candidate and optimize every
    other word; independent score argmaxes do not reconstruct choices.
    Valid candidates score zero when there are no MASK slots.
    Choices are 1-based candidate IDs, with zero for absent words.
    """
    tensors = score_fragments(log_probs, paths, words, segments, mapping)
    return select_scored_paths(candidates, *tensors, vocab_size=log_probs.shape[-1])


@torch.no_grad()
def select_scored_paths(
        candidates: Tensor,
        descriptors: Tensor,
        lengths: Tensor,
        costs: Tensor,
        tails: Tensor,
        capacity: Tensor,
        *,
        vocab_size: int,
) -> tuple[Tensor, Tensor]:
    """Run host DP on fragment scores; return 1-based choices, zero if absent."""
    tensors = descriptors, lengths, costs, tails, capacity
    descriptors, lengths, costs, tails, capacity = (
        value.detach().cpu().numpy() for value in tensors
    )
    valid = candidates.detach().cpu().numpy()
    choices = np.zeros(valid.shape[:2], dtype=np.int64)
    scores = np.full(valid.shape, -np.inf, dtype=np.float32)
    for b in range(valid.shape[0]):
        chosen, values = _select_sample(valid[b], descriptors[b], lengths[b], costs[b], tails[b], capacity[b])
        count = int(capacity[b].sum())
        if count:
            values = values / count + math.log(vocab_size)
        choices[b] = chosen
        scores[b] = values
    return (
        torch.as_tensor(choices, device=candidates.device),
        torch.as_tensor(scores, device=candidates.device),
    )
