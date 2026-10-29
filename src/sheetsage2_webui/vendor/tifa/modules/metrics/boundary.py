import torch
import torchmetrics
from torch import Tensor


def compute_boundary_error_rate(
    pred_spans: Tensor,
    target_spans: Tensor,
    tokens: Tensor,
    tolerance: float,
    mode: str,
) -> tuple[Tensor, Tensor]:
    """Per-sample boundary error counts (batched, stateless).

    Args:
        pred_spans: ``[..., N, 2]`` predicted onset/offset.
        target_spans: ``[..., N, 2]`` ground-truth onset/offset.
        tokens: ``[..., N]`` token IDs (0 = padding).
        tolerance: maximum frame distance for a correct boundary.
        mode: ``"onset"``, ``"offset"``, or ``"both"``.

    Returns:
        ``(incorrect, total)`` each shape ``[...]`` — raw counts per
        sample.  Caller computes ``incorrect / total`` for the BER.
    """
    mask = tokens != 0
    onset_err = (pred_spans[..., 0] - target_spans[..., 0]).abs()
    offset_err = (pred_spans[..., 1] - target_spans[..., 1]).abs()

    if mode == "onset":
        incorrect = (onset_err > tolerance) & mask
    elif mode == "offset":
        incorrect = (offset_err > tolerance) & mask
    else:  # both
        incorrect = ((onset_err > tolerance) | (offset_err > tolerance)) & mask

    return incorrect.sum(dim=-1), mask.sum(dim=-1)


def compute_boundary_mae(
    pred_spans: Tensor,
    target_spans: Tensor,
    tokens: Tensor,
    mode: str,
) -> tuple[Tensor, Tensor]:
    """Per-sample boundary mean absolute error accumulators (batched, stateless).

    Args:
        pred_spans: ``[..., N, 2]`` predicted onset/offset.
        target_spans: ``[..., N, 2]`` ground-truth onset/offset.
        tokens: ``[..., N]`` token IDs (0 = padding).
        mode: ``"onset"`` or ``"offset"``.

    Returns:
        ``(error_sum, count)`` each shape ``[...]`` — raw accumulators
        per sample.  Caller computes ``error_sum / count`` for the MAE.
    """
    dim = 0 if mode == "onset" else 1
    mask = tokens != 0
    errors = (pred_spans[..., dim] - target_spans[..., dim]).abs()
    return (errors * mask).sum(dim=-1), mask.sum(dim=-1)


class BoundaryErrorRate(torchmetrics.Metric):
    """
    Ratio of incorrectly aligned tokens to total tokens, where a token is
    "aligned" when its boundary error is within a tolerance.

    Arguments:
        tolerance: maximum frame distance for a boundary to be considered correct.
        mode: "onset", "offset", or "both".
        vocab_size: number of token IDs (including padding 0). Required when k is set.
        k: if set, compute error rate only over the top-k token IDs with the
           largest per-ID error rate.

    Inputs:
        pred_spans  [..., N, 2]  --  predicted onset/offset in frames
        target_spans [..., N, 2]  --  ground-truth onset/offset in frames
        tokens [..., N]  --  token IDs (0 = padding)

    Output:
        Scalar error rate in [0, 1].
    """

    def __init__(self, tolerance: int, mode: str, vocab_size: int | None = None, k: int | None = None, **kwargs):
        super().__init__(**kwargs)
        if mode not in ("onset", "offset", "both"):
            raise ValueError(f"mode must be 'onset', 'offset', or 'both', got '{mode}'")
        if k is not None and vocab_size is None:
            raise ValueError("vocab_size is required when k is set")
        self.tolerance = tolerance
        self.mode = mode
        self.vocab_size = vocab_size
        self.k = k

        if k is None:
            self.add_state("incorrect", default=torch.tensor(0, dtype=torch.int64), dist_reduce_fx="sum")
            self.add_state("total", default=torch.tensor(0, dtype=torch.int64), dist_reduce_fx="sum")
        else:
            self.add_state("incorrect", default=torch.zeros(vocab_size, dtype=torch.int64), dist_reduce_fx="sum")
            self.add_state("total", default=torch.zeros(vocab_size, dtype=torch.int64), dist_reduce_fx="sum")

    def update(self, pred_spans: Tensor, target_spans: Tensor, tokens: Tensor) -> None:
        if self.k is None:
            incorrect, total = compute_boundary_error_rate(
                pred_spans, target_spans, tokens, self.tolerance, self.mode,
            )
            self.incorrect += incorrect.sum()
            self.total += total.sum()
        else:
            mask = tokens != 0
            onset_err = (pred_spans[..., 0] - target_spans[..., 0]).abs().float()
            offset_err = (pred_spans[..., 1] - target_spans[..., 1]).abs().float()

            if self.mode == "onset":
                incorrect = (onset_err > self.tolerance) & mask
            elif self.mode == "offset":
                incorrect = (offset_err > self.tolerance) & mask
            else:  # both
                incorrect = ((onset_err > self.tolerance) | (offset_err > self.tolerance)) & mask

            valid_tokens = tokens[mask]
            self.incorrect.index_add_(0, valid_tokens, incorrect[mask].long())
            self.total.index_add_(0, valid_tokens, torch.ones_like(valid_tokens, dtype=torch.int64))

    def compute(self) -> Tensor:
        if self.k is None:
            if self.total == 0:
                return torch.tensor(0.0)
            return self.incorrect.float() / self.total.float()

        result = self._worst_ids()
        if result is None:
            return torch.tensor(0.0)
        _, top_ids = result
        top_incorrect = self.incorrect[top_ids].sum()
        top_total = self.total[top_ids].sum()
        if top_total == 0:
            return torch.tensor(0.0)
        return top_incorrect.float() / top_total.float()

    def compute_top_k(self) -> dict[int, Tensor]:
        """Return the worst-k token IDs and their per-ID error rates.

        Only available when *k* is set.  Returns a mapping from token ID
        to error rate for the k worst IDs.
        """
        assert self.k is not None, "compute_top_k() requires k to be set"
        result = self._worst_ids()
        if result is None:
            return {}
        values, top_ids = result
        return {int(tid): val for tid, val in zip(top_ids.tolist(), values)}

    def _worst_ids(self) -> tuple[Tensor, Tensor] | None:
        active = self.total > 0
        if not active.any():
            return None
        per_id_rate = self.incorrect.float() / self.total.float().clamp(min=1)
        per_id_rate[~active] = -1.0
        top_k = min(self.k, active.sum().item())
        return torch.topk(per_id_rate, top_k)


