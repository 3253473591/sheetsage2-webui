import pathlib

import matplotlib.pyplot as plt
import torch
from lightning.pytorch.loggers import TensorBoardLogger
from torch import nn, Tensor

from lib.config.schema import RootConfig, LossConfig
from lib.path_traversal import first_choices, materialize_paths, sample_paths_uniform
from lib.plot import alignment_to_figure, emission_to_figure, reconstruction_to_figure
from lib.vocabulary import SPACE_TOKEN
from modules.commons.common_layers import TemporalMask
from modules.decoding import decode_alignment_spaced
from modules.forced_alignment import ForcedAlignmentSSLModel
from modules.functional import interleave_spaces
from modules.losses import CTCLossWithoutBlank, HMMForwardLossWithEmissions, SpectrogramReconstructionLoss
from training.data import (
    BaseDataset,
    TextOnlyDataset,
)
from training.iterative_ranking import RankingModule, WordRewards, rank_rewards
from training.pl_module_base import BaseLightningModule, LossValue

# Loss names shared between register_losses_and_metrics and forward_model.
_CTC = "ctc_loss"
_HMM_FORWARD = "hmm_forward_loss"
_RECONSTRUCTION = "reconstruction_loss"


class ForcedAlignmentSSLModule(BaseLightningModule, RankingModule):

    def __init__(self, *args, **kwargs):
        self.spec_mask = None
        super().__init__(*args, **kwargs)
        self._best_paths: dict[int, dict[int, int]] | None = None

    @classmethod
    def resolve_data_dirs(cls, config: RootConfig) -> tuple[pathlib.Path, pathlib.Path | None]:
        return (
            config.binarizer.text_only_data_dir_resolved,
            None,
        )

    def build_model(self) -> nn.Module:
        return ForcedAlignmentSSLModel(self.model_config)

    def register_losses_and_metrics(self) -> None:
        loss_cfg: LossConfig = self.training_config.loss

        self.register_loss(
            _CTC, CTCLossWithoutBlank(),
            weight=loss_cfg.ctc.weight,
        )
        self.register_loss(
            _HMM_FORWARD, HMMForwardLossWithEmissions(
                mode=loss_cfg.hmm_forward.mode,
            ),
            weight=loss_cfg.hmm_forward.weight,
        )
        self.register_loss(
            _RECONSTRUCTION, SpectrogramReconstructionLoss(
                loss_type=loss_cfg.reconstruction.loss_type,
                unmasked_weight=loss_cfg.reconstruction.unmasked_weight,
            ),
            weight=loss_cfg.reconstruction.weight,
        )

    def post_init(self) -> None:
        self.spec_mask = TemporalMask(
            channels=self.model_config.in_dim,
            mask_p=0,
            seed=42
        )

    def build_train_dataset(self) -> BaseDataset:
        dl_cfg = self.training_config.dataloader
        return TextOnlyDataset(
            self.data_dir, "train",
            augmentation_config=self.training_config.augmentation,
            max_concat_size=dl_cfg.max_concat_size,
            max_concat_frames=dl_cfg.max_concat_frames,
        )

    def build_valid_dataset(self) -> BaseDataset:
        dl_cfg = self.training_config.dataloader
        return TextOnlyDataset(
            self.data_dir, "valid",
            max_concat_size=dl_cfg.max_concat_size,
            max_concat_frames=dl_cfg.max_concat_frames,
            concat_deterministic=True,
        )
        if self.use_parallel_dirty_metrics:
            aug_cfg = self.training_config.augmentation.keep_destructive()
            return TextOnlyDataset(
                self.data_dir, "valid",
                augmentation_config=aug_cfg,
                augmentation_deterministic=True,
                augmentation_return_dirty=True,
                max_concat_size=dl_cfg.max_concat_size,
                max_concat_frames=dl_cfg.max_concat_frames,
                concat_deterministic=True,
            )
        else:
            return TextOnlyDataset(
                self.data_dir, "valid",
                max_concat_size=dl_cfg.max_concat_size,
                max_concat_frames=dl_cfg.max_concat_frames,
                concat_deterministic=True,
            )

    def _group_count(self, batch_idx: int | None, key: str) -> int:
        if batch_idx is None:
            return 0
        batches = self.get_accumulation_group(batch_idx)
        return sum(
            self.train_dataset.get_metadata(key, idx)
            for batch in batches for idx in batch
        )

    def on_validation_epoch_start(self):
        super().on_validation_epoch_start()
        self.spec_mask.reset_random_generator()

    def set_best_paths(self, best_paths: dict[int, dict[int, int]] | None) -> None:
        self._best_paths = best_paths

    def training_step(self, sample, batch_index):
        total_loss = super().training_step(sample, batch_index)
        ranking_cfg = self.training_config.iterative_ranking
        if self._best_paths is not None and ranking_cfg.enabled:
            return {
                "loss": total_loss,
                "ranking_results": self._compute_ranking(sample),
            }
        return total_loss

    def forward_model(self, sample: dict[str, Tensor], infer: bool, batch_idx=None):
        spectrogram = sample["spectrogram"]  # [B, T, n_mels]
        paths = sample["paths"]  # [B, P, C], word ownership given by sample["words"]
        T = sample["T"]  # [B] int64
        f0 = sample["f0"]  # [B, T]

        device = spectrogram.device
        _, max_T, _ = spectrogram.shape

        # Select complete paths before interleaving space tokens.
        choices = first_choices(sample["candidates"])
        tokens_raw, _, _ = materialize_paths(paths, sample["words"], sample["groups"], choices)
        N = (tokens_raw != 0).sum(dim=1)
        tokens, n_mask = interleave_spaces(tokens_raw, space=SPACE_TOKEN, lengths=N)
        # tokens: [B, 2*N+1], n_mask: [B, 2*N+1] bool

        # Frame mask
        t_mask = torch.arange(max_T, device=device).unsqueeze(0) < T.unsqueeze(1)  # [B, T]

        token_lens = 2 * N + 1

        if infer:
            _, attn_logits, _ = self.model(
                spectrogram, tokens, t_mask, n_mask, reconstruct=False,
            )
            # Mean all heads across all CA layers -> [B, T, N_interleaved]
            all_flat = []
            for a in attn_logits:
                mid = a.dim() - 3
                if mid > 0:
                    all_flat.append(a.flatten(1, mid))
                else:
                    all_flat.append(a.unsqueeze(1))
            emission = torch.cat(all_flat, dim=1).mean(dim=1)  # [B, T, N_interleaved]
            attn_weights = emission.softmax(dim=-1)

            # Masked forward for reconstruction visualization
            masked_spec, _ = self.spec_mask(spectrogram, mask=t_mask)
            _, _, recon_spec = self.model(
                masked_spec, tokens, t_mask, n_mask, f0=f0, reconstruct=True,
            )

            pred_spans = decode_alignment_spaced(emission, T, token_lens)
            return {
                "spans": pred_spans,
                "tokens": tokens,
                "attn_weights": attn_weights,
                "masked_spec": masked_spec,
                "recon_spec": recon_spec,
            }

        # Apply temporal masking (self-supervised corruption)
        masked_spec, corrupted = self.spec_mask(spectrogram, mask=t_mask)

        # Model forward
        frame_logits, attn_logits, x_recon = self.model(
            masked_spec, tokens, t_mask, n_mask, f0=f0, reconstruct=True,
        )

        # Gradient accumulation counts (frame-based for all losses)
        batch_count = int(T.sum().item())
        group_count = self._group_count(batch_idx, "lengths")

        # CTC loss from frame logits
        ctc_loss_val = self.losses[_CTC](
            frame_logits, targets=tokens, frame_lens=T, token_lens=token_lens,
        )
        ctc_loss = LossValue(
            mean=ctc_loss_val, batch_count=batch_count, group_count=group_count,
        )

        # HMM loss from cross-attention logits
        hmm_loss_val = self.losses[_HMM_FORWARD](
            attn_logits, frame_lens=T, token_lens=token_lens,
        )
        hmm_loss = LossValue(
            mean=hmm_loss_val, batch_count=batch_count, group_count=group_count,
        )

        # Reconstruction loss against clean spectrogram
        recon_loss_val = self.losses[_RECONSTRUCTION](
            x_recon=x_recon, target=spectrogram, t_mask=t_mask,
            corrupted_mask=corrupted,
        )
        recon_loss = LossValue(
            mean=recon_loss_val, batch_count=batch_count, group_count=group_count,
        )

        return {
            _CTC: ctc_loss,
            _HMM_FORWARD: hmm_loss,
            _RECONSTRUCTION: recon_loss,
        }

    def _compute_ranking(self, sample):
        candidates = sample["candidates"]
        rank_size = self.training_config.iterative_ranking.rank_size
        choices = sample_paths_uniform(candidates, rank_size)
        with torch.no_grad():
            scores = self.score_paths(sample, choices)
            order = scores.argsort(dim=1, descending=True)
            picked = choices.gather(1, order).transpose(1, 2)
            rewards = rank_rewards(rank_size).to(candidates.device).view(1, 1, rank_size)
            totals = picked.new_zeros(*candidates.shape[:2], candidates.shape[-1] + 1).scatter_add(
                2, picked, rewards.expand_as(picked),
            )[..., 1:]
        # The ranker's persistent per-item records are a Python output boundary.
        counts = candidates.sum(dim=-1)
        results = []
        for b, w in (counts > 1).nonzero().tolist():
            count = int(counts[b, w])
            results.append(WordRewards(int(sample["indices"][b]), w, totals[b, w, :count]))
        return results

    def score_paths(self, item_data: dict, choices: Tensor) -> Tensor:
        """Return [B,K,W] complete-candidate scores. SSL scoring is a stub."""
        return torch.zeros_like(choices, dtype=torch.float32)

    def plot_validation_results(self, sample, outputs):
        indices = sample["indices"]
        spectrograms = sample["spectrogram"]
        T_all = sample["T"]
        N_all = sample["N"]
        tokens_all = outputs["tokens"]  # [B, 2*N+1] interleaved
        pred_spans_all = outputs["spans"]          # [B, 2*N+1, 2]
        attn_all = outputs["attn_weights"]         # [B, T, N_interleaved]

        for i in range(indices.shape[0]):
            data_idx = int(indices[i].item())
            if data_idx not in self.plot_indices:
                continue
            T_i = int(T_all[i].item())
            N_i = int(N_all[i].item())
            if T_i == 0 or N_i == 0:
                continue

            N_interleaved = 2 * N_i + 1

            # All token labels (interleaved: SP for space, decode for real)
            token_labels = []
            for j in range(N_interleaved):
                if j % 2 == 0:
                    token_labels.append("SP")
                else:
                    tid = int(tokens_all[i, j].item())
                    label = self.vocab.decode(tid, stringfy=True) or str(tid)
                    token_labels.append(label)

            # Real token labels (odd indices only)
            real_labels = token_labels[1::2]

            item_path = self.valid_dataset.get_metadata("item_paths", data_idx)
            logger: TensorBoardLogger = self.logger

            # Emission heatmap
            attn = attn_all[i, :T_i, :N_interleaved].float().detach().cpu().numpy()
            fig_em = emission_to_figure(
                attn.T,  # [N_interleaved, T_i]
                title=item_path,
                token_labels=token_labels,
                vmin=0.0,
                label="attention",
            )
            logger.experiment.add_figure(
                f"emission/{data_idx}", fig_em, global_step=self.global_step,
            )
            plt.close(fig_em)

            # Alignment plot: real tokens only
            spec = spectrograms[i, :T_i].detach().cpu().numpy()
            ps = pred_spans_all[i, 1::2].detach().cpu().numpy()  # [N_i, 2] odd indices
            fig_align = alignment_to_figure(
                spec, token_labels=real_labels, pred_spans=ps,
                title=item_path,
            )
            logger.experiment.add_figure(
                f"alignment/{data_idx}", fig_align, global_step=self.global_step,
            )
            plt.close(fig_align)

            # Reconstruction plot: original, masked, reconstructed, |diff|
            spec_masked = outputs["masked_spec"][i, :T_i].detach().cpu().numpy()
            spec_recon = outputs["recon_spec"][i, :T_i].detach().cpu().numpy()
            fig_recon = reconstruction_to_figure(
                spec, spec_masked, spec_recon,
                title=item_path,
            )
            logger.experiment.add_figure(
                f"reconstruction/{data_idx}", fig_recon, global_step=self.global_step,
            )
            plt.close(fig_recon)
