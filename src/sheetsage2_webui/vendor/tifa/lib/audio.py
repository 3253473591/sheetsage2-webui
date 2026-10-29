import pathlib

import librosa
import numpy


def load_audio(filepath: pathlib.Path) -> tuple[numpy.ndarray, int]:
    """Load an audio file and return (waveform_float32, sample_rate).

    Opus files are decoded via opuscodec; all other formats use librosa.
    """
    suffix = filepath.suffix.lower()
    if suffix == ".opus":
        import opuscodec
        with open(filepath, "rb") as f:
            data = f.read()
        decoder = opuscodec.OpusBufferedDecoder()
        pcm = decoder.decode(data)
        waveform = pcm.astype(numpy.float32) / 32768.0
        if pcm.shape[1] > 1:
            waveform = waveform.mean(axis=1)
        else:
            waveform = waveform.ravel()
        return waveform, 48000

    waveform, sr = librosa.load(filepath, sr=None, mono=True)
    return waveform, sr
