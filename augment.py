# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# author: adefossez

import random
import torch as th
from torch import nn
from torch.nn import functional as F

import julius
import numpy as np
import torch
from torch.nn import functional as F


def hz_to_mel(f):
    return 2595 * np.log10(1 + f / 700)


def mel_to_hz(m):
    return 700 * (10**(m / 2595) - 1)


def mel_frequencies(n_mels, fmin, fmax):
    low = hz_to_mel(fmin)
    high = hz_to_mel(fmax)
    mels = np.linspace(low, high, n_mels)
    return mel_to_hz(mels)


def convert_audio_channels(wav, channels=2):
    """Convert audio to the given number of channels."""
    *shape, src_channels, length = wav.shape
    if src_channels == channels:
        pass
    elif channels == 1:
        # Case 1:
        # The caller asked 1-channel audio, but the stream have multiple
        # channels, downmix all channels.
        wav = wav.mean(dim=-2, keepdim=True)
    elif src_channels == 1:
        # Case 2:
        # The caller asked for multiple channels, but the input file have
        # one single channel, replicate the audio over all channels.
        wav = wav.expand(*shape, channels, length)
    elif src_channels >= channels:
        # Case 3:
        # The caller asked for multiple channels, and the input file have
        # more channels than requested. In that case return the first channels.
        wav = wav[..., :channels, :]
    else:
        # Case 4: What is a reasonable choice here?
        raise ValueError('The audio file has less channels than requested but is not mono.')
    return wav


def convert_audio(wav, from_samplerate, to_samplerate, channels):
    """Convert audio from a given samplerate to a target one and target number of channels."""
    wav = convert_audio_channels(wav, channels)
    return julius.resample_frac(wav, from_samplerate, to_samplerate)


class LowPassFilters(torch.nn.Module):
    """
    Bank of low pass filters.

    Args:
        cutoffs (list[float]): list of cutoff frequencies, in [0, 1] expressed as `f/f_s` where
            f_s is the samplerate.
        width (int): width of the filters (i.e. kernel_size=2 * width + 1).
            Default to `2 / min(cutoffs)`. Longer filters will have better attenuation
            but more side effects.
    Shape:
        - Input: `(*, T)`
        - Output: `(F, *, T` with `F` the len of `cutoffs`.
    """

    def __init__(self, cutoffs: list, width: int = None):
        super().__init__()
        self.cutoffs = cutoffs
        if width is None:
            width = int(2 / min(cutoffs))
        self.width = width
        window = torch.hamming_window(2 * width + 1, periodic=False)
        t = np.arange(-width, width + 1, dtype=np.float32)
        filters = []
        for cutoff in cutoffs:
            sinc = torch.from_numpy(np.sinc(2 * cutoff * t)).type(torch.float32)
            filters.append(2 * cutoff * sinc * window)
        self.register_buffer("filters", torch.stack(filters).unsqueeze(1))

    def forward(self, input):
        *others, t = input.shape
        input = input.view(-1, 1, t)
        out = F.conv1d(input, self.filters, padding=self.width)
        return out.permute(1, 0, 2).reshape(-1, *others, t)

    def __repr__(self):
        return "LossPassFilters(width={},cutoffs={})".format(self.width, self.cutoffs)


class Remix(nn.Module):
    """Remix.
    Mixes different noises with clean speech within a given batch
    """

    def forward(self, sources):
        noise, clean = sources
        bs, *other = noise.shape
        device = noise.device
        perm = th.argsort(th.rand(bs, device=device), dim=0)
        return th.stack([noise[perm], clean])


