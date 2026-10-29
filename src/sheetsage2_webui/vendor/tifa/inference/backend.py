from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import torch
import torch.nn.functional as F
from torch import Tensor, nn

from lib.config.schema import InferenceConfig, ModelConfig
from lib.feature.mel import StretchableMelSpectrogram
from lib.path_traversal import first_choices
from modules.decoding import decode_alignment_flat
from modules.forced_alignment import ForcedAlignmentModel, ForcedAlignmentSSLModel
from modules.functional import cross_cosine_similarity
from .scoring import prepare_scoring, select_paths


@dataclass
class SpectrogramContext:
    """Batched mel spectrogram with a validity mask."""
    features: Tensor  # [B, T, C]
    mask: Tensor  # [B, T] bool, True = valid frame


@dataclass
class ScoreResult:
    """Output of pronunciation scoring / disambiguation."""

    choices: Tensor  # [B, W] 1-based complete candidate ID per word, 0 for absent words
    scores: Tensor | None  # [B, W, C] conditional whole-sample scores; invalid = -inf


@dataclass
class AlignResult:
    """Output of forced alignment decoding."""
    spans: Tensor  # [B, N, 2] in the requested unit (seconds or frames)
    similarity: Tensor  # [B, T, N] cross cosine similarity
    agreement: Tensor  # [B] per-sample mean probability of input tokens, range [0, 1]


