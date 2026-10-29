import csv
import pathlib
from dataclasses import dataclass

import numpy

from lib import logging
from .binarizer_base import (
    BaseBinarizer,
    MetadataItem,
    DataSample,
    find_waveform_file,
)

PHONEMES_ITEM_ATTRIBUTES = [
    "tokens",  # int64 [N], encoded group IDs for non-stop symbols, starting from 1
    "spans",  # int64 [N, 2], inclusive start and exclusive end frame for each non-stop symbol
    "regions",  # int64 [T], frame -> 1-based non-stop symbol occurrence index; 0 for stop-symbol spans
]


@dataclass
class PhonemeMetadataItem(MetadataItem):
    raw_durations: list[float]  # including stop symbols, in seconds


class PhonemeTimingBinarizer(BaseBinarizer[PhonemeMetadataItem]):
    __data_attrs__ = PHONEMES_ITEM_ATTRIBUTES

    def resolve_data_dir(self) -> pathlib.Path:
        return self.config.phoneme_timing_data_dir_resolved

    def load_metadata(self, subset_dir) -> list[PhonemeMetadataItem]:
        index_path = subset_dir / "index.csv"
        with open(index_path, "r", encoding="utf8") as f:
            items = list(csv.DictReader(f))
        metadata_items = []
        for item in items:
            name = item["name"]
            waveform_fn = find_waveform_file(subset_dir, name)
            if waveform_fn is None:
                continue
            symbols = item["phones"].split()
            language = item["language"]
            durations = [float(dur) for dur in item["durations"].split()]
            if len(symbols) != len(durations):
                logging.error(
                    f"Length mismatch in raw dataset '{subset_dir.as_posix()}': "
                    f"item '{name}', phones({len(symbols)}), durations({len(durations)})."
                )
                continue
            metadata_items.append(PhonemeMetadataItem(
                name=name,
                language=language,
                waveform_fn=waveform_fn,
                estimated_duration=sum(durations),
                raw_symbols=symbols,
                raw_durations=durations,
            ))
        return metadata_items

    def process_item(self, item: PhonemeMetadataItem) -> DataSample:
        if self.vocabulary is None:
            raise RuntimeError("Vocabulary has not been built.")
        length = self.get_frame_count(item.waveform_fn)
        frame_durations = self.sec_dur_to_frame_dur(numpy.array(item.raw_durations, dtype=numpy.float64), length)
        symbols = []
        tokens = []
        spans = []
        regions = numpy.zeros((length,), dtype=numpy.int64)
        cursor = 0
        for symbol, duration in zip(item.raw_symbols, frame_durations):
            start = cursor
            end = cursor + int(duration)
            cursor = end
            token = self.vocabulary.encode(symbol, item.language)
            if token is None:
                # Must be a stop symbol, because the vocabulary is built on symbols in the dataset.
                continue
            symbols.append(symbol)
            tokens.append(self.vocabulary.encode(symbol, item.language))
            spans.append((start, end))
            regions[start:end] = len(tokens)
        tokens = numpy.array(tokens, dtype=numpy.int64)
        spans = numpy.array(spans, dtype=numpy.int64).reshape((-1, 2))
        valid_frames = int((regions != 0).sum())
        data = {
            "tokens": tokens,
            "spans": spans,
            "regions": regions,
        }
        return DataSample(
            path=item.waveform_fn.relative_to(self.data_dir).as_posix(),
            name=item.name,
            length=length,
            data=data,
            derived={"valid_frames": valid_frames},
        )
