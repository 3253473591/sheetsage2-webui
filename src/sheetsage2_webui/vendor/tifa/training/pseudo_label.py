import math
from dataclasses import dataclass

import torch
from torch import Tensor

from modules.metrics import (
    compute_confidence,
    compute_determinacy,
    compute_monotonicity,
)
from training.data import (
    collate_nd,
    concat_phoneme_timing_fields,
    plan_concat_groups,
)


@dataclass(frozen=True)
class PseudoLabelBatch:
    tokens: Tensor
    spans: Tensor
    regions: Tensor
    frame_targets: Tensor
    n_mask: Tensor
    accepted: Tensor


@dataclass(frozen=True)
class PseudoLabelStudentBatch:
    spectrogram: Tensor
    tokens: Tensor
    spans: Tensor
    regions: Tensor
    frame_targets: Tensor
    t_mask: Tensor
    n_mask: Tensor


def concat_pseudo_label_groups(
    aux_sample: dict[str, Tensor],
    pseudo: PseudoLabelBatch,
    *,
    max_concat_size: int | None,
    max_concat_frames: int | None,
) -> PseudoLabelStudentBatch | None:
    """Filter raw aux fragments, then pack accepted pseudo-labels for the student."""
    raw_size = int(aux_sample.get("size", -1))
    if raw_size <= 0:
        raise ValueError("Aux batches must contain at least one raw fragment.")
    if pseudo.accepted.shape != (raw_size,):
        raise ValueError("Pseudo-label acceptance mask does not match aux batch size.")
    for key in ("spectrogram_dirty", "T"):
        if key not in aux_sample or aux_sample[key].shape[0] != raw_size:
            raise ValueError(f"Aux field '{key}' does not match aux batch size.")

    accepted = pseudo.accepted.detach().cpu().tolist()
    members = []
    for raw_index in range(raw_size):
        if not accepted[raw_index]:
            continue
        frame_count = int(aux_sample["T"][raw_index].item())
        token_count = int(pseudo.n_mask[raw_index].sum().item())
        if frame_count <= 0 or token_count <= 0:
            raise ValueError("Accepted pseudo-label fragments must be non-empty.")
        members.append(
            {
                "spectrogram": aux_sample["spectrogram_dirty"][raw_index, :frame_count],
                "tokens": pseudo.tokens[raw_index, :token_count].detach(),
                "spans": pseudo.spans[raw_index, :token_count].detach(),
                "regions": pseudo.regions[raw_index, :frame_count].detach(),
                "frame_targets": pseudo.frame_targets[raw_index, :frame_count].detach(),
                "T": aux_sample["T"][raw_index].detach(),
                "N": pseudo.n_mask[raw_index].sum().detach(),
            }
        )
    if not members:
        return None

    group_indices = plan_concat_groups(
        list(range(len(members))),
        lambda index: int(members[index]["T"].item()),
        max_concat_size=max_concat_size,
        max_concat_frames=max_concat_frames,
    )
    groups = []
    for indices in group_indices:
        group_members = [members[index] for index in indices]
        group = concat_phoneme_timing_fields(group_members)
        group["spectrogram"] = torch.cat([member["spectrogram"] for member in group_members])
        group["T"] = torch.stack([member["T"] for member in group_members]).sum()
        group["N"] = torch.stack([member["N"] for member in group_members]).sum()
        groups.append(group)

    T = torch.stack([group["T"] for group in groups])
    N = torch.stack([group["N"] for group in groups])
    spectrogram = collate_nd(
        [group["spectrogram"] for group in groups],
        pad_value=math.log(1e-5),
    )
    tokens = collate_nd([group["tokens"] for group in groups])
    spans = collate_nd([group["spans"] for group in groups])
    regions = collate_nd([group["regions"] for group in groups])
    frame_targets = collate_nd([group["frame_targets"] for group in groups])
    t_mask = torch.arange(spectrogram.shape[1], device=T.device).unsqueeze(0) < T.unsqueeze(1)
    n_mask = torch.arange(tokens.shape[1], device=N.device).unsqueeze(0) < N.unsqueeze(1)
    return PseudoLabelStudentBatch(
        spectrogram=spectrogram,
        tokens=tokens,
        spans=spans,
        regions=regions,
        frame_targets=frame_targets,
        t_mask=t_mask,
        n_mask=n_mask,
    )


