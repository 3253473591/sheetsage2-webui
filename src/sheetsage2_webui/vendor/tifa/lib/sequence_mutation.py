"""Random sequence edits for supervised forced-alignment training.

Applies random insertions, deletions and substitutions to a phoneme-timing
sample, then aligns original tokens to real target tokens via Levenshtein DP.
The left-to-right optimal alignment naturally prioritizes the first occurrence
of consecutive identical tokens.
"""

import numba
import numpy as np
import torch
from torch import Tensor

__all__ = [
    "apply_sequence_edits",
    "apply_mask_mutations",
]

_GAP = -1


@numba.njit(cache=True)
def _levenshtein_edit(
        orig_tokens: np.ndarray,
        tgt_tokens: np.ndarray,
        tgt_kept: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Levenshtein DP for sequence edit alignment.

    Only matches kept tokens (token equal AND kept); all other cases force
    indels.  prefer_indel (delete > insert > match at ties).
    Returns two int32 arrays with ``_GAP`` sentinel for gaps.
    """
    n, m = orig_tokens.shape[0], tgt_tokens.shape[0]
    INF = n + m + 1
    dp = np.empty((n + 1, m + 1), dtype=np.int32)
    for i in range(n + 1):
        dp[i, 0] = i
    for j in range(m + 1):
        dp[0, j] = j

    for i in range(1, n + 1):
        a = orig_tokens[i - 1]
        for j in range(1, m + 1):
            b = tgt_tokens[j - 1]
            if a == b and tgt_kept[j - 1]:
                match_val = dp[i - 1, j - 1]
            else:
                match_val = INF
            delete_val = dp[i - 1, j] + 1
            insert_val = dp[i, j - 1] + 1

            # prefer_indel: delete > insert > match at ties
            if delete_val <= insert_val and delete_val <= match_val:
                dp[i, j] = delete_val
            elif insert_val <= delete_val and insert_val <= match_val:
                dp[i, j] = insert_val
            else:
                dp[i, j] = match_val

    # Backtrack (delete > insert > match)
    max_len = n + m
    al_orig = np.full(max_len, _GAP, dtype=np.int32)
    al_mut = np.full(max_len, _GAP, dtype=np.int32)
    pos = max_len - 1
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and dp[i, j] == dp[i - 1, j] + 1:
            al_orig[pos] = orig_tokens[i - 1]
            i -= 1
        elif j > 0 and dp[i, j] == dp[i, j - 1] + 1:
            al_mut[pos] = tgt_tokens[j - 1]
            j -= 1
        else:
            al_orig[pos] = orig_tokens[i - 1]
            al_mut[pos] = tgt_tokens[j - 1]
            i -= 1
            j -= 1
        pos -= 1
    return al_orig[pos + 1:], al_mut[pos + 1:]


@numba.njit(cache=True)
def _levenshtein_mask(
        orig: np.ndarray,
        mut: np.ndarray,
        mask_token: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Levenshtein DP for MASK wildcard alignment.

    Exact match cost 0, MASK wildcard cost 1, no other matches allowed.
    prefer_indel + prefer_insert (insert > delete > match at ties).
    Returns two int32 arrays with ``_GAP`` sentinel for gaps.
    """
    n, m = orig.shape[0], mut.shape[0]
    INF = n + m + 1
    dp = np.empty((n + 1, m + 1), dtype=np.int32)
    for i in range(n + 1):
        dp[i, 0] = i
    for j in range(m + 1):
        dp[0, j] = j

    for i in range(1, n + 1):
        a = orig[i - 1]
        for j in range(1, m + 1):
            b = mut[j - 1]
            if a == b:
                match_val = dp[i - 1, j - 1]
            elif b == mask_token:
                match_val = dp[i - 1, j - 1] + 1
            else:
                match_val = INF
            delete_val = dp[i - 1, j] + 1
            insert_val = dp[i, j - 1] + 1

            # prefer_indel + prefer_insert: insert > delete > match
            if insert_val <= delete_val and insert_val <= match_val:
                dp[i, j] = insert_val
            elif delete_val <= insert_val and delete_val <= match_val:
                dp[i, j] = delete_val
            else:
                dp[i, j] = match_val

    # Backtrack (insert > delete > match)
    max_len = n + m
    al_orig = np.full(max_len, _GAP, dtype=np.int32)
    al_mut = np.full(max_len, _GAP, dtype=np.int32)
    pos = max_len - 1
    i, j = n, m
    while i > 0 or j > 0:
        if j > 0 and dp[i, j] == dp[i, j - 1] + 1:
            al_mut[pos] = mut[j - 1]
            j -= 1
        elif i > 0 and dp[i, j] == dp[i - 1, j] + 1:
            al_orig[pos] = orig[i - 1]
            i -= 1
        else:
            al_orig[pos] = orig[i - 1]
            al_mut[pos] = mut[j - 1]
            i -= 1
            j -= 1
        pos -= 1
    return al_orig[pos + 1:], al_mut[pos + 1:]


def _rebuild_regions(spans: torch.Tensor, T: int) -> torch.Tensor:
    """Rebuild per-frame region indices from spans via scatter-add + cumsum."""
    M = spans.shape[0]
    device = spans.device
    delta = torch.zeros(T + 1, dtype=torch.long, device=device)
    ids = torch.arange(1, M + 1, dtype=torch.long, device=device)
    delta.scatter_add_(0, spans[:, 0], ids)
    delta.scatter_add_(0, spans[:, 1], -ids)
    return delta.cumsum(0)[:T]


def apply_sequence_edits(
        *,
        tokens: Tensor,
        spans: Tensor,
        regions: Tensor,
        min_token: int,
        max_token: int,
        p_sub: float,
        p_del: float,
        p_ins: float,
        rng: np.random.Generator,
) -> tuple[Tensor, Tensor, Tensor, Tensor]:
    """Apply random edits to a token sequence.

    Args:
        tokens: ``[N]`` int64, 1-based vocabulary group IDs.
        spans: ``[N, 2]`` int64, (inclusive_start, exclusive_end) frames.
        regions: ``[T]`` int64, per-frame 1-based token index, 0 for stop.
        min_token: smallest valid token ID for insertions/substitutions (inclusive).
        max_token: largest valid token ID for insertions/substitutions (inclusive).
        p_sub: per-token substitution probability.
        p_del: per-token deletion probability.
        p_ins: per-gap insertion probability.
        rng: numpy ``Generator`` for reproducible random draws.

    Returns:
        ``(new_tokens, new_spans, new_regions, original_tokens)`` where:
        - new_tokens ``[N']`` int64, edited token IDs.
        - new_spans ``[N', 2]`` int64, (inclusive_start, exclusive_end) frames.
        - new_regions ``[T]`` int64, per-frame 1-based token index, 0 for stop.
        - original_tokens ``[N']`` int64, the phoneme ID that belongs at each
          position: the original token ID for kept and substituted tokens, 0
          for insertions.
    """
    N = tokens.shape[0]
    T = regions.shape[0]
    device = tokens.device

    # ------------------------------------------------------------------
    # Phase 1  --  Sample edits and build target sequence
    # ------------------------------------------------------------------

    do_sub = rng.random(N) < p_sub
    do_del = rng.random(N) < p_del
    do_ins = rng.random(N + 1) < p_ins

    # Guard: can't delete all tokens
    if do_del.all():
        keep_idx = rng.integers(0, N)
        do_del[keep_idx] = False

    # Priority: sub > del > ins (at most one operation per place)
    do_del[do_sub] = False
    blocked_ins = np.zeros(N + 1, dtype=bool)
    blocked_ins[1:] = do_sub | do_del
    do_ins[blocked_ins] = False

    n_rand = int(do_sub.sum()) + int(do_ins.sum())
    all_rand = rng.integers(min_token, max_token + 1, size=n_rand)
    rand_ptr = 0

    orig_tokens = tokens.tolist()
    tgt_tokens: list[int] = []
    tgt_original_tokens: list[int] = []

    for i in range(N):
        if do_ins[i]:
            tgt_tokens.append(int(all_rand[rand_ptr]))
            tgt_original_tokens.append(0)
            rand_ptr += 1
        if do_del[i]:
            continue
        if do_sub[i]:
            tgt_tokens.append(int(all_rand[rand_ptr]))
            tgt_original_tokens.append(orig_tokens[i])
            rand_ptr += 1
        else:
            tgt_tokens.append(orig_tokens[i])
            tgt_original_tokens.append(orig_tokens[i])

    if do_ins[N]:
        tgt_tokens.append(int(all_rand[rand_ptr]))
        tgt_original_tokens.append(0)
        rand_ptr += 1

    # ------------------------------------------------------------------
    # Phase 2  --  Levenshtein DP alignment (numba, only kept tokens match)
    # ------------------------------------------------------------------
    tgt_kept = [tgt_original_tokens[j] == t for j, t in enumerate(tgt_tokens)]
    al_orig, al_mut = _levenshtein_edit(
        np.array(orig_tokens, dtype=np.int32),
        np.array(tgt_tokens, dtype=np.int32),
        np.array(tgt_kept, dtype=np.bool_),
    )

    # ------------------------------------------------------------------
    # Phase 3  --  Assign spans and rebuild regions
    # ------------------------------------------------------------------

    orig_spans = spans.tolist()
    new_spans_list: list[tuple[int, int]] = []
    orig_ptr = 0
    for j in range(al_mut.shape[0]):
        # Advance orig_ptr for deleted original tokens (gap in mut)
        if al_mut[j] == _GAP:
            if al_orig[j] != _GAP:
                orig_ptr += 1
            continue
        if al_orig[j] == _GAP:
            new_spans_list.append((0, 0))
        else:
            new_spans_list.append(tuple(orig_spans[orig_ptr]))
            orig_ptr += 1

    new_tokens = torch.tensor(tgt_tokens, dtype=torch.long, device=device)
    new_spans = torch.tensor(new_spans_list, dtype=torch.long, device=device)
    original_tokens = torch.tensor(tgt_original_tokens, dtype=torch.long, device=device)
    new_regions = _rebuild_regions(new_spans, T)
    return new_tokens, new_spans, new_regions, original_tokens


def apply_mask_mutations(
        *,
        tokens: torch.Tensor,
        spans: torch.Tensor,
        regions: torch.Tensor,
        p_mask: float,
        p_insert: float,
        p_chain: float,
        max_chain: int,
        mask_token: int,
        space_token: int,
        rng: np.random.Generator,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """Apply MASK replacement and chained insertion, resolve with alignment.

    MASK replacement randomly replaces existing tokens with *mask_token*.
    Chained insertion randomly inserts MASK runs at gaps with geometric
    continuation.  After both mutations, Levenshtein alignment with
    MASK-as-wildcard resolves which MASKs correspond to original tokens
    and which are genuine insertions.

    The alignment step is necessary because a MASK can arise from either
    a replacement or an insertion, and the model sees only the final token
    sequence -- without alignment the target for each MASK position would
    be ambiguous.  Treating MASK as a wildcard during alignment ensures
    each replacement-MASK is deterministically paired with the leftmost
    available original token, while unpaired MASKs are labeled as SPACE.

    Args:
        tokens: ``[N]`` int64, original token IDs (no MASK).
        spans: ``[N, 2]`` int64, (start, end) frames.
        regions: ``[T]`` int64, per-frame 1-based token indices.
        p_mask: per-token replacement probability.
        p_insert: per-gap probability of starting an insertion run.
        p_chain: probability of extending a run by one more MASK.
        max_chain: maximum number of consecutive MASKs in a run.
        mask_token: token ID used for MASK.
        space_token: token ID used for SPACE (insertion target).
        rng: numpy ``Generator`` for reproducible random draws.

    Returns:
        ``(new_tokens, new_spans, new_regions, targets)`` where targets
        is *space_token* for inserted positions and the original token ID
        for kept or replaced positions.
    """

    N = tokens.shape[0]
    T = regions.shape[0]
    device = tokens.device

    orig_list = tokens.tolist()
    span_list = spans.tolist()

    # Step 1: MASK replacement (per-token, batched)
    do_mask = rng.random(N) < p_mask
    mut_list = [mask_token if do_mask[i] else orig_list[i] for i in range(N)]

    # Step 2: MASK chained insertion (per-gap, batched chain lengths)
    do_insert = rng.random(N + 1) < p_insert
    insertions: list[list[int]] = [[] for _ in range(N + 1)]
    n_active = int(do_insert.sum())
    if n_active > 0 and max_chain > 1:
        # chain_lens[i] = number of additional MASKs beyond the first
        chain_rands = rng.random((n_active, max_chain - 1))
        still_going = np.cumprod(chain_rands < p_chain, axis=1)
        chain_lens = still_going.sum(axis=1)  # 0 .. max_chain-1
        active_idx = 0
        for gap in range(N + 1):
            if do_insert[gap]:
                cl = int(chain_lens[active_idx]) + 1
                insertions[gap] = [mask_token] * cl
                active_idx += 1
    elif n_active > 0:
        # max_chain <= 1: each active gap gets exactly one MASK
        for gap in range(N + 1):
            if do_insert[gap]:
                insertions[gap] = [mask_token]

    # Build pre-alignment sequence
    pre_tok: list[int] = []
    for i in range(N):
        pre_tok.extend(insertions[i])
        pre_tok.append(mut_list[i])
    pre_tok.extend(insertions[N])

    # Step 3: Levenshtein alignment (numba, MASK wildcard: exact=0, wildcard=1)
    al_orig, al_mut = _levenshtein_mask(
        np.array(orig_list, dtype=np.int32),
        np.array(pre_tok, dtype=np.int32),
        mask_token,
    )

    # Step 4: Build targets, tokens, and spans from alignment.
    # _GAP in al_orig means this position is an insertion.
    # _GAP in al_mut means the original token was deleted.
    new_tok: list[int] = []
    new_spans: list[tuple[int, int]] = []
    targets: list[int] = []
    orig_ptr = 0
    for j in range(al_mut.shape[0]):
        if al_mut[j] == _GAP:
            continue  # deletion -- skip this position
        new_tok.append(int(al_mut[j]))
        if al_orig[j] == _GAP:
            new_spans.append((0, 0))
            targets.append(space_token)
        else:
            new_spans.append(tuple(span_list[orig_ptr]))
            targets.append(orig_list[orig_ptr])
            orig_ptr += 1

    new_tokens = torch.tensor(new_tok, dtype=torch.long, device=device)
    new_spans_t = torch.tensor(new_spans, dtype=torch.long, device=device)
    new_targets = torch.tensor(targets, dtype=torch.long, device=device)
    new_regions = _rebuild_regions(new_spans_t, T)
    return new_tokens, new_spans_t, new_regions, new_targets