class RevEcho(nn.Module):
    """
    Hacky Reverb but runs on GPU without slowing down training.
    This reverb adds a succession of attenuated echos of the input
    signal to itself. Intuitively, the delay of the first echo will happen
    after roughly 2x the radius of the room and is controlled by `first_delay`.
    Then RevEcho keeps adding echos with the same delay and further attenuation
    until the amplitude ratio between the last and first echo is 1e-3.
    The attenuation factor and the number of echos to adds is controlled
    by RT60 (measured in seconds). RT60 is the average time to get to -60dB
    (remember volume is measured over the squared amplitude so this matches
    the 1e-3 ratio).

    At each call to RevEcho, `first_delay`, `initial` and `RT60` are
    sampled from their range. Then, to prevent this reverb from being too regular,
    the delay time is resampled uniformly within `first_delay +- 10%`,
    as controlled by the `jitter` parameter. Finally, for a denser reverb,
    multiple trains of echos are added with different jitter noises.

    Args:
        - initial: amplitude of the first echo as a fraction
            of the input signal. For each sample, actually sampled from
            `[0, initial]`. Larger values means louder reverb. Physically,
            this would depend on the absorption of the room walls.
        - rt60: range of values to sample the RT60 in seconds, i.e.
            after RT60 seconds, the echo amplitude is 1e-3 of the first echo.
            The default values follow the recommendations of
            https://arxiv.org/ftp/arxiv/papers/2001/2001.08662.pdf, Section 2.4.
            Physically this would also be related to the absorption of the
            room walls and there is likely a relation between `RT60` and
            `initial`, which we ignore here.
        - first_delay: range of values to sample the first echo delay in seconds.
            The default values are equivalent to sampling a room of 3 to 10 meters.
        - repeat: how many train of echos with differents jitters to add.
            Higher values means a denser reverb.
        - jitter: jitter used to make each repetition of the reverb echo train
            slightly different. For instance a jitter of 0.1 means
            the delay between two echos will be in the range `first_delay +- 10%`,
            with the jittering noise being resampled after each single echo.
        - keep_clean: fraction of the reverb of the clean speech to add back
            to the ground truth. 0 = dereverberation, 1 = no dereverberation.
        - sample_rate: sample rate of the input signals.
    """

    def __init__(self, proba=0.5, initial=0.3, rt60=(0.3, 1.3), first_delay=(0.01, 0.03),
                 repeat=3, jitter=0.1, keep_clean=0.1, sample_rate=16000):
        super().__init__()
        self.proba = proba
        self.initial = initial
        self.rt60 = rt60
        self.first_delay = first_delay
        self.repeat = repeat
        self.jitter = jitter
        self.keep_clean = keep_clean
        self.sample_rate = sample_rate

    def _reverb(self, source, initial, first_delay, rt60):
        """
        Return the reverb for a single source.
        """
        length = source.shape[-1]
        reverb = th.zeros_like(source)
        for _ in range(self.repeat):
            frac = 1  # what fraction of the first echo amplitude is still here
            echo = initial * source
            while frac > 1e-3:
                # First jitter noise for the delay
                jitter = 1 + self.jitter * random.uniform(-1, 1)
                delay = min(
                    1 + int(jitter * first_delay * self.sample_rate),
                    length)
                # Delay the echo in time by padding with zero on the left
                echo = F.pad(echo[:, :, :-delay], (delay, 0))
                reverb += echo

                # Second jitter noise for the attenuation
                jitter = 1 + self.jitter * random.uniform(-1, 1)
                # we want, with `d` the attenuation, d**(rt60 / first_ms) = 1e-3
                # i.e. log10(d) = -3 * first_ms / rt60, so that
                attenuation = 10**(-3 * jitter * first_delay / rt60)
                echo *= attenuation
                frac *= attenuation
        return reverb

    def forward(self, wav):
        if random.random() >= self.proba:
            return wav
        noise, clean = wav
        # Sample characteristics for the reverb
        initial = random.random() * self.initial
        first_delay = random.uniform(*self.first_delay)
        rt60 = random.uniform(*self.rt60)

        reverb_noise = self._reverb(noise, initial, first_delay, rt60)
        # Reverb for the noise is always added back to the noise
        noise += reverb_noise
        reverb_clean = self._reverb(clean, initial, first_delay, rt60)
        # Split clean reverb among the clean speech and noise
        clean += self.keep_clean * reverb_clean
        noise += (1 - self.keep_clean) * reverb_clean

        return th.stack([noise, clean])


class BandMask(nn.Module):
    """BandMask.
    Maskes bands of frequencies. Similar to Park, Daniel S., et al.
    "Specaugment: A simple data augmentation method for automatic speech recognition."
    (https://arxiv.org/pdf/1904.08779.pdf) but over the waveform.
    """

    def __init__(self, maxwidth=0.2, bands=120, sample_rate=16_000):
        """__init__.

        :param maxwidth: the maximum width to remove
        :param bands: number of bands
        :param sample_rate: signal sample rate
        """
        super().__init__()
        self.maxwidth = maxwidth
        self.bands = bands
        self.sample_rate = sample_rate

    def forward(self, wav):
        bands = self.bands
        bandwidth = int(abs(self.maxwidth) * bands)
        mels = mel_frequencies(bands, 40, self.sample_rate/2) / self.sample_rate
        low = random.randrange(bands)
        high = random.randrange(low, min(bands, low + bandwidth))
        filters = LowPassFilters([mels[low], mels[high]]).to(wav.device)
        low, midlow = filters(wav)
        # band pass filtering
        out = wav - midlow + low
        return out


class Shift(nn.Module):
    """Shift."""

    def __init__(self, shift=8000, same=False):
        """__init__.

        :param shift: randomly shifts the signals up to a given factor
        :param same: shifts both clean and noisy files by the same factor
        """
        super().__init__()
        self.shift = shift
        self.same = same

    def forward(self, wav):
        sources, batch, channels, length = wav.shape
        length = length - self.shift
        if self.shift > 0:
            if not self.training:
                wav = wav[..., :length]
            else:
                offsets = th.randint(
                    self.shift,
                    [1 if self.same else sources, batch, 1, 1], device=wav.device)
                offsets = offsets.expand(sources, -1, channels, -1)
                indexes = th.arange(length, device=wav.device)
                wav = wav.gather(3, indexes + offsets)
        return wav