class BoundaryMAE(torchmetrics.Metric):
    """
    Mean absolute error of token boundaries in frames.

    Arguments:
        mode: "onset" or "offset".
        vocab_size: number of token IDs (including padding 0). Required when k is set.
        k: if set, compute MAE only over the top-k token IDs with the largest
           per-ID average error.

    Inputs:
        pred_spans  [..., N, 2]  --  predicted onset/offset in frames
        target_spans [..., N, 2]  --  ground-truth onset/offset in frames
        tokens [..., N]  --  token IDs (0 = padding)

    Output:
        Scalar MAE in frames.
    """

    def __init__(self, mode: str, vocab_size: int | None = None, k: int | None = None, **kwargs):
        super().__init__(**kwargs)
        if mode not in ("onset", "offset"):
            raise ValueError(f"mode must be 'onset' or 'offset', got '{mode}'")
        if k is not None and vocab_size is None:
            raise ValueError("vocab_size is required when k is set")
        self.mode = mode
        self.vocab_size = vocab_size
        self.k = k

        self._dim = 0 if mode == "onset" else 1

        if k is None:
            self.add_state("error_sum", default=torch.tensor(0.0), dist_reduce_fx="sum")
            self.add_state("total", default=torch.tensor(0, dtype=torch.int64), dist_reduce_fx="sum")
        else:
            self.add_state("error_sum", default=torch.zeros(vocab_size), dist_reduce_fx="sum")
            self.add_state("count", default=torch.zeros(vocab_size, dtype=torch.int64), dist_reduce_fx="sum")

    def update(self, pred_spans: Tensor, target_spans: Tensor, tokens: Tensor) -> None:
        if self.k is None:
            error_sum, count = compute_boundary_mae(
                pred_spans, target_spans, tokens, self.mode,
            )
            self.error_sum += error_sum.sum()
            self.total += count.sum()
        else:
            mask = tokens != 0
            errors = (pred_spans[..., self._dim] - target_spans[..., self._dim]).abs().float()
            valid_tokens = tokens[mask]
            valid_errors = errors[mask]
            self.error_sum.index_add_(0, valid_tokens, valid_errors.float())
            self.count.index_add_(0, valid_tokens, torch.ones_like(valid_tokens, dtype=torch.int64))

    def compute(self) -> Tensor:
        if self.k is None:
            if self.total == 0:
                return torch.tensor(0.0)
            return self.error_sum / self.total.float()

        result = self._worst_ids()
        if result is None:
            return torch.tensor(0.0)
        _, top_ids = result
        top_error = self.error_sum[top_ids].sum()
        top_count = self.count[top_ids].sum()
        if top_count == 0:
            return torch.tensor(0.0)
        return top_error / top_count.float()

    def compute_top_k(self) -> dict[int, Tensor]:
        """Return the worst-k token IDs and their per-ID MAE values.

        Only available when *k* is set.  Returns a mapping from token ID
        to MAE in frames for the k worst IDs.
        """
        if self.k is None:
            return {}

        result = self._worst_ids()
        if result is None:
            return {}
        values, top_ids = result
        return {int(tid): val for tid, val in zip(top_ids.tolist(), values)}

    def _worst_ids(self) -> tuple[Tensor, Tensor] | None:
        active = self.count > 0
        if not active.any():
            return None
        mean_per_id = self.error_sum / self.count.clamp(min=1).float()
        mean_per_id[~active] = -1.0
        top_k = min(self.k, active.sum().item())
        return torch.topk(mean_per_id, top_k)
