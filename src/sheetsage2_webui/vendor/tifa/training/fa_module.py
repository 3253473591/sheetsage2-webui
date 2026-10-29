import pathlib

import matplotlib.pyplot as plt
import torch
from lightning.pytorch.loggers import TensorBoardLogger
from torch import Tensor, nn
from torch.utils.data import DataLoader

from inference.backend import ForcedAlignmentInferenceModel, SpectrogramContext
from lib.config.schema import RootConfig, LossConfig, AugmentationConfig
from lib.path_traversal import materialize_paths
from lib.plot import alignment_to_figure, emission_to_figure, topk_bar_figure
from modules.decoding import decode_alignment_flat
from modules.forced_alignment import ForcedAlignmentModel
from modules.functional import cross_cosine_similarity
from modules.losses import (
    FrameAlignmentLoss, SpanContrastiveLoss,
    TokenIdentityLoss, FrameIdentityLoss,
)
from modules.metrics import (
    BoundaryErrorRate,
    BoundaryMAE,
    Confidence,
    Determinacy,
    Monotonicity,
    PairConjunctionMAE,
    OverlapRatioCollection,
    PhonemeErrorRate,
)
from training.data import (
    BaseDataset,
    PhonemeTimingDataset,
    TextOnlyDataset,
    DynamicBatchSampler,
    ZippedDataLoader,
)
from training.pl_module_base import (
    BaseLightningModule,
    LossValue,
)
from training.pseudo_label import (
    PseudoLabelBatch,
    build_pseudo_labels,
    concat_pseudo_label_groups,
)

# Loss names shared between register_losses_and_metrics and forward_model.
_FRAME_ALIGNMENT = "frame_alignment_loss"
_SPAN_CONTRASTIVE = "span_contrastive_loss"
_TOKEN_IDENTITY = "token_identity_loss"
_FRAME_IDENTITY = "frame_identity_loss"
_AUX_FRAME_ALIGNMENT = "aux_frame_alignment_loss"
_AUX_SPAN_CONTRASTIVE = "aux_span_contrastive_loss"
_AUX_FRAME_IDENTITY = "aux_frame_identity_loss"

# Metric name bases shared between _register_fa_metrics and plot_validation_metrics.
_BER_ONSET = "BER_onset"
_BER_OFFSET = "BER_offset"
_B_MAE_ONSET = "B-MAE_onset"
_B_MAE_OFFSET = "B-MAE_offset"
_OVERLAP = "Overlap"
_CONJ_MAE = "Conj-MAE"
_CONFIDENCE = "Confidence"
_DETERMINACY = "Determinacy"
_MONOTONICITY = "Monotonicity"
_PHONEME_ERROR_RATE = "PER"


def _split_batch_budget(total: int, aux_ratio: float, name: str) -> tuple[int, int]:
    """Split a fixed total budget using an aux-to-main ratio."""
    aux = round(total * aux_ratio / (1.0 + aux_ratio))
    main = total - aux
    if main <= 0 or aux <= 0:
        raise ValueError(
            f"training.dataloader.{name}={total} cannot be split with "
            f"aux_ratio={aux_ratio} (aux:main); both main and aux budgets "
            "must be positive."
        )
    return main, aux


def _convert_aux_sampler_budget(
    group_batch_size: int,
    group_batch_frames: int,
    max_concat_size: int | None,
) -> tuple[int, int]:
    """Convert student concat-group budgets to raw teacher fragment budgets.

    Item capacity expands because the sampler now sees fragments. The padded
    frame budget already measures the teacher tensor footprint, so it is kept.
    """
    concat_capacity = max_concat_size if max_concat_size is not None else 1
    return group_batch_size * concat_capacity, group_batch_frames


