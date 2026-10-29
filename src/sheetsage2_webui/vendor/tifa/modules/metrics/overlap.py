import torch
import torchmetrics
from torch import Tensor


def compute_overlap(
    pred_spans: Tensor,
    target_spans: Tensor,
    tokens: Tensor,
) -> tuple[Tensor, Tensor, Tensor]:
    """Per-sample overlap accumulators (batched, stateless).

    Args:
        pred_spans: ``[..., N, 2]`` predicted onset/offset.
        target_spans: ``[..., N, 2]`` ground-truth onset/offset.
        tokens: ``[..., N]`` token IDs (0 = padding).

    Returns:
        ``(overlap_sum, pred_sum, gt_sum)`` each shape ``[...]`` — raw
        accumulators per sample.  Caller computes
        ``overlap_sum / pred_sum`` (precision) and
        ``overlap_sum / gt_sum`` (recall).
    """
    mask = tokens != 0
    pred_len = (pred_spans[..., 1] - pred_spans[..., 0]).clamp(min=0)
    gt_len = (target_spans[..., 1] - target_spans[..., 0]).clamp(min=0)
    overlap_start = torch.maximum(pred_spans[..., 0], target_spans[..., 0])
    overlap_end = torch.minimum(pred_spans[..., 1], target_spans[..., 1])
    overlap_len = (overlap_end - overlap_start).clamp(min=0)

    overlap_sum = (overlap_len * mask).sum(dim=-1)
    pred_sum = (pred_len * mask).sum(dim=-1)
    gt_sum = (gt_len * mask).sum(dim=-1)
    return overlap_sum, pred_sum, gt_sum


class OverlapRatioCollection(torchmetrics.Metric):
    """
    Overlap precision and recall for token-level forced-alignment spans.

    Each token defines an interval [onset, offset].  Overlap is the length
    of the intersection between predicted and ground-truth intervals.
    Precision = total_overlap / total_predicted_length; recall =
    total_overlap / total_ground_truth_length.

    When k is set, precision and recall each independently select their own
    worst-k token IDs (lowest per-ID ratio for that metric).

    Arguments:
        template: format string with ``{}`` placeholder for the metric
            name, e.g. ``"overlap_{}@3"`` produces ``"overlap_precision@3"``
            and ``"overlap_recall@3"``.
        vocab_size: number of token IDs (including padding 0). Required when k is set.
        k: if set, compute each metric over only its own worst-k token IDs.

    Inputs:
        pred_spans  [..., N, 2]  --  predicted onset/offset in frames
        target_spans [..., N, 2]  --  ground-truth onset/offset in frames
        tokens [..., N]  --  token IDs (0 = padding)

    Output:
        Dict with formatted precision and recall keys.
    """

    def __init__(
            self, template: str = "overlap_{}",
            vocab_size: int | None = None, k: int | None = None, **kwargs
    ):
        super().__init__(**kwargs)
        if k is not None and vocab_size is None:
            raise ValueError("vocab_size is required when k is set")
        self.template = template
        self.vocab_size = vocab_size
        self.k = k

        if k is None:
            self.add_state("overlap_sum", default=torch.tensor(0.0), dist_reduce_fx="sum")
            self.add_state("pred_sum", default=torch.tensor(0.0), dist_reduce_fx="sum")
            self.add_state("gt_sum", default=torch.tensor(0.0), dist_reduce_fx="sum")
        else:
            self.add_state("overlap_sum", default=torch.zeros(vocab_size), dist_reduce_fx="sum")
            self.add_state("pred_sum", default=torch.zeros(vocab_size), dist_reduce_fx="sum")
            self.add_state("gt_sum", default=torch.zeros(vocab_size), dist_reduce_fx="sum")

    def _format(self, name: str) -> str:
        return self.template.format(name)

    def update(self, pred_spans: Tensor, target_spans: Tensor, tokens: Tensor) -> None:
        if self.k is None:
            overlap_sum, pred_sum, gt_sum = compute_overlap(
                pred_spans, target_spans, tokens,
            )
            self.overlap_sum += overlap_sum.sum()
            self.pred_sum += pred_sum.sum()
            self.gt_sum += gt_sum.sum()
        else:
            mask = tokens != 0
            pred_spans_f = pred_spans.float()
            target_spans_f = target_spans.float()
            pred_len = (pred_spans_f[..., 1] - pred_spans_f[..., 0]).clamp(min=0)
            gt_len = (target_spans_f[..., 1] - target_spans_f[..., 0]).clamp(min=0)
            overlap_start = torch.maximum(pred_spans_f[..., 0], target_spans_f[..., 0])
            overlap_end = torch.minimum(pred_spans_f[..., 1], target_spans_f[..., 1])
            overlap_len = (overlap_end - overlap_start).clamp(min=0)
            valid_tokens = tokens[mask]
            self.overlap_sum.index_add_(0, valid_tokens, overlap_len[mask].float())
            self.pred_sum.index_add_(0, valid_tokens, pred_len[mask].float())
            self.gt_sum.index_add_(0, valid_tokens, gt_len[mask].float())

    def compute(self) -> dict[str, Tensor]:
        if self.k is None:
            precision = self.overlap_sum / (self.pred_sum + 1e-6)
            recall = self.overlap_sum / (self.gt_sum + 1e-6)
            return {self._format("precision"): precision, self._format("recall"): recall}

        precision_result = self._worst_ids(self.pred_sum)
        recall_result = self._worst_ids(self.gt_sum)
        if precision_result is None:
            return {self._format("precision"): torch.tensor(0.0), self._format("recall"): torch.tensor(0.0)}

        _, precision_ids = precision_result
        _, recall_ids = recall_result
        precision = self.overlap_sum[precision_ids].sum() / (self.pred_sum[precision_ids].sum() + 1e-6)
        recall = self.overlap_sum[recall_ids].sum() / (self.gt_sum[recall_ids].sum() + 1e-6)
        return {self._format("precision"): precision, self._format("recall"): recall}

    def compute_top_k(self) -> dict[str, dict[int, Tensor]]:
        """Return the worst-k token IDs and their per-ID metric values.

        Only available when *k* is set.  Returns a mapping from each formatted
        metric name to ``{token_id: value}`` for its own worst-k IDs.
        """
        if self.k is None:
            return {}

        precision_dict: dict[int, Tensor] = {}
        recall_dict: dict[int, Tensor] = {}
        precision_result = self._worst_ids(self.pred_sum)
        recall_result = self._worst_ids(self.gt_sum)
        if precision_result is not None:
            values, ids = precision_result
            precision_dict = {int(tid): val for tid, val in zip(ids.tolist(), values)}
        if recall_result is not None:
            values, ids = recall_result
            recall_dict = {int(tid): val for tid, val in zip(ids.tolist(), values)}
        return {self._format("precision"): precision_dict, self._format("recall"): recall_dict}

    def _worst_ids(self, divisor: Tensor) -> tuple[Tensor, Tensor] | None:
        """Find worst-k IDs by lowest overlap / divisor ratio."""
        active = divisor > 0
        if not active.any():
            return None
        per_id_ratio = self.overlap_sum / divisor.clamp(min=1)
        per_id_ratio[~active] = float("inf")
        top_k = min(self.k, active.sum().item())
        return torch.topk(per_id_ratio, top_k, largest=False)