def build_pseudo_labels(
    *,
    tokens: Tensor,
    spans: Tensor,
    similarity: Tensor,
    agreement: Tensor,
    t_mask: Tensor,
    n_mask: Tensor,
    vocab_size: int,
    min_agreement: float,
    min_confidence: float,
    min_determinacy: float,
    min_monotonicity: float,
    determinacy_power: float,
    determinacy_width: int | None,
    monotonicity_power: float,
    monotonicity_width: int | None,
) -> PseudoLabelBatch:
    """Validate decoded spans, compute quality gates, and rebuild frame targets."""
    B, T, N = similarity.shape
    if tokens.shape != (B, N) or spans.shape != (B, N, 2):
        raise ValueError("Pseudo-label tensor shapes do not agree.")

    frame_lengths = t_mask.sum(dim=-1)
    token_lengths = n_mask.sum(dim=-1)
    onsets = spans[..., 0]
    offsets = spans[..., 1]
    durations = offsets - onsets

    invalid_token = (((tokens <= 0) | (tokens >= vocab_size)) & n_mask).any(dim=-1)
    invalid_bounds = (((onsets < 0) | (offsets > frame_lengths.unsqueeze(1)) | (onsets > offsets)) & n_mask).any(dim=-1)
    adjacent = n_mask[:, :-1] & n_mask[:, 1:]
    nonmonotonic = ((onsets[:, 1:] < offsets[:, :-1]) & adjacent).any(dim=-1)
    empty = (frame_lengths == 0) | (token_lengths == 0)
    invalid = invalid_token | invalid_bounds | nonmonotonic | empty

    # TODO: Replace whole-item zero-span rejection after token-local masking is
    # validated against the agreement, confidence, determinacy, and monotonicity gates.
    zero_span = ((durations <= 0) & n_mask).any(dim=-1)

    confidence = compute_confidence(
        spans,
        similarity,
        t_mask,
        n_mask,
        reduction="mean",
    )
    det_numerator, det_denominator = compute_determinacy(
        spans,
        similarity,
        t_mask,
        n_mask,
        power=determinacy_power,
        width=determinacy_width,
    )
    determinacy = det_numerator / det_denominator.clamp(min=1e-8)
    monotonicity = compute_monotonicity(
        spans,
        similarity,
        t_mask,
        n_mask,
        power=monotonicity_power,
        width=monotonicity_width,
    )

    low_agreement = agreement < min_agreement
    low_confidence = confidence < min_confidence
    low_determinacy = determinacy < min_determinacy
    low_monotonicity = monotonicity < min_monotonicity
    accepted = ~(invalid | zero_span | low_agreement | low_confidence | low_determinacy | low_monotonicity)

    frame_index = torch.arange(T, device=similarity.device).view(1, T, 1)
    in_span = (
        (frame_index >= onsets.unsqueeze(1))
        & (frame_index < offsets.unsqueeze(1))
        & t_mask.unsqueeze(-1)
        & n_mask.unsqueeze(1)
    )
    region_ids = torch.arange(1, N + 1, device=similarity.device).view(1, 1, N)
    regions = (in_span.long() * region_ids).amax(dim=-1)
    token_index = (regions - 1).clamp(min=0)
    frame_targets = tokens.gather(1, token_index)
    frame_targets = frame_targets.masked_fill(regions == 0, 0)

    return PseudoLabelBatch(
        tokens=tokens,
        spans=spans,
        regions=regions,
        frame_targets=frame_targets,
        n_mask=n_mask,
        accepted=accepted,
    )
