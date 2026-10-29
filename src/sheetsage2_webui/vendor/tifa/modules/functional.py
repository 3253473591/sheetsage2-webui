import torch
from torch import Tensor
import torch.nn.functional as F


def cross_cosine_similarity(x: Tensor, y: Tensor, temperature: float = 1.0) -> Tensor:
    """
    Compute cross-sequence cosine similarity matrix.
    x: [..., T, C]  (e.g. frame features)
    y: [..., N, C]  (e.g. token embeddings)
    Returns: [..., T, N]
    """
    x_norm = F.normalize(x.float(), p=2, dim=-1, eps=1e-8)
    y_norm = F.normalize(y.float(), p=2, dim=-1, eps=1e-8)
    return (x_norm @ y_norm.transpose(-1, -2)) / temperature


def interleave_spaces(
    tokens: Tensor, space: int, lengths: Tensor | None = None,
) -> tuple[Tensor, Tensor | None]:
    """Insert space tokens around every real token.  Fully batched.

    tokens:  [..., N] int64, PAD=0, trailing padding
    space:   int, token ID to insert between real tokens
    lengths: [...] int64 or None, valid count per sample (None = all valid)

    Returns:
        tokens: [..., 2*N+1] int64, interleaved tokens (PAD beyond valid)
        mask:   [..., 2*N+1] bool or None, True for valid positions
    """
    N = tokens.shape[-1]
    result = tokens.new_full(tokens.shape[:-1] + (2 * N + 1,), space)
    result[..., 1::2] = tokens
    if lengths is None:
        return result, None
    pos = torch.arange(2 * N + 1, device=result.device)
    mask = pos < (2 * lengths.unsqueeze(-1) + 1)
    result = result * mask.long()
    return result, mask
