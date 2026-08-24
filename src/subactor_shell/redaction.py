from __future__ import annotations

from collections.abc import Iterable


REDACTED = "[REDACTED]"


class ExactRedactor:
    def __init__(self, values: Iterable[str], replacement: str = REDACTED):
        self.replacement = replacement
        self.values = sorted({value for value in values if value}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for value in self.values:
            text = text.replace(value, self.replacement)
        return text


class StreamingRedactor:
    """Redact exact values without leaking a secret split across stream chunks."""

    def __init__(self, values: Iterable[str], replacement: str = REDACTED):
        self._exact = ExactRedactor(values, replacement)
        self._buffer = ""
        self._max_secret_length = max((len(value) for value in self._exact.values), default=1)

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buffer += chunk
        hold = self._max_secret_length - 1
        safe_cut = len(self._buffer) - hold
        if safe_cut <= 0:
            return ""

        # Gdy pełny sekret przecina granicę safe_cut, cofamy granicę do
        # początku sekretu. Powtarzamy skan aż granica się ustabilizuje, bo
        # cofnięcie przez jeden sekret może odsłonić przecięcie przez inny.
        while True:
            previous_cut = safe_cut
            for value in self._exact.values:
                start = self._buffer.find(value)
                while start != -1:
                    end = start + len(value)
                    if start < safe_cut < end:
                        safe_cut = min(safe_cut, start)
                        break
                    start = self._buffer.find(value, start + 1)
            if safe_cut == previous_cut:
                break

        if safe_cut <= 0:
            return ""
        ready = self._buffer[:safe_cut]
        self._buffer = self._buffer[safe_cut:]
        return self._exact.redact(ready)

    def finish(self) -> str:
        ready = self._exact.redact(self._buffer)
        self._buffer = ""
        return ready
