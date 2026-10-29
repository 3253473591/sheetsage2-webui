"""Tensor operations on complete word candidates.

paths/groups: [B,P,C], words: [B,P], candidates: [B,W,C].
P is the aligned source-grid capacity, C the candidate capacity.
choices uses 1-based candidate IDs, including empty paths; zero means absent.
Word and group IDs start at one; zero denotes padding.
"""

import math

import torch
from torch import Tensor


def first_choices(candidates: Tensor) -> Tensor:
    """Choose the first valid 1-based candidate ID [B,W], or zero if absent."""
    first = candidates.long().argmax(dim=-1) + 1
    return first.masked_fill(~candidates.any(dim=-1), 0)


def sample_paths_uniform(candidates: Tensor, k: int) -> Tensor:
    """Sample complete choices [B,k,W]; candidate validity is prefix-packed."""
    widths = candidates.sum(dim=-1)
    B, W = widths.shape
    choices = (torch.rand(B, W, k, device=candidates.device) * widths.unsqueeze(-1)).long() + 1
    choices = choices.clamp(max=widths.unsqueeze(-1))
    return choices.permute(0, 2, 1)


def sample_paths_perm(candidates: Tensor, r: int = 1) -> Tensor:
    """Sample [B,r*C,W], covering every valid candidate of each word."""
    B, W, C = candidates.shape
    widths = candidates.sum(dim=-1)
    rand = torch.rand(B, W, C, r, device=candidates.device)
    rand = rand.masked_fill(~candidates.unsqueeze(-1), torch.inf)
    perm = rand.argsort(dim=2)
    choices = perm % widths.clamp(min=1).view(B, W, 1, 1) + 1
    choices = choices.masked_fill(widths.view(B, W, 1, 1) == 0, 0)
    return choices.permute(0, 3, 2, 1).reshape(B, r * C, W)


def extract_tokens(
        *args: Tensor,
        words: Tensor,
        choices: Tensor,
) -> tuple[Tensor, ...]:
    """Gather whole candidates from [B,P,C] grids.

    choices is [B,...,W], with 1-based candidate IDs and zero for padding.
    All positions of a word use the same candidate column. Word zero
    selects padding. Output capacity remains P.
    """
    B, P, _ = args[0].shape
    middle_shape = choices.shape[1:-1]
    K = math.prod(middle_shape)
    chosen = choices.reshape(B, K, choices.shape[-1])
    chosen = torch.cat([chosen.new_zeros(B, K, 1), chosen], dim=-1)
    indices = chosen.gather(-1, words.unsqueeze(1).expand(B, K, P)).unsqueeze(-1)
    shape = (B, *middle_shape, P)
    results = []
    for source in args:
        source = torch.cat([source.new_zeros(B, P, 1), source], dim=-1)
        selected = source.unsqueeze(1).expand(-1, K, -1, -1).gather(-1, indices).squeeze(-1)
        results.append(selected.reshape(shape))
    return tuple(results)


def compact_sequences(tokens: Tensor, *args: Tensor) -> tuple[Tensor, ...]:
    """Left-pack nonzero tokens and matching arrays, keeping input capacity.

    The padding tail stays zero. No tensor value controls a Python
    branch or an output dimension. Equal sort keys occur only in padding.
    """
    active = tokens != 0
    capacity = tokens.shape[-1]
    positions = torch.arange(capacity, device=tokens.device).expand_as(tokens)
    order = torch.where(active, positions, capacity).argsort(dim=-1)
    return (tokens.gather(-1, order),) + tuple(
        value.masked_fill(~active, 0).gather(-1, order) for value in args
    )


def materialize_paths(
        paths: Tensor,
        words: Tensor,
        groups: Tensor,
        choices: Tensor,
) -> tuple[Tensor, Tensor, Tensor]:
    """Return selected tokens, word IDs and global group IDs.

    Outputs are [B,...,P] for choices [B,...,W]. Gap slots are removed before comparing
    local groups, so an aligned gap cannot split a pronunciation group.
    Text labels are recovered separately from candidate metadata.
    """
    tokens, local_groups = extract_tokens(paths, groups, words=words, choices=choices)
    owners = words.reshape(words.shape[0], *((1,) * (choices.ndim - 2)), words.shape[1]).expand_as(tokens)
    tokens, owners, local_groups = compact_sequences(tokens, owners, local_groups)
    active = tokens != 0
    positions = torch.arange(tokens.shape[-1], device=tokens.device)
    starts = active & (
        (positions == 0)
        | (owners != owners.roll(1, -1))
        | (local_groups != local_groups.roll(1, -1))
    )
    numbered = starts.long().cumsum(dim=-1).masked_fill(~active, 0)
    return tokens, owners, numbered


def path_lengths(paths: Tensor, words: Tensor, candidates: Tensor) -> Tensor:
    """Maximum real token count [B], independent of alignment gap slots."""
    B, _, C = paths.shape
    counts = paths.new_zeros(B, candidates.shape[1] + 1, C)
    counts = counts.scatter_add(1, words.unsqueeze(-1).expand_as(paths), (paths != 0).long())
    return counts[:, 1:].amax(dim=-1).sum(dim=-1)
