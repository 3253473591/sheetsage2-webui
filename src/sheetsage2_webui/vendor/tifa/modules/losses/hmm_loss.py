import math

import torch
import torch.nn as nn
import torch.nn.functional as F


def hmm_forward_loss_with_emission(
        emission: torch.Tensor,
        frame_lens: torch.Tensor,
        token_lens: torch.Tensor,
) -> torch.Tensor:
    """CTC-based HMM forward loss from pre-computed emission matrix.

    Args:
        emission: [B, T_max, N_max] frame-to-token emission scores
        frame_lens: [B] int64 number of valid frames per sample
        token_lens: [B] int64 number of valid tokens per sample
    Returns:
        Scalar loss.
    """
    B, T_max, N_max = emission.shape

    # mask padding token states
    state_mask = torch.arange(N_max, device=emission.device).unsqueeze(0) >= token_lens.unsqueeze(1)
    emission.masked_fill_(state_mask.unsqueeze(1), -1e9)

    # append blank column (set to -inf, effectively disabled)
    blank_col = torch.full((B, T_max, 1), -1e9, device=emission.device)
    logits = torch.cat([emission, blank_col], dim=-1)  # [B, T_max, N_max+1]

    log_probs = F.log_softmax(logits, dim=-1)

    targets = torch.arange(N_max, device=emission.device).unsqueeze(0).expand(B, -1)

    loss = F.ctc_loss(
        log_probs.transpose(0, 1),  # [T_max, B, N_max+1]
        targets,
        input_lengths=frame_lens,
        target_lengths=token_lens,
        blank=N_max,  # global blank index
        reduction='mean',
        zero_infinity=True,
    )
    return loss


class HMMForwardLossWithEmissions(nn.Module):
    """HMM forward loss via CTC with disabled blank.

    Accepts a single emission [B, ..., T, N] or a list of such tensors.
    Middle dims are flattened as separate instances (separate mode)
    or averaged before loss computation (mean mode).

    Inputs:
        emissions: [B, ..., T, N] or list of such tensors
        frame_lens: [B] int64 number of valid frames per sample
        token_lens: [B] int64 number of valid tokens per sample
    Returns:
        Scalar loss.
    """

    def __init__(self, mode: str = "separate"):
        super().__init__()
        if mode not in ("separate", "mean"):
            raise ValueError(f"Unknown mode: {mode}")
        self.mode = mode

    def forward(
            self,
            emissions: torch.Tensor | list[torch.Tensor],
            frame_lens: torch.Tensor,
            token_lens: torch.Tensor,
    ) -> torch.Tensor:
        if isinstance(emissions, torch.Tensor):
            emissions = [emissions]

        if self.mode == "mean":
            # Flatten middle dims of all emissions, then average over all of them
            # so every CA head contributes equally regardless of layer.
            all_flat = []
            for e in emissions:
                middle_ndim = e.dim() - 3
                if middle_ndim > 0:
                    all_flat.append(e.flatten(1, middle_ndim))  # [B, H*, T, N]
                else:
                    all_flat.append(e.unsqueeze(1))  # [B, 1, T, N]
            combined = torch.cat(all_flat, dim=1)  # [B, total_heads, T, N]
            emission = combined.mean(dim=1)  # [B, T, N]
            return hmm_forward_loss_with_emission(emission, frame_lens, token_lens)

        # separate mode: flatten all heads from all layers into one batch,
        # so CTC reduction='mean' weights every head equally.
        all_flat = []
        all_frame_lens = []
        all_token_lens = []
        for e in emissions:
            middle_ndim = e.dim() - 3
            if middle_ndim > 0:
                repeat = math.prod(e.shape[1:1 + middle_ndim])
                all_flat.append(e.flatten(0, middle_ndim))  # [B*, T, N]
                all_frame_lens.append(frame_lens.repeat_interleave(repeat))
                all_token_lens.append(token_lens.repeat_interleave(repeat))
            else:
                all_flat.append(e)
                all_frame_lens.append(frame_lens)
                all_token_lens.append(token_lens)
        if not all_flat:
            return torch.zeros(1, requires_grad=True)
        return hmm_forward_loss_with_emission(
            torch.cat(all_flat, dim=0),  # [total_B*, T, N]
            torch.cat(all_frame_lens, dim=0),
            torch.cat(all_token_lens, dim=0),
        )


class CTCLossWithoutBlank(nn.Module):
    """CTC loss with disabled blank for models that do not predict blank.

    Inputs:
        logits: [B, T_max, V] frame-to-vocab scores (no blank class)
        targets: [B, S_max] int64 target token indices (padded)
        frame_lens: [B] int64 number of valid frames per sample
        token_lens: [B] int64 number of valid tokens per sample
    Returns:
        Scalar loss.
    """

    def forward(
            self,
            logits: torch.Tensor,
            targets: torch.Tensor,
            frame_lens: torch.Tensor,
            token_lens: torch.Tensor,
    ) -> torch.Tensor:
        B, T_max, V = logits.shape

        # append blank column (set to -inf, effectively disabled)
        blank_col = torch.full((B, T_max, 1), -1e9, device=logits.device, dtype=logits.dtype)
        logits = torch.cat([logits, blank_col], dim=-1)  # [B, T_max, V+1]

        log_probs = F.log_softmax(logits, dim=-1)

        return F.ctc_loss(
            log_probs.transpose(0, 1),  # [T_max, B, V+1]
            targets,
            input_lengths=frame_lens,
            target_lengths=token_lens,
            blank=V,  # global blank index
            reduction='mean',
            zero_infinity=True,
        )
