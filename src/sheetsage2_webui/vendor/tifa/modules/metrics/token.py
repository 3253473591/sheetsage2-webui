import torch
import torchmetrics
from torch import Tensor


class PhonemeErrorRate(torchmetrics.Metric):
    """Fraction of token positions where the predicted phoneme differs from target.

    Inputs:
        preds  [..., N, V]  --  per-position logits
        target [..., N]     --  ground-truth token IDs
        mask   [..., N]     --  bool, True for valid positions

    Output:
        Scalar in [0, 1].  Lower is better.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_state("errors", default=torch.tensor(0, dtype=torch.int64), dist_reduce_fx="sum")
        self.add_state("total", default=torch.tensor(0, dtype=torch.int64), dist_reduce_fx="sum")

    def update(self, preds: Tensor, target: Tensor, mask: Tensor) -> None:
        incorrect = (preds.argmax(dim=-1) != target) & mask
        self.errors += incorrect.long().sum()
        self.total += mask.long().sum()

    def compute(self) -> Tensor:
        if self.total == 0:
            return torch.tensor(0.0)
        return self.errors.float() / self.total.float()
