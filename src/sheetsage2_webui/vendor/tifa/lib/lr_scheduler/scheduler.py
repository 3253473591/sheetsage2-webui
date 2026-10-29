from typing import Union

import numpy as np
import torch
from torch.optim.lr_scheduler import LRScheduler


class WarmupLR(LRScheduler):
    """The WarmupLR scheduler

    This scheduler is almost same as NoamLR Scheduler except for following
    difference:

    NoamLR:
        lr = optimizer.lr * model_size ** -0.5
             * min(step ** -0.5, step * warmup_step ** -1.5)
    WarmupLR:
        lr = optimizer.lr * warmup_step ** 0.5
             * min(step ** -0.5, step * warmup_step ** -1.5)

    Note that the maximum lr equals to optimizer.lr in this scheduler.

    """

    def __init__(
            self,
            optimizer: torch.optim.Optimizer,
            warmup_steps: Union[int, float] = 5000,
            min_lr=2e-5,
            last_epoch: int = -1,
    ):
        self.warmup_steps = warmup_steps
        self.min_lr = min_lr
        super().__init__(optimizer, last_epoch)

    def get_lr(self):
        step_num = self.last_epoch + 1
        lrs = []
        for lr in self.base_lrs:
            if self.warmup_steps == 0:
                lr = lr * step_num ** -0.5
                if lr < self.min_lr:
                    lr = self.min_lr
            else:
                lr = lr * self.warmup_steps ** 0.5 * min(step_num ** -0.5, step_num * self.warmup_steps ** -1.5)
                if lr < self.min_lr and step_num > self.warmup_steps:
                    lr = self.min_lr
            lrs.append(lr)
        return lrs

    def set_step(self, step: int):
        self.last_epoch = step


