import abc
import math
import pathlib
import random
from dataclasses import dataclass
from typing import Generic, TypeVar

import librosa
import matplotlib.pyplot as plt
import numpy
import tqdm

from lib import logging
from lib.config.io import save_raw_config
from lib.config.schema import BinarizerConfig
from lib.indexed_dataset import IndexedDatasetBuilder
from lib.multiprocess import FailedItem, chunked_multiprocess_run
from lib.plot import vocab_distribution_to_figure
from lib.vocabulary import Vocabulary, VocabularyBuilder

ACCEPTED_AUDIO_FORMATS = {".wav", ".flac", ".opus"}


@dataclass
class MetadataItem(abc.ABC):
    name: str
    language: str
    waveform_fn: pathlib.Path
    estimated_duration: float
    raw_symbols: list[str]  # including stop symbols


MetadataT = TypeVar("MetadataT", bound=MetadataItem)


@dataclass
class DataSample:
    path: str
    name: str
    length: int
    data: dict[str, int | float | numpy.ndarray]
    derived: dict[str, int] = None
    error: str = None

    def __post_init__(self):
        if self.derived is None:
            self.derived = {}


class BaseBinarizer(abc.ABC, Generic[MetadataT]):
    __data_attrs__: list[str] = None

    def __init__(self, config: BinarizerConfig, eval_mode=False, aux_mode=False):
        self.config = config
        self.eval_mode = eval_mode
        self.aux_mode = aux_mode
        self.data_dir: pathlib.Path = self.resolve_data_dir()
        self.timestep = config.features.timestep

        self.vocabulary: Vocabulary | None = None
        self.vocab_builder: VocabularyBuilder | None = None

        self.valid_items: list[MetadataT] = []
        self.train_items: list[MetadataT] = []

    @abc.abstractmethod
    def resolve_data_dir(self) -> pathlib.Path:
        """Resolve this binarizer's source root and binary output directory."""

        pass

    @abc.abstractmethod
    def load_metadata(self, subset_dir: pathlib.Path) -> list[MetadataT]:
        pass

    @abc.abstractmethod
    def process_item(self, item: MetadataT) -> DataSample:
        pass

    def _resolve_validation_scope_dir(self) -> pathlib.Path:
        root = self.data_dir.resolve()
        subdir = self.config.split.subdir
        if subdir is None:
            return root

        relative_subdir = pathlib.Path(subdir)
        if relative_subdir.is_absolute():
            raise ValueError("binarizer.split.subdir must be relative to the main data directory.")

        scope_dir = (root / relative_subdir).resolve()
        try:
            relative_scope = scope_dir.relative_to(root)
        except ValueError as exc:
            raise ValueError("binarizer.split.subdir must stay inside the main data directory.") from exc
        if relative_scope == pathlib.Path("."):
            raise ValueError("Use an empty binarizer.split.subdir to select from the full dataset.")
        if not scope_dir.is_dir():
            raise ValueError(f"Validation split subdirectory does not exist: '{scope_dir.as_posix()}'.")
        return scope_dir

    @staticmethod
    def _item_is_in_dir(item: MetadataItem, directory: pathlib.Path) -> bool:
        try:
            item.waveform_fn.resolve().relative_to(directory)
        except ValueError:
            return False
        return True

    def _validation_sort_key(self, item: MetadataItem) -> tuple[str, str]:
        waveform_fn = item.waveform_fn.resolve()
        try:
            waveform_path = waveform_fn.relative_to(self.data_dir.resolve()).as_posix()
        except ValueError:
            waveform_path = waveform_fn.as_posix()
        return waveform_path, item.name

    def _select_validation_indices(
        self,
        metadata_list: list[MetadataT],
    ) -> set[int]:
        scope_dir = self._resolve_validation_scope_dir()
        candidates = [
            (index, item) for index, item in enumerate(metadata_list) if self._item_is_in_dir(item, scope_dir)
        ]
        candidates.sort(key=lambda entry: self._validation_sort_key(entry[1]))
        if not candidates:
            raise RuntimeError(f"Validation split scope contains no metadata items: " f"'{scope_dir.as_posix()}'.")

        split_config = self.config.split
        sample_count = len(candidates) if split_config.count < 0 else min(split_config.count, len(candidates))
        if sample_count == len(candidates):
            selected = candidates
        else:
            rng = random.SystemRandom() if split_config.seed is None else random.Random(split_config.seed)
            selected = rng.sample(candidates, k=sample_count)

        seed_description = "system randomness" if split_config.seed is None else str(split_config.seed)
        logging.info(
            f"Selected {len(selected)}/{len(candidates)} validation item(s) "
            f"from '{scope_dir.as_posix()}' with seed {seed_description}."
        )
        return {index for index, _ in selected}

    def split_dataset(self, metadata_list: list[MetadataT]):
        if self.aux_mode:
            self.train_items = sorted(
                metadata_list, key=lambda itm: itm.estimated_duration, reverse=True
            )
            return
        if self.eval_mode:
            # Put all items into validation set and leave training set empty.
            self.valid_items.extend(metadata_list)
            self.valid_items.sort(key=lambda itm: itm.estimated_duration, reverse=True)
        else:
            validation_indices = self._select_validation_indices(metadata_list)
            for i, item in enumerate(metadata_list):
                if i in validation_indices:
                    self.valid_items.append(item)
                else:
                    self.train_items.append(item)
            self.train_items.sort(key=lambda itm: itm.estimated_duration, reverse=True)
        if not self.eval_mode and not self.train_items:
            raise RuntimeError("Training set is empty.")
        if not self.valid_items:
            raise RuntimeError("Validation set is empty.")

    def process_items(self, items: list[MetadataT], prefix: str, multiprocessing=True):
        builder = IndexedDatasetBuilder(
            path=self.data_dir, prefix=prefix, allowed_attr=self.__data_attrs__
        )
        if multiprocessing and self.config.num_workers > 0:
            logging.debug(f"Processing {prefix} items with {self.config.num_workers} worker(s).")
            iterable = chunked_multiprocess_run(
                self.process_item, [(item,) for item in items], num_workers=self.config.num_workers
            )
        else:
            logging.debug(f"Processing {prefix} items in main process.")
            iterable = (self.process_item(item) for item in items)
        item_paths = []
        lengths = []
        attr_lengths = {}
        total_duration = 0
        with tqdm.tqdm(zip(items, iterable), total=len(items), desc=f"Processing {prefix} items") as progress:
            for item, sample in progress:
                if isinstance(sample, FailedItem):
                    logging.error(
                        f"Worker failed: {sample.exception}\n{sample.traceback_str}",
                        callback=progress.write
                    )
                    continue
                sample: DataSample
                if sample.error:
                    logging.error(
                        f"Error encountered in sample '{sample.name}': {sample.error}",
                        callback=progress.write
                    )
                    continue
                builder.add_item(sample.data)
                item_paths.append(sample.path)
                lengths.append(sample.length)
                for k, v in sample.data.items():
                    if isinstance(v, numpy.ndarray) and v.ndim > 0 and k not in sample.derived:
                        if k not in attr_lengths:
                            attr_lengths[k] = []
                        attr_lengths[k].append(v.shape[0])
                for k, v in sample.derived.items():
                    if k not in attr_lengths:
                        attr_lengths[k] = []
                    attr_lengths[k].append(v)
                duration = sample.length * self.timestep
                total_duration += duration
        builder.finalize()
        metadata = {
            "item_paths": item_paths,
            "lengths": lengths,
            **attr_lengths
        }
        metadata = {
            k: numpy.array(v)
            for k, v in metadata.items()
        }
        with open(self.data_dir / f"{prefix}.info.npz", "wb") as f:
            numpy.savez(f, **metadata)

        logging.info(f"Total duration of {prefix}: {format_duration(total_duration)}.")
        logging.debug(f"Processing {prefix} items done.")

    def collect_metadata(self) -> list[MetadataT]:
        index_file_paths = list(self.data_dir.rglob("index.csv"))
        metadata_list: list[MetadataT] = []
        for index_file_path in index_file_paths:
            subset_dir = index_file_path.parent
            subset_metadata_list = self.load_metadata(subset_dir)
            metadata_list.extend(subset_metadata_list)
            logging.debug(f"Loaded {len(subset_metadata_list)} metadata items from '{subset_dir.as_posix()}'.")
        return metadata_list

    def build_vocabulary(self, metadata_list: list[MetadataT]):
        prebuilt = None
        if self.config.vocabulary.prebuilt_vocab_file is not None:
            prebuilt = Vocabulary.from_file(self.config.vocabulary.prebuilt_vocab_file)
        vocab_builder = VocabularyBuilder(
            global_symbols=self.config.vocabulary.global_symbols,
            stop_symbols=self.config.vocabulary.stop_symbols,
            merged_groups=self.config.vocabulary.merged_groups,
            prebuilt_vocab=prebuilt,
        )
        for item in metadata_list:
            vocab_builder.add(item.raw_symbols, default_language=item.language)
        self.vocab_builder = vocab_builder
        self.vocabulary = vocab_builder.build()
        self.save_vocab_plot(vocab_builder.counter())

    def filter_metadata_by_vocabulary(
        self,
        metadata_list: list[MetadataT],
    ) -> list[MetadataT]:
        """Return metadata items compatible with the assigned vocabulary."""
        if self.vocabulary is None:
            raise RuntimeError("Vocabulary has not been built.")
        return metadata_list

    def save_vocab_plot(self, counter) -> None:
        fig = vocab_distribution_to_figure(counter)
        if fig is not None:
            filename = self.data_dir / "vocab_distribution.jpg"
            fig.savefig(fname=filename, bbox_inches="tight", pad_inches=0.25)
            plt.close(fig)
            logging.info(f"Vocabulary distribution plot saved to '{filename.as_posix()}'.")

    def build_dataset(self, metadata_list: list[MetadataT]):
        self.split_dataset(metadata_list)
        logging.info(f"Training set total size: {len(self.train_items)}.")
        logging.info(f"Validation set total size: {len(self.valid_items)}.")
        self._process_datasets()

    def save_auxiliary_files(self):
        save_raw_config(self.config.features.model_dump(), self.data_dir / "feature.yaml")
        self.vocabulary.dump(self.data_dir / "vocabulary.json")

    def _process_datasets(self):
        if self.aux_mode:
            self.process_items(self.train_items, prefix="aux", multiprocessing=True)
        elif self.eval_mode:
            self.process_items(self.valid_items, prefix="valid", multiprocessing=True)
        else:
            self.process_items(self.valid_items, prefix="valid", multiprocessing=False)
            self.process_items(self.train_items, prefix="train", multiprocessing=True)

    def process(self):
        metadata_list = self.collect_metadata()
        self.build_vocabulary(metadata_list)
        self.build_dataset(metadata_list)
        self.save_auxiliary_files()

    def get_frame_count(self, waveform_fn) -> int:
        duration = librosa.get_duration(path=waveform_fn)
        return max(1, math.ceil(duration / self.timestep))

    def sec_dur_to_frame_dur(self, dur_sec: numpy.ndarray, length: int) -> numpy.ndarray:
        dur_cumsum = numpy.round(numpy.cumsum(dur_sec, axis=0) / self.timestep).astype(numpy.int64)
        dur_cumsum = numpy.clip(dur_cumsum, a_min=0, a_max=length)
        dur_cumsum[-1] = length
        return numpy.diff(dur_cumsum, axis=0, prepend=numpy.array([0]))


def find_waveform_file(subset_dir: pathlib.Path, item_name: str) -> pathlib.Path:
    for ext in ACCEPTED_AUDIO_FORMATS:
        searched_wav_fn = subset_dir / "waveforms" / f"{item_name}{ext}"
        if searched_wav_fn.exists():
            return searched_wav_fn
    logging.error(
        f"Waveform file missing in raw dataset \'{subset_dir.as_posix()}\': "
        f"item {item_name}, searched extensions: {', '.join(ACCEPTED_AUDIO_FORMATS)}."
    )
    return None


def format_duration(seconds: float) -> str:
    """Formats a duration in seconds to a 'XhYmZs' string."""
    if seconds < 0:
        raise ValueError("Duration cannot be negative.")

    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    parts = []
    if hours > 0:
        parts.append(f"{int(hours)}h")
    if minutes > 0:
        parts.append(f"{int(minutes)}m")

    if sec > 0:
        if parts:
            parts.append(f"{int(sec)}s")
        else:
            parts.append(f"{sec:.2f}s")
    elif not parts:
        return "0s"

    return "".join(parts)