class InferenceBackend(ABC):
    """Protocol for forced alignment inference backends.

    Two-method design:
    - score(): score pronunciation alternatives and pick the best path
    - align(): produce forced alignment for a token sequence
    """

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Audio sample rate."""

    @property
    @abstractmethod
    def timestep(self) -> float:
        """Seconds per frame."""

    @abstractmethod
    def spectrogram(
            self, waveform: Tensor, duration: Tensor,
    ) -> SpectrogramContext:
        """Compute mel spectrogram and frame validity mask.

        Args:
            waveform: ``[B, L]`` float audio samples, 0-padded.
            duration: ``[B]`` float seconds of actual audio per item.

        Returns:
            SpectrogramContext with ``features [B,T,C]`` and ``mask [B,T]``.
        """

    @abstractmethod
    def score(
            self,
            spec: SpectrogramContext,
            *,
            paths: Tensor,
            words: Tensor,
            candidates: Tensor,
            **kwargs,
    ) -> ScoreResult:
        """Score pronunciation alternatives and pick the best path.

        paths [B,P,C] keeps whole-word candidate columns. words [B,P]
        assigns rows to semantic words; candidates [B,W,C] marks valid
        candidates, including empty paths. Metadata stays outside scoring.
        Extra keyword arguments are backend-specific scoring options.
        """

    @abstractmethod
    def align(
            self,
            spec: SpectrogramContext,
            *,
            tokens: Tensor,
            groups: Tensor | None = None,
            unit: Literal["frame", "second"] = "second",
            **kwargs,
    ) -> AlignResult:
        """Produce forced alignment for a token sequence.

        Args:
            spec: SpectrogramContext.
            tokens: ``[B, N]`` int64 phoneme token IDs, 0-padded.
            groups: ``[B, N]`` int64 group IDs or None (each token = own group).
            unit: ``"second"`` returns spans in seconds (default);
                ``"frame"`` returns spans as frame indices.
            **kwargs: Backend-specific alignment options.

        Returns:
            AlignResult with spans in the requested unit and similarity matrix.
        """


class ForcedAlignmentInferenceModel(nn.Module, InferenceBackend):
    """Supervised FA inference with MLM-based pronunciation scoring."""

    def __init__(
            self,
            model_config: ModelConfig,
            inference_config: InferenceConfig | None = None,
            *,
            model: ForcedAlignmentModel | None = None,
            spec_fn: StretchableMelSpectrogram | None = None,
    ):
        super().__init__()
        if spec_fn is None:
            if inference_config is None:
                raise ValueError("inference_config is required when spec_fn is not provided.")
            feat = inference_config.features
            spec_fn = StretchableMelSpectrogram(
                sample_rate=feat.audio_sample_rate,
                n_mels=feat.spectrogram.num_bins,
                n_fft=feat.fft_size,
                win_length=feat.win_size,
                hop_length=feat.hop_size,
                fmin=feat.spectrogram.fmin,
                fmax=feat.spectrogram.fmax,
            )
        self.spec_fn = spec_fn
        self._timestep = spec_fn.hop_length / spec_fn.sample_rate
        self.model = model if model is not None else ForcedAlignmentModel(model_config)

    @property
    def timestep(self) -> float:
        return self._timestep

    @property
    def sample_rate(self) -> int:
        return self.spec_fn.sample_rate

    def spectrogram(
            self, waveform: Tensor, duration: Tensor,
    ) -> SpectrogramContext:
        features = self.spec_fn(waveform).permute(0, 2, 1)  # [B, T, C]
        T = features.shape[1]
        L = duration.div(self.timestep).round().long()  # [B]
        idx = torch.arange(T, dtype=torch.long, device=duration.device)
        mask = idx.unsqueeze(0) < L.unsqueeze(1)  # [B, T]
        return SpectrogramContext(features=features, mask=mask)

    def score(
            self,
            spec: SpectrogramContext,
            *,
            paths: Tensor,
            words: Tensor,
            candidates: Tensor,
            unit: Literal["levenshtein", "word", "none"] = "levenshtein",
            **kwargs,
    ) -> ScoreResult:
        choices = first_choices(candidates)
        if unit == "none":
            return ScoreResult(choices=choices, scores=None)
        masked_tokens, segments, mapping = prepare_scoring(paths, words, candidates, unit=unit)
        scores = torch.zeros_like(candidates, dtype=torch.float32).masked_fill(~candidates, -torch.inf)
        active = (segments > 0).any(dim=-1)
        # Scheduling empty/no-ambiguity batches is outside the numerical graph.
        if bool(active.any()):
            tokens = masked_tokens[active]
            _, _, _, logits = self.model(
                spec.features[active], tokens, spec.mask[active], tokens != 0,
            )
            picked, values = select_paths(
                logits.float().log_softmax(-1),
                paths[active], words[active], candidates[active],
                segments[active], mapping[active],
            )
            choices[active] = picked
            scores[active] = values
        return ScoreResult(choices=choices, scores=scores)

    def align(
            self,
            spec: SpectrogramContext,
            *,
            tokens: Tensor,
            groups: Tensor | None = None,
            unit: Literal["frame", "second"] = "second",
            skip_penalty: float = 0.5,
            **kwargs,
    ) -> AlignResult:
        n_mask = tokens != 0
        active = n_mask.any(dim=1) & spec.mask.any(dim=1)
        if not bool(active.all()):
            B, N = tokens.shape
            dtype = torch.long if unit == "frame" else torch.float32
            spans = torch.zeros((B, N, 2), dtype=dtype, device=tokens.device)
            similarity = spec.features.new_zeros((B, spec.features.shape[1], N), dtype=torch.float32)
            agreement = spec.features.new_zeros(B, dtype=torch.float32)
            if bool(active.any()):
                result = self.align(
                    SpectrogramContext(spec.features[active], spec.mask[active]),
                    tokens=tokens[active],
                    groups=groups[active] if groups is not None else None,
                    unit=unit,
                    skip_penalty=skip_penalty,
                    **kwargs,
                )
                spans[active, :result.spans.shape[1]] = result.spans
                similarity[active] = result.similarity
                agreement[active] = result.agreement
            return AlignResult(spans=spans, similarity=similarity, agreement=agreement)
        frame_features, _, token_features, token_logits = self.model(
            spec.features, tokens, spec.mask, n_mask,
        )
        similarity = cross_cosine_similarity(frame_features, token_features)
        frame_lengths = spec.mask.sum(dim=-1)
        token_lengths = n_mask.sum(dim=-1)
        spans = decode_alignment_flat(
            similarity, frame_lengths, token_lengths, groups=groups,
            skip_penalty=skip_penalty,
        )
        if unit == "second":
            spans = spans.float() * self.timestep
        probs = F.softmax(token_logits.float(), dim=-1)
        token_prob = probs.gather(-1, tokens.unsqueeze(-1)).squeeze(-1)
        agreement = (token_prob * n_mask.float()).sum(dim=-1) / n_mask.sum(dim=-1).clamp(min=1)
        return AlignResult(
            spans=spans,
            similarity=similarity,
            agreement=agreement,
        )


class ForcedAlignmentSSLInferenceModel(nn.Module, InferenceBackend):
    """SSL FA inference: attention-based similarity + decode. STUB."""

    def __init__(
            self,
            model_config: ModelConfig,
            inference_config: InferenceConfig,
            **kwargs,
    ):
        super().__init__()
        feat = inference_config.features
        self._timestep = feat.hop_size / feat.audio_sample_rate
        self.spec_fn = StretchableMelSpectrogram(
            sample_rate=feat.audio_sample_rate,
            n_mels=feat.spectrogram.num_bins,
            n_fft=feat.fft_size,
            win_length=feat.win_size,
            hop_length=feat.hop_size,
            fmin=feat.spectrogram.fmin,
            fmax=feat.spectrogram.fmax,
        )
        self.model = ForcedAlignmentSSLModel(model_config)

    @property
    def timestep(self) -> float:
        return self._timestep

    @property
    def sample_rate(self) -> int:
        return self.spec_fn.sample_rate

    def spectrogram(
            self, waveform: Tensor, duration: Tensor,
    ) -> SpectrogramContext:
        features = self.spec_fn(waveform).permute(0, 2, 1)
        T = features.shape[1]
        L = duration.div(self.timestep).round().long()
        idx = torch.arange(T, dtype=torch.long, device=duration.device)
        mask = idx.unsqueeze(0) < L.unsqueeze(1)
        return SpectrogramContext(features=features, mask=mask)

    def score(self, spec, *, paths, words, candidates, **kwargs):
        raise NotImplementedError("SSL inference not yet implemented")

    def align(
            self, spec, *, tokens, groups=None,
            unit: Literal["frame", "second"] = "second", **kwargs,
    ):
        raise NotImplementedError("SSL inference not yet implemented")
