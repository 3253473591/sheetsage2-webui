import typing
from typing import Any

import lightning.pytorch
import torch
import torch.distributed
from torch import Tensor


class WordRewards:
    """Per-word reward vector for PathRanker accumulation.

    Attributes:
        item_idx: dataset item index.
        word_idx: word index within the item.
        rewards: ``[w]`` tensor where *w* is the word's complete candidate count.
    """

    __slots__ = ("item_idx", "word_idx", "rewards")

    def __init__(self, item_idx: int, word_idx: int, rewards: Tensor):
        self.item_idx = item_idx
        self.word_idx = word_idx
        self.rewards = rewards


class RankingModule(typing.Protocol):
    def set_best_paths(self, best_paths: dict[int, dict[int, int]] | None) -> None: ...


def rank_rewards(rank_size: int) -> Tensor:
    """Rank-based rewards with step 2 centered on zero, best first. rank_size=4 -> [3,1,-1,-3]."""
    return torch.arange(rank_size - 1, -rank_size, -2, dtype=torch.long)


class PathRanker:
    """Per-word accumulated rewards with sparse auto-creation.

    Stores ``{item_idx: {word_idx: Tensor([alt0_reward, ...])}}``.
    """

    def __init__(self, gamma: float):
        self.gamma = gamma
        self._rewards: dict[int, dict[int, Tensor]] = {}

    def __len__(self) -> int:
        return len(self._rewards)

    def known_items(self) -> list[int]:
        return list(self._rewards)

    def get_best_path(self, item_idx: int) -> dict[int, int]:
        """``{word_idx: best_candidate_idx}`` for known ambiguous words. Empty if unknown."""
        segs = self._rewards.get(item_idx)
        if not segs:
            return {}
        return {p: int(r.argmax().item()) for p, r in segs.items()}

    def sync_results(self, results: list[WordRewards]) -> None:
        """Apply per-batch deltas, all-gathering across DDP ranks."""
        if not torch.distributed.is_initialized() or torch.distributed.get_world_size() < 2:
            for r in results:
                self._apply_rewards(r.item_idx, r.word_idx, r.rewards)
            return

        world_size = torch.distributed.get_world_size()
        all_results: list[list[WordRewards]] = [None] * world_size
        torch.distributed.all_gather_object(all_results, results)
        for rank_results in all_results:
            for r in rank_results:
                self._apply_rewards(r.item_idx, r.word_idx, r.rewards)

    def _apply_rewards(self, item_idx: int, word_idx: int, rewards: Tensor) -> None:
        """Accumulate a per-word reward vector, clamped to [0, inf)."""
        self._ensure_word(item_idx, word_idx, rewards.numel())
        seg = self._rewards[item_idx][word_idx]
        seg.add_(rewards.cpu().to(dtype=torch.long))
        seg.clamp_(min=0)

    def _ensure_word(self, item_idx: int, word_idx: int, width: int):
        if item_idx not in self._rewards:
            self._rewards[item_idx] = {}
        segs = self._rewards[item_idx]
        if word_idx not in segs:
            segs[word_idx] = torch.zeros(width, dtype=torch.long)
        elif segs[word_idx].numel() < width:
            old = segs[word_idx]
            new = torch.zeros(width, dtype=torch.long)
            new[:len(old)] = old
            segs[word_idx] = new

    def decay_rewards(self):
        for segs in self._rewards.values():
            for rewards in segs.values():
                rewards.copy_(
                    rewards.float().mul_(self.gamma).round_().long()
                )

    def state_dict(self) -> dict[str, dict[str, Tensor]]:
        """``{"words": {"itemIdx,wordIdx": per_candidate_rewards}}``."""
        sd: dict[str, Tensor] = {}
        for i, segs in self._rewards.items():
            for p, rewards in segs.items():
                sd[f"{i},{p}"] = rewards.clone()
        return {"words": sd}

    def load_state_dict(self, state_dict: dict[str, dict[str, Tensor]]):
        if "words" not in state_dict:
            raise ValueError("Expected word-ranking state; reinitialize rankings for the re-binarized data.")
        self._rewards.clear()
        for key, tensor in state_dict["words"].items():
            i_str, p_str = key.split(",")
            i = int(i_str)
            p = int(p_str)
            self._rewards.setdefault(i, {})[p] = tensor


class IterativeRanking(lightning.pytorch.Callback):
    """Per-step path ranking with epoch-boundary decay and DDP sync.

    Requires *pl_module* to implement :class:`RankingModule`.
    """

    def __init__(self, gamma: float, update_every_n_epochs: int) -> None:
        super().__init__()
        self.gamma = gamma
        self.update_every_n_epochs = update_every_n_epochs
        self.ranker: PathRanker | None = None
        self._best_paths: dict[int, dict[int, int]] | None = None
        self._pending_ranker_state: dict[str, dict[str, Tensor]] | None = None

    def on_train_start(
            self,
            trainer: lightning.pytorch.Trainer,
            pl_module: RankingModule,
    ) -> None:
        self.ranker = PathRanker(self.gamma)
        if self._pending_ranker_state is not None:
            self.ranker.load_state_dict(self._pending_ranker_state)
            self._pending_ranker_state = None
        self._best_paths = {}

    def on_train_batch_start(
            self,
            trainer: lightning.pytorch.Trainer,
            pl_module: RankingModule,
            batch: dict[str, torch.Tensor],
            batch_idx: int,
    ) -> None:
        pl_module.set_best_paths(self._best_paths)

    def on_train_batch_end(
            self,
            trainer: lightning.pytorch.Trainer,
            pl_module: RankingModule,
            outputs: dict[str, torch.Tensor] | torch.Tensor,
            batch: dict[str, torch.Tensor],
            batch_idx: int,
    ) -> None:
        if self.ranker is None:
            return
        if isinstance(outputs, dict):
            results = outputs.get("ranking_results")
        else:
            results = None
        if results is not None:
            self.ranker.sync_results(results)

    def on_train_epoch_start(
            self,
            trainer: lightning.pytorch.Trainer,
            pl_module: RankingModule,
    ) -> None:
        if self.ranker is None:
            return
        if trainer.current_epoch > 0 and trainer.current_epoch % self.update_every_n_epochs == 0:
            self.ranker.decay_rewards()
            self._best_paths = {
                i: self.ranker.get_best_path(i)
                for i in self.ranker.known_items()
            }
            pl_module.set_best_paths(self._best_paths)

    def state_dict(self) -> dict[str, Any]:
        sd = super().state_dict()
        if self.ranker is not None:
            sd["iterative_ranker"] = self.ranker.state_dict()
        if self._best_paths is not None:
            sd["iterative_best_paths"] = {
                f"{i},{p}": a
                for i, segs in self._best_paths.items()
                for p, a in segs.items()
            }
        return sd

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Restore ranker state, deferring if ``on_train_start`` hasn't run yet."""
        super().load_state_dict(state_dict)
        pending = state_dict.get("iterative_ranker")
        if pending is None:
            return
        if self.ranker is not None:
            self.ranker.load_state_dict(pending)
            saved_paths = state_dict.get("iterative_best_paths")
            if saved_paths is not None:
                self._best_paths = {}
                for key, best_alt in saved_paths.items():
                    i_str, p_str = key.split(",")
                    self._best_paths.setdefault(int(i_str), {})[int(p_str)] = int(best_alt)
            else:
                self._best_paths = {
                    i: self.ranker.get_best_path(i)
                    for i in self.ranker.known_items()
                }
        else:
            self._pending_ranker_state = pending
