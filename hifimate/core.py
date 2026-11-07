"""Pure, dependency-free four-source Hi-Fi arbitration.

The real receivers and PipeWire/CamillaDSP graph belong behind :class:`Backend`.
This module owns policy: one current output, explicit playback requests,
generation fencing, output-ready handover, fade-in, and diagnostics. It is event
driven, so an idle system has no audio polling loop to consume CPU.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import cmath
import math
from typing import Iterable, Protocol


class Source(str, Enum):
    """The four independent logical input lanes."""

    BLUETOOTH = "bluetooth"
    MUSIC_ASSISTANT_DLNA = "music_assistant_dlna"
    DLNA = "dlna"
    AIRPLAY = "airplay"
    MA_DLNA = "music_assistant_dlna"


@dataclass(frozen=True)
class Intent:
    """An explicit user playback action from one receiver session."""

    source: Source
    session: str
    generation: int
    kind: str = "play"

    def __post_init__(self) -> None:
        if not isinstance(self.source, Source):
            try:
                object.__setattr__(self, "source", Source(self.source))
            except (TypeError, ValueError) as exc:
                raise TypeError("source must be a Source or source name") from exc
        if type(self.session) is not str or not 1 <= len(self.session) <= 128:
            raise ValueError("session must be a non-empty string of at most 128 characters")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in self.session):
            raise ValueError("session contains a control character")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        if type(self.kind) is not str or self.kind not in {"play", "resume"}:
            raise ValueError("kind must be play or resume")


@dataclass
class Session:
    source: Source
    session: str
    generation: int
    playing: bool = False
    stopped: bool = False


class Backend(Protocol):
    """Adapter for receiver pause and the local audio graph."""

    def pause(self, source: Source) -> bool: ...
    def prepare(self, source: Source, session: str) -> bool: ...
    def grant(self, source: Source, session: str) -> bool: ...
    def revoke(self, source: Source) -> None: ...
    def write(self, source: Source, samples: list[float]) -> None: ...


class MemoryBackend:
    """Deterministic backend useful for tests, demos and adapter development."""

    def __init__(self) -> None:
        self.pauses: list[Source] = []
        self.pause_results: list[bool] = []
        self.prepares: list[tuple[Source, str]] = []
        self.grants: list[tuple[Source, str]] = []
        self.revokes: list[Source] = []
        self.output: list[tuple[Source, list[float]]] = []
        self.fail_prepare: set[Source] = set()
        self.fail_grant: set[Source] = set()

    def pause(self, source: Source) -> bool:
        self.pauses.append(source)
        self.pause_results.append(True)
        return True

    def prepare(self, source: Source, session: str) -> bool:
        self.prepares.append((source, session))
        return source not in self.fail_prepare

    def grant(self, source: Source, session: str) -> bool:
        self.grants.append((source, session))
        return source not in self.fail_grant

    def revoke(self, source: Source) -> None:
        self.revokes.append(source)

    def write(self, source: Source, samples: list[float]) -> None:
        self.output.append((source, list(samples)))


@dataclass(frozen=True)
class PauseResult:
    source: Source
    requested: bool


@dataclass(frozen=True)
class Spectrum:
    bins: tuple[float, ...]
    source: Source | None


class Controller:
    """Single-writer controller for the four logical audio sources."""

    def __init__(
        self,
        backend: Backend,
        *,
        fade_step: float = 0.1,
        spectrum_enabled: bool = True,
        spectrum_decay: float = 0.75,
    ) -> None:
        if not all(callable(getattr(backend, name, None)) for name in
                   ("pause", "prepare", "grant", "revoke", "write")):
            raise TypeError("backend does not implement Backend")
        if type(fade_step) not in (int, float) or not math.isfinite(float(fade_step)) or not 0 < fade_step <= 1:
            raise ValueError("fade_step must be in (0, 1]")
        if type(spectrum_decay) not in (int, float) or not 0 <= spectrum_decay < 1:
            raise ValueError("spectrum_decay must be in [0, 1)")
        self.backend = backend
        self.fade_step = float(fade_step)
        self.spectrum_enabled = bool(spectrum_enabled)
        self.spectrum_decay = float(spectrum_decay)
        self.sessions: dict[Source, Session] = {}
        self.owner: Session | None = None
        self.idle = True
        self.fade = 0.0
        self.spectrum = Spectrum((), None)
        self.pause_results: deque[PauseResult] = deque(maxlen=128)
        self._latest_generation: dict[tuple[Source, str], int] = {}
        self._accepted: set[tuple[Source, str, int]] = set()

    @staticmethod
    def _source(value: Source | str) -> Source:
        if isinstance(value, Source):
            return value
        try:
            return Source(value)
        except (TypeError, ValueError) as exc:
            raise TypeError("unknown source") from exc

    @staticmethod
    def _samples(samples: Iterable[float]) -> list[float]:
        try:
            result = list(samples)
        except TypeError as exc:
            raise TypeError("samples must be an iterable of real numbers") from exc
        if len(result) > 1_000_000:
            raise ValueError("sample block is too large")
        for value in result:
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError("samples must contain finite real numbers")
        return [float(value) for value in result]

    def _pause(self, source: Source) -> None:
        try:
            result = bool(self.backend.pause(source))
        except Exception:
            result = False
        self.pause_results.append(PauseResult(source, result))

    def play(self, intent: Intent) -> bool:
        """Attempt a play/resume handover with an output-ready barrier."""
        if not isinstance(intent, Intent):
            raise TypeError("intent must be an Intent")
        key = (intent.source, intent.session, intent.generation)
        generation_key = (intent.source, intent.session)
        latest = self._latest_generation.get(generation_key, 0)
        if key in self._accepted or intent.generation < latest:
            return False
        if intent.generation > latest:
            self._latest_generation[generation_key] = intent.generation

        try:
            prepared = bool(self.backend.prepare(intent.source, intent.session))
        except Exception:
            prepared = False
        if not prepared:
            return False
        try:
            granted = bool(self.backend.grant(intent.source, intent.session))
        except Exception:
            granted = False
        if not granted:
            # Do not source-wide revoke a failed reconnect while the previous
            # generation of that same lane is still the live owner.
            if self.owner is None or self.owner.source is not intent.source:
                self._revoke(intent.source)
            return False

        old = self.owner
        candidate = Session(intent.source, intent.session, intent.generation, playing=True)
        self._accepted.add(key)
        self.sessions[intent.source] = candidate
        # A reconnect of the same logical lane has already been granted on the
        # same graph. Calling a source-wide revoke/pause here could tear down
        # the newly prepared candidate, so only cross-source handovers dispatch
        # the remote stop request.
        if old is not None and old.source is not candidate.source:
            self._pause(old.source)
            self._revoke(old.source)
        if old is not None:
            old.playing = False
        self.owner = candidate
        self.fade = 0.0
        self.idle = False
        self.spectrum = Spectrum((), intent.source)
        return True

    def stop(self, source: Source, session: str, generation: int) -> bool:
        """Stop only the exact current session; never stop a newer owner."""
        owner = self.owner
        try:
            source = self._source(source)
        except TypeError:
            return False
        if owner is None:
            return False
        if owner.source is not source or owner.session != session or owner.generation != generation:
            return False
        owner.stopped = True
        owner.playing = False
        self._revoke(source)
        self.owner = None
        self.idle = True
        self.fade = 0.0
        self.spectrum = Spectrum((), None)
        return True

    def pause(self, source: Source, session: str, generation: int) -> bool:
        """A local pause enters idle; resumption must be explicit."""
        return self.stop(source, session, generation)

    def write(self, source: Source, samples: Iterable[float]) -> bool:
        """Write only the current owner and update actual-output diagnostics."""
        source = self._source(source)
        block = self._samples(samples)
        owner = self.owner
        if owner is None or owner.source is not source or not owner.playing:
            return False
        gain = min(1.0, self.fade + self.fade_step)
        self.fade = gain
        rendered = [sample * gain for sample in block]
        self.backend.write(source, rendered)
        if self.spectrum_enabled:
            self.spectrum = Spectrum(self._fft(rendered), source)
        return True

    def tick(self) -> Spectrum:
        """Advance GUI diagnostics without polling audio."""
        if self.spectrum_enabled and self.spectrum.bins:
            self.spectrum = Spectrum(
                tuple(round(value * self.spectrum_decay, 6) for value in self.spectrum.bins),
                self.spectrum.source if self.owner is not None else None,
            )
        return self.spectrum

    def set_spectrum_enabled(self, enabled: bool) -> None:
        self.spectrum_enabled = bool(enabled)
        if not self.spectrum_enabled:
            self.spectrum = Spectrum((), self.owner.source if self.owner else None)

    def tick_idle(self) -> bool:
        return self.owner is None

    def status(self) -> dict[str, object]:
        """Return a serializable snapshot for a GUI or health endpoint."""
        owner = self.owner
        return {
            "idle": self.idle,
            "owner": None if owner is None else {
                "source": owner.source.value,
                "session": owner.session,
                "generation": owner.generation,
            },
            "fade": round(self.fade, 6),
            "spectrum": {
                "source": None if self.spectrum.source is None else self.spectrum.source.value,
                "bins": list(self.spectrum.bins),
            },
            "pause_results": [
                {"source": item.source.value, "requested": item.requested}
                for item in self.pause_results
            ],
        }

    def _revoke(self, source: Source) -> None:
        try:
            self.backend.revoke(source)
        except Exception:
            # Local ownership is authoritative; a failed cleanup must not make
            # an old lane appear active or block a subsequent handover.
            pass

    @staticmethod
    def _fft(samples: list[float]) -> tuple[float, ...]:
        n = min(64, len(samples))
        if not n:
            return ()
        values = samples[:n]
        return tuple(
            round(abs(sum(value * cmath.exp(-2j * math.pi * j * k / n)
                        for k, value in enumerate(values))), 6)
            for j in range(n // 2)
        )
