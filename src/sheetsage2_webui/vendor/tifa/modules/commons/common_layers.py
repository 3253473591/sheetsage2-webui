import torch
import torch.nn.functional as F
from torch import nn
import torch.onnx.operators


class CyclicRegionEmbedding(nn.Module):
    def __init__(self, embedding_dim: int, cycle_length: int = 3):
        super().__init__()
        self.cycle_length = cycle_length
        self.embedding = nn.Embedding(cycle_length, embedding_dim)

    def forward(self, idx):
        if self.training:
            *B, _ = idx.shape
            shift = torch.randint(0, self.cycle_length, (*B, 1)).to(idx)
            idx = idx + shift
        return self.embedding(idx % self.cycle_length)


class LocalDownsample(nn.Module):
    # noinspection PyMethodMayBeStatic
    def forward(self, x, regions, max_n: int = None):
        """
        :param x: [..., T, C] input tensor to downsample
        :param regions: int64 [..., T] mapping from positions to region indices starting from 1.
        :param max_n: int, maximum number of regions. N = max(regions) if not given.
        :return: [..., N, C] where N = max(regions)
        """
        N = regions.max() if max_n is None else max_n
        B = (1,) * (x.ndim - 2)
        idx = torch.arange(N + 1, dtype=torch.long, device=regions.device).reshape(*B, -1, 1)  # [..., N+1, 1]
        region_map = idx == regions.unsqueeze(-2)  # [..., N, T]
        region_weight = region_map.float()
        region_size = torch.where(
            torch.any(region_map, dim=-1, keepdim=True),
            region_weight.sum(dim=-1, keepdim=True),
            1.0
        )  # [..., N, 1]
        weight = region_weight / region_size  # [..., N+1, T]
        weight = weight[..., 1:, :]  # [..., N, T]
        x_down = weight @ x  # [..., N, T] @ [..., T, C] -> [..., N, C]
        return x_down  # [..., N, C]


class TemporalMask(nn.Module):
    """Time-domain masking for sequence features.

    Supports two mask patterns:
    - ``chunk``: Contiguous spans of length ``mask_len``, each starting
      independently with probability ``mask_p`` per time step. Expected
      masked fraction: approximately ``1 - (1 - mask_p) ^ mask_len``.
    - ``random``: Scattered frames. Each position is independently kept
      with probability ``1 - mask_p``, subject to the optional eligibility
      mask.

    Masked positions are replaced with a fill value: either a learnable
    parameter of shape ``[channels]`` (``fill_method="learnable"``) or
    fresh Gaussian noise per forward pass (``fill_method="randn"``).

    In eval mode, randomness is deterministic via a seeded generator.
    Call ``reset_random_generator()`` to re-seed.  The caller controls
    whether the module is invoked (no automatic gate by training mode).

    :param channels: feature dimension C
    :param mask_type: ``"chunk"`` or ``"random"``
    :param fill_method: ``"learnable"`` or ``"randn"``
    :param mask_len: span length for chunk masking
    :param mask_p: probability of starting a chunk mask, or masking rate
        for random masking
    :param seed: optional seed for the deterministic eval-mode generator
    """

    def __init__(
            self, channels: int,
            mask_type: str = "chunk", fill_method: str = "learnable",
            mask_len: int = 50, mask_p: float = 0.01,
            seed: int | None = None,
    ):
        super().__init__()
        if mask_type not in ("chunk", "random"):
            raise ValueError(f"Unknown mask_type: {mask_type}")
        if fill_method not in ("learnable", "randn"):
            raise ValueError(f"Unknown fill_method: {fill_method}")
        self.mask_type = mask_type
        self.fill_method = fill_method
        self.mask_len = mask_len
        self.mask_p = mask_p
        self._seed = seed
        self._rng = None
        if fill_method == "learnable":
            self.mask_fill = nn.Parameter(torch.randn(channels))

    def _get_rng(self, device):
        """Lazy-init the eval-mode generator on the correct device."""
        if self._rng is None or self._rng.device != device:
            self._rng = torch.Generator(device=device)
            if self._seed is not None:
                self._rng.manual_seed(self._seed)
        return self._rng

    def reset_random_generator(self):
        """Reset the eval-mode random generator with the original init seed."""
        if self._rng is not None:
            if self._seed is not None:
                self._rng.manual_seed(self._seed)
            else:
                self._rng.seed()

    def _rand(self, *shape, device):
        """Random tensor; deterministic in eval mode."""
        if self.training:
            return torch.rand(*shape, device=device)
        return torch.rand(*shape, device=device, generator=self._get_rng(device))

    def forward(self, x, mask=None):
        """
        :param x: [..., T, C]
        :param mask: optional bool [..., T], True = eligible for masking
        :return: (masked_x, corrupted_mask) where masked_x is [..., T, C]
            and corrupted_mask is [..., T] bool (True = frame was replaced)
        """
        *B, T, C = x.shape
        x_flat = x.reshape(-1, T, C)
        B_flat = x_flat.shape[0]
        device = x.device
        mask_flat = mask.reshape(-1, T) if mask is not None else None

        if self.mask_type == "chunk":
            keep_mask = self._chunk_keep_mask(B_flat, T, device, mask_flat)
        else:
            keep_mask = self._random_keep_mask(B_flat, T, device, mask_flat)

        if self.fill_method == "learnable":
            fill = self.mask_fill  # [C]
        else:
            fill = torch.randn(B_flat, T, C, device=device)
        masked_x = x_flat * keep_mask.unsqueeze(-1) + fill * (~keep_mask).unsqueeze(-1)
        return masked_x.reshape(*B, T, C), (~keep_mask).reshape(*B, T)

    def _chunk_keep_mask(self, B, T, device, mask):
        starts = self._rand(B, T, device=device) < self.mask_p
        if mask is not None:
            starts = starts & mask
        kernel = torch.ones(1, 1, self.mask_len, device=device)
        spans = F.conv1d(starts.float().unsqueeze(1), kernel, padding=self.mask_len - 1)
        spans = spans[:, :, :T]
        return (spans == 0).bool().squeeze(1)

    def _random_keep_mask(self, B, T, device, mask):
        rand = self._rand(B, T, device=device)
        if mask is not None:
            rand = rand.masked_fill(~mask, float('inf'))
        sorted_idx = torch.argsort(rand, dim=-1)
        if mask is not None:
            eligible = mask.sum(dim=-1)  # [B]
            keep_count = (eligible * (1 - self.mask_p)).long() + 1
            keep_count = torch.clamp(keep_count, max=eligible)
        else:
            keep_count = torch.full((B,), int(T * (1 - self.mask_p)) + 1, device=device)
            keep_count = torch.clamp(keep_count, max=T)
        max_keep = keep_count.max().item()
        sorted_idx_trunc = sorted_idx[:, :max_keep]
        pos = torch.arange(max_keep, device=device).unsqueeze(0)
        valid = pos < keep_count.unsqueeze(1)
        batch_ids = torch.arange(B, device=device).unsqueeze(1).expand(B, max_keep)
        flat_idx = (batch_ids * T + sorted_idx_trunc)[valid]
        keep_mask = torch.zeros(B, T, dtype=torch.bool, device=device)
        keep_mask.view(-1)[flat_idx] = True
        if mask is not None:
            keep_mask = keep_mask | ~mask
        return keep_mask