def _accumulation_group_count(
    dataset: BaseDataset,
    sampler: DynamicBatchSampler,
    batch_idx: int,
    accumulate_grad_batches: int,
    key: str,
) -> float:
    """Sum dataset metadata over the sampler's current accumulation group."""
    sampler.form_batches()
    group_start = (batch_idx // accumulate_grad_batches) * accumulate_grad_batches
    batches = sampler.batches[group_start : group_start + accumulate_grad_batches]
    if len(batches) != accumulate_grad_batches:
        raise RuntimeError("Sampler did not provide a complete gradient accumulation group.")
    return float(sum(dataset.get_metadata(key, index) for batch in batches for index in batch))


class ForcedAlignmentModule(BaseLightningModule):

    def post_init(self) -> None:
        self._validate_semisupervised_config()

    def _validate_semisupervised_config(self) -> None:
        cfg = self.training_config.semisupervised
        if not cfg.enabled:
            return
        if self.aux_data_dir is None:
            raise ValueError("training.semisupervised.enabled requires binarizer.text_only_data_dir.")
        if not self.training_config.weight_averaging.ema_enabled:
            raise ValueError("training.semisupervised.enabled requires training.weight_averaging.ema_enabled.")
        dl_cfg = self.training_config.dataloader
        _split_batch_budget(
            dl_cfg.max_batch_size,
            dl_cfg.aux_ratio,
            "max_batch_size",
        )
        _split_batch_budget(
            dl_cfg.max_batch_frames,
            dl_cfg.aux_ratio,
            "max_batch_frames",
        )

    @classmethod
    def resolve_data_dirs(cls, config: RootConfig) -> tuple[pathlib.Path, pathlib.Path | None]:
        return (
            config.binarizer.phoneme_timing_data_dir_resolved,
            config.binarizer.text_only_data_dir_resolved,
        )

    def build_model(self) -> nn.Module:
        return ForcedAlignmentModel(self.model_config)

    def register_losses_and_metrics(self) -> None:
        loss_cfg: LossConfig = self.training_config.loss

        self.register_loss(_FRAME_ALIGNMENT, FrameAlignmentLoss(
            temperature=loss_cfg.frame_alignment.temperature,
        ), weight=loss_cfg.frame_alignment.weight)
        self.register_loss(_SPAN_CONTRASTIVE, SpanContrastiveLoss(
            temperature=loss_cfg.span_contrastive.temperature,
            bidirectional=loss_cfg.span_contrastive.bidirectional,
        ), weight=loss_cfg.span_contrastive.weight)
        self.register_loss(
            _TOKEN_IDENTITY, TokenIdentityLoss(),
            weight=loss_cfg.token_identity.weight,
        )
        self.register_loss(
            _FRAME_IDENTITY, FrameIdentityLoss(),
            weight=loss_cfg.frame_identity.weight,
        )

        semi_cfg = self.training_config.semisupervised
        if semi_cfg.enabled:
            self.register_loss(
                _AUX_FRAME_ALIGNMENT,
                FrameAlignmentLoss(
                    temperature=loss_cfg.frame_alignment.temperature,
                ),
                weight=semi_cfg.aux_loss_weight,
                validation=False,
            )
            self.register_loss(
                _AUX_SPAN_CONTRASTIVE,
                SpanContrastiveLoss(
                    temperature=loss_cfg.span_contrastive.temperature,
                    bidirectional=loss_cfg.span_contrastive.bidirectional,
                ),
                weight=semi_cfg.aux_loss_weight,
                validation=False,
            )
            self.register_loss(
                _AUX_FRAME_IDENTITY,
                FrameIdentityLoss(),
                weight=(semi_cfg.aux_loss_weight * semi_cfg.pseudo_frame_identity_weight),
                validation=False,
            )

        self._register_fa_metrics()
        if self.use_parallel_dirty_metrics:
            self._register_fa_metrics(postfix="_dirty")

    def _register_fa_metrics(self, postfix: str = "") -> None:
        V = self.vocab.vocab_size
        T = self.training_config.validation.metrics_ber_tolerance
        K = self.training_config.validation.metrics_k_values
        KC = self.training_config.validation.metrics_conjunction_k_values

        # Boundary Error Rate -- onset
        self.register_metric(
            f"{_BER_ONSET}{postfix}",
            BoundaryErrorRate(tolerance=T, mode="onset"),
        )
        for k in K:
            self.register_metric(
                f"{_BER_ONSET}@{k}{postfix}",
                BoundaryErrorRate(tolerance=T, mode="onset", vocab_size=V, k=k),
            )

        # Boundary Error Rate -- offset
        self.register_metric(
            f"{_BER_OFFSET}{postfix}",
            BoundaryErrorRate(tolerance=T, mode="offset"),
        )
        for k in K:
            self.register_metric(
                f"{_BER_OFFSET}@{k}{postfix}",
                BoundaryErrorRate(tolerance=T, mode="offset", vocab_size=V, k=k),
            )

        # Boundary MAE -- onset
        self.register_metric(
            f"{_B_MAE_ONSET}{postfix}",
            BoundaryMAE(mode="onset"),
        )
        for k in K:
            self.register_metric(
                f"{_B_MAE_ONSET}@{k}{postfix}",
                BoundaryMAE(mode="onset", vocab_size=V, k=k),
            )

        # Boundary MAE -- offset
        self.register_metric(
            f"{_B_MAE_OFFSET}{postfix}",
            BoundaryMAE(mode="offset"),
        )
        for k in K:
            self.register_metric(
                f"{_B_MAE_OFFSET}@{k}{postfix}",
                BoundaryMAE(mode="offset", vocab_size=V, k=k),
            )

        # Pair Conjunction MAE
        for k in KC:
            self.register_metric(
                f"{_CONJ_MAE}@{k}{postfix}",
                PairConjunctionMAE(vocab_size=V, k=k),
            )

        # Overlap Ratio Collection
        self.register_metric(
            f"{_OVERLAP}{postfix}",
            OverlapRatioCollection(template=f"{_OVERLAP}_{{}}{postfix}"),
        )
        for k in K:
            self.register_metric(
                f"{_OVERLAP}@{k}{postfix}",
                OverlapRatioCollection(
                    template=f"{_OVERLAP}_{{}}@{k}{postfix}", vocab_size=V, k=k,
                ),
            )

        # Phoneme Error Rate
        self.register_metric(
            f"{_PHONEME_ERROR_RATE}{postfix}",
            PhonemeErrorRate(),
        )

        # Confidence
        self.register_metric(
            f"{_CONFIDENCE}{postfix}",
            Confidence(),
        )
        # Path Determinacy
        det_power = self.training_config.validation.metrics_determinacy_power
        det_width = self.training_config.validation.metrics_determinacy_width
        self.register_metric(
            f"{_DETERMINACY}{postfix}",
            Determinacy(power=det_power, width=det_width),
        )
        # Monotonicity
        mono_power = self.training_config.validation.metrics_monotonicity_power
        mono_width = self.training_config.validation.metrics_monotonicity_width
        self.register_metric(
            f"{_MONOTONICITY}{postfix}",
            Monotonicity(power=mono_power, width=mono_width),
        )

    def _update_fa_metrics(
        self, pred_spans: Tensor, target_spans: Tensor, tokens: Tensor, postfix: str = ""
    ) -> None:
        for name, metric in self.metrics.items():
            if not name.endswith(postfix):
                continue
            if not isinstance(metric, (BoundaryErrorRate, BoundaryMAE, PairConjunctionMAE, OverlapRatioCollection)):
                continue
            metric.update(pred_spans, target_spans, tokens)

    def build_train_dataset(self) -> BaseDataset:
        dl_cfg = self.training_config.dataloader
        return PhonemeTimingDataset(
            self.data_dir,
            "train",
            augmentation_config=self.training_config.augmentation,
            augmentation_seed=self.training_config.trainer.seed,
            max_concat_size=dl_cfg.max_concat_size,
            max_concat_frames=dl_cfg.max_concat_frames,
        )

    def build_valid_dataset(self) -> BaseDataset:
        dl_cfg = self.training_config.dataloader
        aug_cfg = self.training_config.augmentation.keep(
            *AugmentationConfig.destructive_augmentation_names(),
            "token_masking", "sequence_edit",
        )
        return PhonemeTimingDataset(
            self.data_dir, "valid",
            augmentation_config=aug_cfg,
            augmentation_deterministic=True,
            augmentation_return_dirty=True,
            augmentation_return_mutated=True,
            max_concat_size=dl_cfg.max_concat_size,
            max_concat_frames=dl_cfg.max_concat_frames,
            concat_deterministic=True,
        )

    def build_aux_dataset(self) -> BaseDataset | None:
        if not self.training_config.semisupervised.enabled:
            return None
        aug_cfg = self.training_config.augmentation.drop(
            "time_stretching",
            "sequence_edit",
            "token_masking",
        )
        return TextOnlyDataset(
            self.aux_data_dir,
            "aux",
            augmentation_config=aug_cfg,
            augmentation_return_dirty=True,
            augmentation_seed=(
                None if self.training_config.trainer.seed is None else self.training_config.trainer.seed + 1
            ),
        )

    def train_dataloader(self):
        main_dl = super().train_dataloader()
        if self.aux_dataset is None:
            # Always use ZippedDataLoader for consistent sample format in forward_model
            return ZippedDataLoader(main_dl)

        dl_cfg = self.training_config.dataloader
        main_batch_size, aux_batch_size = _split_batch_budget(
            dl_cfg.max_batch_size,
            dl_cfg.aux_ratio,
            "max_batch_size",
        )
        main_batch_frames, aux_batch_frames = _split_batch_budget(
            dl_cfg.max_batch_frames,
            dl_cfg.aux_ratio,
            "max_batch_frames",
        )
        aux_raw_batch_size, aux_raw_batch_frames = _convert_aux_sampler_budget(
            aux_batch_size,
            aux_batch_frames,
            dl_cfg.max_concat_size,
        )
        self._main_active_batch_size = main_batch_size
        self._main_active_batch_frames = main_batch_frames
        baseline_num_batches = len(main_dl)
        self.aux_sampler = DynamicBatchSampler(
            self.aux_dataset,
            max_batch_size=aux_raw_batch_size,
            max_batch_frames=aux_raw_batch_frames,
            sort_by_len=True,
            frame_count_grid=dl_cfg.frame_count_grid,
            batch_count_multiple_of=self.training_config.trainer.accumulate_grad_batches,
            reassign_batches=True,
            shuffle_batches=True,
            seed=42,
        )
        aux_dl = DataLoader(
            self.aux_dataset,
            collate_fn=self.aux_dataset.collate,
            batch_sampler=self.aux_sampler,
            num_workers=dl_cfg.num_workers,
            prefetch_factor=dl_cfg.prefetch_factor if dl_cfg.num_workers > 0 else None,
            pin_memory=True,
            persistent_workers=dl_cfg.num_workers > 0,
        )
        return ZippedDataLoader(
            main_dl,
            aux_dl,
            self.aux_sampler,
            aux_warmup_epochs=dl_cfg.aux_warmup_epochs,
            num_batches=baseline_num_batches,
        )

    def val_dataloader(self):
        return ZippedDataLoader(super().val_dataloader())

    def validation_step(self, sample, batch_index):
        sample["indices"] = sample["main"]["indices"]
        super().validation_step(sample, batch_index)

    def on_train_start(self):
        super().on_train_start()
        # Lightning skips on_train_epoch_start when resuming mid-epoch.
        self._sync_train_sampler_state()

    def on_train_epoch_start(self):
        self._sync_train_sampler_state()

    def _sync_train_sampler_state(self) -> None:
        if self.train_sampler is None:
            return

        epoch = self.current_epoch
        dl_cfg = self.training_config.dataloader
        batch_size = dl_cfg.max_batch_size
        batch_frames = dl_cfg.max_batch_frames
        if self.aux_sampler is not None and not self._is_aux_warmup():
            batch_size = self._main_active_batch_size
            batch_frames = self._main_active_batch_frames

        if (
            self.train_sampler.epoch != epoch
            or self.train_sampler.max_batch_size != batch_size
            or self.train_sampler.max_batch_frames != batch_frames
        ):
            self.train_sampler.max_batch_size = batch_size
            self.train_sampler.max_batch_frames = batch_frames
            self.train_sampler.set_epoch(epoch)
            self.train_sampler.formed = None
        if self.aux_sampler is not None and self.aux_sampler.epoch != epoch:
            self.aux_sampler.set_epoch(epoch)
            self.aux_sampler.formed = None

    def _is_aux_warmup(self) -> bool:
        """Whether aux dataset exists but hasn't started contributing yet."""
        return (
            self.aux_data_dir is not None
            and self.current_epoch < self.training_config.dataloader.aux_warmup_epochs
        )

    def _group_count(self, batch_idx: int | None, key: str) -> float:
        if batch_idx is None:
            return 0  # validation: weight falls back to 1.0 in training_step
        return _accumulation_group_count(
            self.train_dataset,
            self.train_sampler,
            batch_idx,
            self.training_config.trainer.accumulate_grad_batches,
            key,
        )

    def _aux_group_count(self, batch_idx: int, key: str) -> float:
        return _accumulation_group_count(
            self.aux_dataset,
            self.aux_sampler,
            batch_idx,
            self.training_config.trainer.accumulate_grad_batches,
            key,
        )

    def _zero_aux_losses(
        self,
        reference: Tensor,
        group_frames: float = 0.0,
        group_tokens: float = 0.0,
    ) -> dict[str, LossValue]:
        zero = reference.new_zeros(())
        return {
            _AUX_FRAME_ALIGNMENT: LossValue(zero, 0, group_frames),
            _AUX_SPAN_CONTRASTIVE: LossValue(zero, 0, group_tokens),
            _AUX_FRAME_IDENTITY: LossValue(zero, 0, group_frames),
        }

    def _generate_aux_pseudo_labels(self, aux_sample: dict[str, Tensor]) -> PseudoLabelBatch:
        if not self.use_ema:
            raise RuntimeError("Semi-supervised training requires an EMA teacher.")

        spectrogram = aux_sample["spectrogram"]
        frame_lengths = aux_sample["T"]
        max_T = spectrogram.shape[1]
        t_mask = torch.arange(max_T, device=spectrogram.device).unsqueeze(0) < frame_lengths.unsqueeze(1)
        spec = SpectrogramContext(features=spectrogram.float(), mask=t_mask)
        # Keep this borrowed backend local to avoid registering the model twice.
        backend = ForcedAlignmentInferenceModel(
            self.model_config,
            model=self.model,
            spec_fn=self.aux_dataset.mel_spectrogram,
        )
        was_training = self.model.training
        applied = False
        self.model.eval()
        try:
            self.ema.apply()
            applied = True
            with torch.no_grad(), torch.autocast(
                device_type=spectrogram.device.type,
                enabled=False,
            ):
                scored = backend.score(
                    spec, paths=aux_sample["paths"], words=aux_sample["words"],
                    candidates=aux_sample["candidates"],
                )
                tokens, _, _ = materialize_paths(
                    aux_sample["paths"], aux_sample["words"], aux_sample["groups"], scored.choices,
                )
                n_mask = tokens != 0
                token_lengths = n_mask.sum(dim=-1)
                if int(token_lengths.max().item()) == 0:
                    agreement = spectrogram.new_zeros((spectrogram.shape[0],))
                    similarity = spectrogram.new_zeros(
                        (spectrogram.shape[0], max_T, tokens.shape[1]),
                    )
                    spans = torch.zeros(
                        spectrogram.shape[0],
                        tokens.shape[1],
                        2,
                        dtype=torch.long,
                        device=spectrogram.device,
                    )
                else:
                    aligned = backend.align(
                        spec, tokens=tokens, unit="frame",
                        skip_penalty=self.training_config.semisupervised.skip_penalty,
                    )
                    agreement = aligned.agreement
                    similarity = aligned.similarity
                    # The decoder trims to real tokens; candidate grids retain padding.
                    spans = nn.functional.pad(
                        aligned.spans, (0, 0, 0, tokens.shape[1] - aligned.spans.shape[1]),
                    )
        finally:
            if applied:
                self.ema.restore()
            self.model.train(was_training)

        validation_cfg = self.training_config.validation
        semi_cfg = self.training_config.semisupervised
        return build_pseudo_labels(
            tokens=tokens,
            spans=spans,
            similarity=similarity,
            agreement=agreement,
            t_mask=t_mask,
            n_mask=n_mask,
            vocab_size=self.vocab.vocab_size,
            min_agreement=semi_cfg.min_agreement,
            min_confidence=semi_cfg.min_confidence,
            min_determinacy=semi_cfg.min_determinacy,
            min_monotonicity=semi_cfg.min_monotonicity,
            determinacy_power=validation_cfg.metrics_determinacy_power,
            determinacy_width=validation_cfg.metrics_determinacy_width,
            monotonicity_power=validation_cfg.metrics_monotonicity_power,
            monotonicity_width=validation_cfg.metrics_monotonicity_width,
        )

    def _forward_aux(
        self,
        aux_sample: dict[str, Tensor],
        batch_idx: int,
    ) -> dict[str, LossValue]:
        reference = aux_sample["spectrogram_dirty"]
        group_frames = self._aux_group_count(batch_idx, "lengths")
        group_tokens = self._aux_group_count(batch_idx, "paths")
        if self._is_aux_warmup():
            return self._zero_aux_losses(reference, group_frames, group_tokens)

        pseudo = self._generate_aux_pseudo_labels(aux_sample)
        if (
            self.global_step > self.logger_step
            and self.global_step % self.training_config.trainer.log_every_n_steps == 0
        ):
            self.logger.log_metrics(
                {"training/aux_acceptance_rate": pseudo.accepted.float().mean()},
                step=self.global_step,
            )
        dl_cfg = self.training_config.dataloader
        student = concat_pseudo_label_groups(
            aux_sample,
            pseudo,
            max_concat_size=dl_cfg.max_concat_size,
            max_concat_frames=dl_cfg.max_concat_frames,
        )
        if student is None:
            return self._zero_aux_losses(
                reference,
                group_frames,
                group_tokens,
            )

        frame_features, frame_logits, token_features, _ = self.model(
            student.spectrogram,
            student.tokens,
            student.t_mask,
            student.n_mask,
        )
        frame_alignment = self.losses[_AUX_FRAME_ALIGNMENT](
            frame_features,
            token_features,
            student.regions,
            student.t_mask,
            student.n_mask,
        )
        span_contrastive = self.losses[_AUX_SPAN_CONTRASTIVE](
            frame_features,
            token_features,
            student.spans,
            student.t_mask,
            student.n_mask,
        )
        frame_identity = self.losses[_AUX_FRAME_IDENTITY](
            frame_logits,
            student.frame_targets,
            student.t_mask,
        )

        frame_count = int(student.t_mask.sum().item())
        token_count = int(student.n_mask.sum().item())
        return {
            _AUX_FRAME_ALIGNMENT: LossValue(
                frame_alignment,
                frame_count,
                group_frames,
            ),
            _AUX_SPAN_CONTRASTIVE: LossValue(
                span_contrastive,
                token_count,
                group_tokens,
            ),
            _AUX_FRAME_IDENTITY: LossValue(
                frame_identity,
                frame_count,
                group_frames,
            ),
        }

    def forward_model(self, sample: dict[str, Tensor], infer: bool, batch_idx=None):
        aux_losses = {}
        if not infer and batch_idx is not None and self.training_config.semisupervised.enabled:
            if self._is_aux_warmup():
                aux_losses = self._zero_aux_losses(
                    sample["main"]["spectrogram"],
                )
            else:
                if "aux" not in sample:
                    raise RuntimeError("Semi-supervised training batch is missing aux data.")
                aux_losses = self._forward_aux(sample["aux"], batch_idx)

        main_sample = sample["main"]
        spectrogram = main_sample["spectrogram"]
        tokens = main_sample["tokens"]
        regions = main_sample["regions"]
        T = main_sample["T"]
        N = main_sample["N"]

        device = spectrogram.device
        B, max_T = spectrogram.shape[:2]
        max_N = tokens.shape[1]

        t_mask = torch.arange(max_T, device=device).unsqueeze(0) < T.unsqueeze(1)
        n_mask = torch.arange(max_N, device=device).unsqueeze(0) < N.unsqueeze(1)

        is_mlm = main_sample["is_mlm"]  # [B] bool -- MLM-masked samples excluded from FA/SC
        n_mask_fa = n_mask.clone()
        n_mask_fa[is_mlm] = False
        t_mask_fa = t_mask.clone()
        t_mask_fa[is_mlm] = False

        frame_features, frame_logits, token_features, token_logits = self.model(spectrogram, tokens, t_mask, n_mask)

        if infer:
            similarity = cross_cosine_similarity(frame_features, token_features)  # [-1, 1]
            pred_spans = decode_alignment_flat(
                similarity, T, N, skip_penalty=self.training_config.validation.skip_penalty,
            )
            target_spans = main_sample["spans"]
            self._update_fa_metrics(pred_spans, target_spans, tokens)
            self.metrics[_CONFIDENCE].update(
                pred_spans, similarity, t_mask_fa, n_mask_fa,
            )
            self.metrics[_DETERMINACY].update(
                pred_spans, similarity, t_mask_fa, n_mask_fa,
            )
            self.metrics[_MONOTONICITY].update(
                pred_spans, similarity, t_mask_fa, n_mask_fa,
            )

            if self.use_parallel_dirty_metrics and "spectrogram_dirty" in main_sample:
                xf_d, _, tf_d, _ = self.model(
                    main_sample["spectrogram_dirty"], tokens, t_mask, n_mask,
                )
                sim_dirty = cross_cosine_similarity(xf_d, tf_d)
                pred_spans_dirty = decode_alignment_flat(
                    sim_dirty, T, N, skip_penalty=self.training_config.validation.skip_penalty,
                )
                self._update_fa_metrics(
                    pred_spans_dirty, target_spans, tokens, postfix="_dirty",
                )
                self.metrics[f"{_CONFIDENCE}_dirty"].update(
                    pred_spans_dirty, sim_dirty, t_mask_fa, n_mask_fa,
                )
                self.metrics[f"{_DETERMINACY}_dirty"].update(
                    pred_spans_dirty, sim_dirty, t_mask_fa, n_mask_fa,
                )
                self.metrics[f"{_MONOTONICITY}_dirty"].update(
                    pred_spans_dirty, sim_dirty, t_mask_fa, n_mask_fa,
                )

            if "tokens_mutated" in main_sample:
                tokens_mutated = main_sample["tokens_mutated"]
                token_targets_mutated = main_sample["token_targets_mutated"]
                n_mask_mutated = tokens_mutated != 0

                _, _, _, token_logits_mutated = self.model(
                    spectrogram, tokens_mutated, t_mask, n_mask_mutated,
                )
                self.metrics[_PHONEME_ERROR_RATE].update(
                    token_logits_mutated, token_targets_mutated, n_mask_mutated,
                )

                if self.use_parallel_dirty_metrics and "spectrogram_dirty" in main_sample:
                    _, _, _, token_logits_both = self.model(
                        main_sample["spectrogram_dirty"], tokens_mutated, t_mask, n_mask_mutated,
                    )
                    self.metrics[f"{_PHONEME_ERROR_RATE}_dirty"].update(
                        token_logits_both, token_targets_mutated, n_mask_mutated,
                    )

            return {
                "spans": pred_spans,
                "similarity": similarity,
            }

        batch_frames = T.sum().item()
        batch_tokens = N.sum().item()
        group_frames = self._group_count(batch_idx, "lengths")
        group_tokens = self._group_count(batch_idx, "tokens")

        frame_alignment_loss = LossValue(
            mean=self.losses[_FRAME_ALIGNMENT](frame_features, token_features, regions, t_mask_fa, n_mask_fa),
            batch_count=batch_frames, group_count=group_frames,
        )
        span_contrastive_loss = LossValue(
            mean=self.losses[_SPAN_CONTRASTIVE](frame_features, token_features, main_sample["spans"], t_mask_fa, n_mask_fa),
            batch_count=batch_tokens, group_count=group_tokens,
        )
        token_identity_loss = LossValue(
            mean=self.losses[_TOKEN_IDENTITY](
                token_logits, main_sample["token_targets"], n_mask,
            ),
            batch_count=batch_tokens, group_count=group_tokens,
        )
        frame_identity_loss = LossValue(
            mean=self.losses[_FRAME_IDENTITY](
                frame_logits, main_sample["frame_targets"], t_mask,
            ),
            batch_count=batch_frames, group_count=group_frames,
        )
        losses = {
            _FRAME_ALIGNMENT: frame_alignment_loss,
            _SPAN_CONTRASTIVE: span_contrastive_loss,
            _TOKEN_IDENTITY: token_identity_loss,
            _FRAME_IDENTITY: frame_identity_loss,
        }
        losses.update(aux_losses)

        return losses

    def plot_validation_results(self, sample, outputs):
        main = sample["main"]
        indices = main["indices"]

        tokens = main["tokens"]
        gt_spans = main["spans"]
        pred_spans = outputs["spans"]
        sim_all = outputs["similarity"]
        spectrograms = main["spectrogram"]
        regions = main["regions"]
        T_all = main["T"]
        N_all = main["N"]

        for i in range(indices.shape[0]):
            data_idx = int(indices[i].item())
            if data_idx not in self.plot_indices:
                continue

            T_i = int(T_all[i].item())
            N_i = int(N_all[i].item())
            if T_i == 0 or N_i == 0:
                continue

            token_ids = tokens[i, :N_i].tolist()
            token_labels = [
                self.vocab.decode(int(tid), stringfy=True) or str(tid)
                for tid in token_ids
            ]

            item_path = self.valid_dataset.get_metadata("item_paths", data_idx)
            logger: TensorBoardLogger = self.logger

            sim = sim_all[i, :T_i, :N_i]  # [T_i, N_i]
            fig_sim = emission_to_figure(
                sim.float().detach().cpu().numpy().T,  # [N_i, T_i]
                regions=regions[i, :T_i].detach().cpu().numpy(),
                title=item_path,
                token_labels=token_labels,
            )
            logger.experiment.add_figure(
                f"similarity/{data_idx}", fig_sim, global_step=self.global_step,
            )
            plt.close(fig_sim)

            spec = spectrograms[i, :T_i].detach().cpu().numpy()
            ps = pred_spans[i, :N_i].detach().cpu().numpy()
            gs = gt_spans[i, :N_i].detach().cpu().numpy()
            fig_align = alignment_to_figure(
                spec,
                token_labels=token_labels,
                pred_spans=ps,
                gt_spans=gs,
                title=item_path,
            )
            logger.experiment.add_figure(
                f"alignment/{data_idx}", fig_align, global_step=self.global_step,
            )
            plt.close(fig_align)

    def plot_validation_metrics(self) -> None:
        K = self.training_config.validation.metrics_k_values
        KC = self.training_config.validation.metrics_conjunction_k_values
        postfixes = [""]
        if self.use_parallel_dirty_metrics:
            postfixes.append("_dirty")

        if K:
            k = max(K)
            for pf in postfixes:
                self._plot_boundary_topk(f"{_BER_ONSET}@{k}{pf}")
                self._plot_boundary_topk(f"{_BER_OFFSET}@{k}{pf}")
                self._plot_boundary_topk(f"{_B_MAE_ONSET}@{k}{pf}")
                self._plot_boundary_topk(f"{_B_MAE_OFFSET}@{k}{pf}")
                self._plot_overlap_topk(f"{_OVERLAP}@{k}{pf}")
        if KC:
            k = max(KC)
            for pf in postfixes:
                self._plot_conjunction_topk(f"{_CONJ_MAE}@{k}{pf}")

    def _plot_boundary_topk(self, name: str) -> None:
        data = self.metrics[name].compute_top_k()
        if not data:
            return
        labels = [self.vocab.decode(tid, stringfy=True) or str(tid) for tid in data]
        values = [v.item() for v in data.values()]
        fig = topk_bar_figure(labels, values, name)
        logger: TensorBoardLogger = self.logger
        logger.experiment.add_figure(f"topk/{name}", fig, global_step=self.global_step)
        plt.close(fig)

    def _plot_conjunction_topk(self, name: str) -> None:
        data = self.metrics[name].compute_top_k()
        if not data:
            return
        labels = [
            f"{self.vocab.decode(i, stringfy=True) or str(i)} -> {self.vocab.decode(j, stringfy=True) or str(j)}"
            for (i, j) in data
        ]
        values = [v.item() for v in data.values()]
        fig = topk_bar_figure(labels, values, name)
        logger: TensorBoardLogger = self.logger
        logger.experiment.add_figure(f"topk/{name}", fig, global_step=self.global_step)
        plt.close(fig)

    def _plot_overlap_topk(self, name: str) -> None:
        data = self.metrics[name].compute_top_k()
        if not data:
            return
        logger: TensorBoardLogger = self.logger
        for sub_name, sub_data in data.items():
            labels = [self.vocab.decode(tid, stringfy=True) or str(tid) for tid in sub_data]
            values = [v.item() for v in sub_data.values()]
            fig = topk_bar_figure(labels, values, sub_name, reverse=False)
            logger.experiment.add_figure(
                f"topk/{sub_name}", fig, global_step=self.global_step,
            )
            plt.close(fig)
