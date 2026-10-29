import torch
import torchmetrics
from torch import Tensor


class PairConjunctionMAE(torchmetrics.Metric):
    """
    Mean joint-point error for the worst-k adjacent token pairs.

    For each adjacent pair (token i followed by token j), the joint error is
    (offset_error_i + onset_error_j) / 2.  Reports the mean over the top-k
    pairs with the largest average error.  A k value is required because the
    overall mean across all pairs would be redundant with BoundaryMAE.

    Arguments:
        vocab_size: number of token IDs (including padding 0).
        k: number of worst pairs to average over.

    Inputs:
        pred_spans  [..., N, 2]  --  predicted onset/offset in frames
        target_spans [..., N, 2]  --  ground-truth onset/offset in frames
        tokens [..., N]  --  token IDs (0 = padding)

    Output:
        Scalar MAE in frames over the top-k worst pairs.
    """

    def __init__(self, vocab_size: int, k: int, **kwargs):
        super().__init__(**kwargs)
        self.vocab_size = vocab_size
        self.k = k
        self.add_state(
            "pair_error_sum",
            default=torch.zeros(vocab_size, vocab_size), dist_reduce_fx="sum"
        )
        self.add_state(
            "pair_count",
            default=torch.zeros(vocab_size, vocab_size, dtype=torch.int64), dist_reduce_fx="sum"
        )

    def update(self, pred_spans: Tensor, target_spans: Tensor, tokens: Tensor) -> None:
        N = tokens.shape[-1]
        tokens = tokens.reshape(-1, N)
        pred_spans = pred_spans.reshape(-1, N, 2)
        target_spans = target_spans.reshape(-1, N, 2)
        B, N = tokens.shape
        onset_err = (pred_spans[..., 0] - target_spans[..., 0]).abs().float()  # [B, N]
        offset_err = (pred_spans[..., 1] - target_spans[..., 1]).abs().float()  # [B, N]

        for b in range(B):
            valid = tokens[b] != 0
            valid_indices = torch.where(valid)[0]
            if len(valid_indices) < 2:
                continue
            for idx in range(len(valid_indices) - 1):
                i_pos = valid_indices[idx]
                j_pos = valid_indices[idx + 1]
                tid_i = tokens[b, i_pos].item()
                tid_j = tokens[b, j_pos].item()
                joint_err = (offset_err[b, i_pos] + onset_err[b, j_pos]) / 2.0
                self.pair_error_sum[tid_i, tid_j] += joint_err
                self.pair_count[tid_i, tid_j] += 1

    def compute(self) -> Tensor:
        result = self._worst_pairs()
        if result is None:
            return torch.tensor(0.0)
        _, flat_ids = result
        flat_error = self.pair_error_sum.flatten()
        flat_count = self.pair_count.flatten()
        top_error = flat_error[flat_ids].sum()
        top_count = flat_count[flat_ids].sum()
        if top_count == 0:
            return torch.tensor(0.0)
        return top_error / top_count.float()

    def compute_top_k(self) -> dict[tuple[int, int], Tensor]:
        """Return the worst-k token pairs and their per-pair joint errors.

        Returns a mapping ``{(i, j): value}`` for the k worst adjacent pairs,
        where *i* is the preceding token ID and *j* the following token ID.
        """
        result = self._worst_pairs()
        if result is None:
            return {}
        values, flat_ids = result
        row_ids = (flat_ids // self.vocab_size).tolist()
        col_ids = (flat_ids % self.vocab_size).tolist()
        return {(int(ri), int(ci)): v for ri, ci, v in zip(row_ids, col_ids, values)}

    def _worst_pairs(self) -> tuple[Tensor, Tensor] | None:
        active = self.pair_count > 0
        if not active.any():
            return None
        mean_per_pair = self.pair_error_sum / self.pair_count.clamp(min=1).float()
        mean_per_pair[~active] = -1.0
        flat_mean = mean_per_pair.flatten()
        top_k = min(self.k, active.sum().item())
        return torch.topk(flat_mean, top_k)
