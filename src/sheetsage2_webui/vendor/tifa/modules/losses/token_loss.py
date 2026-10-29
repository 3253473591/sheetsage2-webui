import torch.nn as nn
from torch import Tensor
from torch.nn import functional as F


class TokenIdentityLoss(nn.Module):
    """Cross-entropy over token identity per token position.

    Inputs:
        logits: [B, N, V]  raw logits per token position
        targets: [B, N] int64       target phoneme ID (0 for gap/insertion)
        mask_token: [B, N] bool     non-padding tokens
    Returns:
        Scalar loss averaged over valid tokens.
    """

    def forward(self, logits: Tensor, targets: Tensor, mask_token: Tensor) -> Tensor:
        loss = F.cross_entropy(
            logits.float().permute(0, 2, 1),  # [B, V, N]
            targets,
            reduction='none',
        )
        return (loss * mask_token.float()).sum() / mask_token.float().sum().clamp(min=1)


class FrameIdentityLoss(nn.Module):
    """Cross-entropy over token identity per frame.

    Targets are precomputed per-frame vocabulary IDs. Class 0 is the gap.
    Padding frames are ignored.

    Inputs:
        frame_logits:  [B, T, V]  raw logits per frame
        frame_targets: [B, T]     int64 vocabulary ID per frame, 0 = gap
        t_mask:        [B, T]     bool, non-padding frames
    Returns:
        Scalar loss averaged over non-padding frames.
    """

    def forward(
        self,
        frame_logits: Tensor,
        frame_targets: Tensor,
        mask_frame: Tensor,
    ) -> Tensor:
        if mask_frame.sum() == 0:
            return frame_logits.new_zeros(())

        target = frame_targets.clone()
        target[~mask_frame] = -100  # mask padding frames only; 0 = gap is a valid class

        B, T, V = frame_logits.shape
        loss = F.cross_entropy(
            frame_logits.reshape(B * T, V),
            target.reshape(B * T),
            ignore_index=-100,
            reduction='mean',
        )
        return loss