class WarmupDecayingCosineAnnealingWarmRestarts(LRScheduler):
    """Warmup + decaying-peak SGDR learning-rate scheduler.

    This is cosine annealing with warm restarts (SGDR, Stochastic Gradient
    Descent with warm Restarts) in which the upper and lower bounds decay at
    every restart. After each restart the learning rate returns to a peak that
    is lower than the previous one, so the schedule keeps oscillating while its
    overall envelope decreases toward convergence.

    The learning rate has two phases (see :meth:`_compute_lr` for the actual
    computation):

    1. Warmup phase (``step < warmup_steps``):
        Linear ramp from 0 up to ``max_lr``::

            lr = step * (max_lr / warmup_steps)

    2. Cosine annealing with periodic warm restarts (``step >= warmup_steps``):
        Let ``t = step - warmup_steps`` be the number of steps elapsed since
        warmup finished. Within a cycle the learning rate follows a cosine
        curve smoothly decreasing from the upper bound to the lower bound, then
        warm-restarts back to the upper bound at the end of the cycle. The
        bounds of the ``k``-th cycle are both scaled by ``peak_decay ** k`` so
        that the peak of each cycle decreases over time. Two cycle-length
        schemes are available, selected by ``cycle_mult``:

        * ``cycle_mult == 1`` (default): fixed-length cycles, every cycle lasts
          ``cycle_steps`` steps::

            k     = t // cycle_steps
            T_cur = t %  cycle_steps
            T_i   = cycle_steps

        * ``cycle_mult > 1``: geometrically growing cycles, the ``k``-th cycle
          lasts ``cycle_steps * cycle_mult ** k`` steps::

            k     = floor(log(t * (cycle_mult - 1) / cycle_steps + 1) / log(cycle_mult))
            T_cur = t - cycle_steps * (cycle_mult ** k - 1) / (cycle_mult - 1)
            T_i   = cycle_steps * cycle_mult ** k

        In both cases the learning rate is::

            lo = (min_lr * peak_decay ** k) if decay_floor else min_lr
            hi = max_lr * peak_decay ** k
            lr = lo + 0.5 * (hi - lo) * (1 + cos(pi * T_cur / T_i))

        Because ``t`` is measured from the end of warmup, the first cosine cycle
        starts at ``T_cur == 0`` exactly when warmup finishes, so the schedule
        is continuous (warmup ends at ``max_lr`` and the cosine starts there
        too -- no gap/jump).

    Args:
        optimizer: The optimizer to schedule.
        warmup_steps: Number of linear-warmup steps before cosine annealing.
        max_lr: Peak learning rate (top of the first cosine cycle).
        min_lr: Trough learning rate (bottom of the first cosine cycle).
        cycle_steps: Number of steps in the first cosine annealing cycle.
        peak_decay: Multiplier applied to the bounds at every restart, e.g.
            ``0.98`` lowers them by 2% per cycle. Use ``1.0`` to disable decay
            (classic SGDR).
        decay_floor: If ``True`` (default) both the peak and the trough decay
            by ``peak_decay`` each restart. If ``False`` only the peak decays
            and the trough stays fixed at ``min_lr``.
        cycle_mult: Cycle-length scheme. ``1.0`` for fixed-length cycles;
            ``> 1.0`` for geometrically growing cycles (e.g. ``2.0`` doubles the
            cycle length after each restart).
        cycle_boundary: Where a cosine cycle boundary falls. ``"peak"`` (default)
            puts the peak (``max_lr``) on the boundary -- the classic SGDR phase
            where the trough is reached one step before the restart. ``"trough"``
            shifts the phase so the trough (``min_lr``) lands exactly on the
            boundary, so checkpoints saved on cycle boundaries capture the
            lowest lr instead of the post-restart peak.
        last_epoch: Index of the previous step; ``-1`` starts from scratch.
    """

    def __init__(
            self,
            optimizer: torch.optim.Optimizer,
            warmup_steps: Union[int, float] = 2500,
            max_lr: float = 4e-4,
            min_lr: float = 5e-5,
            cycle_steps: Union[int, float] = 15000,
            peak_decay: float = 0.98,
            cycle_mult: float = 1.0,
            decay_floor: bool = True,
            cycle_boundary: str = "peak",
            last_epoch: int = -1,
    ):
        if warmup_steps < 0:
            raise ValueError(f"warmup_steps must be >= 0, got {warmup_steps}.")
        if cycle_steps <= 0:
            raise ValueError(f"cycle_steps must be > 0, got {cycle_steps}.")
        if max_lr < min_lr:
            raise ValueError(f"max_lr ({max_lr}) must be >= min_lr ({min_lr}).")
        if cycle_mult < 1.0:
            raise ValueError(f"cycle_mult must be >= 1.0, got {cycle_mult}.")
        if cycle_boundary not in ("peak", "trough"):
            raise ValueError(f"cycle_boundary must be 'peak' or 'trough', got {cycle_boundary!r}.")
        self.warmup_steps = warmup_steps
        self.max_lr = max_lr
        self.min_lr = min_lr
        self.cycle_steps = cycle_steps
        self.peak_decay = peak_decay
        self.cycle_mult = cycle_mult
        self.decay_floor = decay_floor
        self.cycle_boundary = cycle_boundary
        super().__init__(optimizer, last_epoch)

    def _compute_lr(self, step_num: int) -> float:
        # last_epoch can briefly be -1 before the initial step; clamp it.
        step_num = max(step_num, 0)
        # Phase 1: linear warmup.
        if step_num < self.warmup_steps:
            return step_num * (self.max_lr / self.warmup_steps)

        # Phase 2: cosine annealing with warm restarts. The cycle phase is
        # measured from the end of warmup so the schedule connects to it.
        t = step_num - self.warmup_steps
        cycle, T_cur, T_i = self._cycle_position(t)

        decay = self.peak_decay ** cycle
        lo = self.min_lr * decay if self.decay_floor else self.min_lr
        # Guard against the upper bound decaying below the (fixed) floor, which
        # would otherwise invert the cosine curve when decay_floor is False.
        hi = max(self.max_lr * decay, lo)
        return float(lo + 0.5 * (hi - lo) * (1 + np.cos(np.pi * T_cur / T_i)))

    def _cycle_position(self, t):
        """Return ``(cycle_index, T_cur, T_i)`` for post-warmup step ``t``.

        With ``cycle_boundary == "peak"`` (default) each cycle starts at its peak
        on the boundary, so ``T_cur`` runs over ``[0, T_i)`` and the trough is
        reached one step before the next boundary -- the boundary step sits at
        ``max_lr``.

        With ``cycle_boundary == "trough"`` the phase is shifted so each cycle
        ends at its trough exactly on the boundary (``T_cur`` over ``(0, T_i]``)
        while ``t == 0`` still starts at the peak to connect with warmup. This
        way checkpoints saved on cycle boundaries land on ``min_lr`` instead of
        the post-restart ``max_lr``.
        """
        trough = self.cycle_boundary == "trough" and t > 0
        if self.cycle_mult == 1.0:
            if trough:
                cycle = (t - 1) // self.cycle_steps
                T_cur = t - cycle * self.cycle_steps
            else:
                cycle = t // self.cycle_steps
                T_cur = t % self.cycle_steps
            return cycle, T_cur, self.cycle_steps

        log_pos = np.log(t * (self.cycle_mult - 1) / self.cycle_steps + 1) / np.log(self.cycle_mult)
        cycle = int(np.ceil(log_pos)) - 1 if trough else int(log_pos)
        cycle_start = self.cycle_steps * (self.cycle_mult ** cycle - 1) / (self.cycle_mult - 1)
        T_i = self.cycle_steps * self.cycle_mult ** cycle
        return cycle, t - cycle_start, T_i

    def get_lr(self):
        # Index the schedule directly by last_epoch (the scheduler step count),
        # so the lr applied at step ``s`` is exactly ``_compute_lr(s)``. This is
        # what makes cycle_boundary="trough" actually land min_lr on the
        # boundary step (a previous ``last_epoch + 1`` shifted it onto the peak).
        lr = self._compute_lr(self.last_epoch)
        return [lr for _ in self.base_lrs]

    def set_step(self, step: int):
        self.last_epoch = step


class WarmupDecayingWSDWarmRestarts(LRScheduler):
    """Warmup + plateau/cooldown WSD-style warm restarts with decaying peaks.

    This keeps the structure of ``WarmupDecayingCosineAnnealingWarmRestarts``,
    but adds a stable high-LR plateau at the beginning of every cycle.

    Per cycle::

        peak_k  ───────────────────────╲
                                        ╲
                                         ╲ trough_k
                                           ↑ restart to peak_(k+1)

    where::

        peak_k   = max_lr * peak_decay ** k
        trough_k = min_lr * peak_decay ** k    (if decay_floor=True)

    ``stable_ratio`` controls the fraction of each cycle spent exactly at the
    current cycle peak. The remaining fraction is a cosine cooldown.

    Examples:
        stable_ratio=0.0 -> equivalent to a full-cycle cosine schedule.
        stable_ratio=0.9 -> 90% plateau + 10% cosine cooldown.

    Args:
        optimizer: The optimizer to schedule.
        warmup_steps: Number of linear warmup steps.
        max_lr: Peak LR of the first cycle.
        min_lr: Trough LR of the first cycle.
        cycle_steps: Length of the first post-warmup cycle.
        stable_ratio: Fraction of each cycle spent at the peak LR.
            Must satisfy 0 <= stable_ratio < 1.
        peak_decay: Multiplicative decay applied to cycle peaks after restart.
        cycle_mult: Geometric multiplier for cycle lengths.
        decay_floor: If True, decay min_lr with the same peak_decay.
            If False, keep min_lr fixed.
        cycle_boundary:
            "peak": cycle boundaries are restart peaks (classic SGDR behavior).
            "trough": cycle boundaries land exactly on the trough, useful when
            checkpoints are saved at exact cycle boundaries.
        last_epoch: Previous scheduler step.
    """

    def __init__(
            self,
            optimizer: torch.optim.Optimizer,
            warmup_steps: Union[int, float] = 2500,
            max_lr: float = 4e-4,
            min_lr: float = 5e-5,
            cycle_steps: Union[int, float] = 15000,
            stable_ratio: float = 0.9,
            peak_decay: float = 0.98,
            cycle_mult: float = 1.0,
            decay_floor: bool = True,
            cycle_boundary: str = "trough",
            last_epoch: int = -1,
    ):
        if warmup_steps < 0:
            raise ValueError(f"warmup_steps must be >= 0, got {warmup_steps}.")
        if cycle_steps <= 0:
            raise ValueError(f"cycle_steps must be > 0, got {cycle_steps}.")
        if max_lr < min_lr:
            raise ValueError(f"max_lr ({max_lr}) must be >= min_lr ({min_lr}).")
        if not 0.0 <= stable_ratio < 1.0:
            raise ValueError(
                f"stable_ratio must satisfy 0 <= stable_ratio < 1, got {stable_ratio}."
            )
        if not 0.0 < peak_decay <= 1.0:
            raise ValueError(f"peak_decay must satisfy 0 < peak_decay <= 1, got {peak_decay}.")
        if cycle_mult < 1.0:
            raise ValueError(f"cycle_mult must be >= 1.0, got {cycle_mult}.")
        if cycle_boundary not in ("peak", "trough"):
            raise ValueError(
                f"cycle_boundary must be 'peak' or 'trough', got {cycle_boundary!r}."
            )

        self.warmup_steps = warmup_steps
        self.max_lr = max_lr
        self.min_lr = min_lr
        self.cycle_steps = cycle_steps
        self.stable_ratio = stable_ratio
        self.peak_decay = peak_decay
        self.cycle_mult = cycle_mult
        self.decay_floor = decay_floor
        self.cycle_boundary = cycle_boundary
        super().__init__(optimizer, last_epoch)

    def _compute_lr(self, step_num: int) -> float:
        step_num = max(step_num, 0)

        # Phase 1: linear warmup.
        if step_num < self.warmup_steps:
            if self.warmup_steps == 0:
                return self.max_lr
            return step_num * (self.max_lr / self.warmup_steps)

        # Phase 2: cyclic WSD:
        # plateau at the current peak, then a short cosine cooldown.
        t = step_num - self.warmup_steps
        cycle, T_cur, T_i = self._cycle_position(t)

        decay = self.peak_decay ** cycle
        lo = self.min_lr * decay if self.decay_floor else self.min_lr
        hi = max(self.max_lr * decay, lo)

        stable_steps = self.stable_ratio * T_i

        # Plateau. Using <= keeps the plateau/cooldown join exactly at hi.
        if T_cur <= stable_steps:
            return float(hi)

        cooldown_steps = T_i - stable_steps
        progress = (T_cur - stable_steps) / cooldown_steps
        progress = float(np.clip(progress, 0.0, 1.0))

        return float(
            lo + 0.5 * (hi - lo) * (1.0 + np.cos(np.pi * progress))
        )

    def _cycle_position(self, t):
        """Return ``(cycle_index, T_cur, T_i)`` for post-warmup step ``t``.

        ``cycle_boundary="peak"``:
            The exact cycle boundary belongs to the next cycle and is its peak.
            The previous step gets very close to the trough but does not land
            exactly on it.

        ``cycle_boundary="trough"``:
            The exact cycle boundary belongs to the current cycle and lands
            exactly on its trough. The following step restarts to the next peak.
        """
        trough = self.cycle_boundary == "trough" and t > 0

        if self.cycle_mult == 1.0:
            if trough:
                cycle = int((t - 1) // self.cycle_steps)
                T_cur = t - cycle * self.cycle_steps
            else:
                cycle = int(t // self.cycle_steps)
                T_cur = t % self.cycle_steps
            return cycle, T_cur, self.cycle_steps

        log_pos = (
            np.log(t * (self.cycle_mult - 1) / self.cycle_steps + 1)
            / np.log(self.cycle_mult)
        )
        cycle = int(np.ceil(log_pos)) - 1 if trough else int(log_pos)
        cycle_start = (
            self.cycle_steps
            * (self.cycle_mult ** cycle - 1)
            / (self.cycle_mult - 1)
        )
        T_i = self.cycle_steps * self.cycle_mult ** cycle
        return cycle, t - cycle_start, T_i

    def get_lr(self):
        lr = self._compute_lr(self.last_epoch)
        return [lr for _ in self.base_lrs]

    def set_step(self, step: int):
        self.last_epoch = step
