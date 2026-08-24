from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
import tempfile
from pathlib import Path

from .config import ensure_private_dir, ensure_private_file
from .models import Artifact, utc_now


class ArtifactError(RuntimeError):
    pass


_WORD_RE = re.compile(r"[\w.-]+", re.UNICODE)


def _tokens(value: str) -> set[str]:
    return {item.casefold() for item in _WORD_RE.findall(value) if len(item) > 1}


def select_relevant_text(
    content: str,
    query: str,
    *,
    max_chars: int,
    chunk_chars: int = 1800,
    max_chunks: int = 4,
) -> tuple[str, bool]:
    """Select lexical chunks locally instead of sending a whole artifact.

    This intentionally uses a cheap deterministic algorithm. It avoids an
    embedding dependency and keeps private source text local until a concrete
    query selects a bounded subset.
    """

    max_chars = max(256, int(max_chars))
    chunk_chars = max(256, int(chunk_chars))
    max_chunks = max(1, int(max_chunks))
    if len(content) <= max_chars:
        return content, False

    query_tokens = _tokens(query)
    overlap = max(96, chunk_chars // 6)
    step = max(1, chunk_chars - overlap)
    chunks: list[tuple[int, str]] = []
    for start in range(0, len(content), step):
        chunk = content[start : start + chunk_chars]
        if not chunk:
            break
        chunks.append((start, chunk))
        if start + chunk_chars >= len(content):
            break

    if not query_tokens:
        selected = chunks[:max_chunks]
    else:
        ranked: list[tuple[float, int, str]] = []
        for start, chunk in chunks:
            chunk_tokens = _tokens(chunk)
            shared = query_tokens & chunk_tokens
            coverage = len(shared) / max(1, len(query_tokens))
            density = len(shared) / max(1, len(chunk_tokens))
            early_bonus = 0.01 / (1 + start)
            ranked.append((0.82 * coverage + 0.18 * density + early_bonus, start, chunk))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        useful = [item for item in ranked if item[0] > 0]
        selected = [(start, chunk) for _, start, chunk in (useful or ranked)[:max_chunks]]
        selected.sort(key=lambda item: item[0])

    rendered: list[str] = []
    used = 0
    for start, chunk in selected:
        prefix = f"[fragment offset={start}]\n"
        remaining = max_chars - used
        if remaining <= len(prefix):
            break
        body = chunk[: remaining - len(prefix)]
        rendered.append(prefix + body)
        used += len(prefix) + len(body)
        if used >= max_chars:
            break
    return "\n\n".join(rendered), True


class ArtifactManager:
    def __init__(self, root: Path, *, max_bytes: int, max_text_chars: int):
        self.root = ensure_private_dir(root.expanduser())
        self.max_bytes = max_bytes
        self.max_text_chars = max_text_chars

    def import_file(self, source: Path) -> Artifact:
        source = source.expanduser()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise ArtifactError(f"Nie można otworzyć pliku: {source}") from exc
        temp_path: Path | None = None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactError("Załącznik musi być zwykłym plikiem")
            if info.st_size > self.max_bytes:
                raise ArtifactError(
                    f"Plik ma {info.st_size} B; limit wynosi {self.max_bytes} B"
                )
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=True) as source_handle:
                descriptor = -1
                with tempfile.NamedTemporaryFile(
                    dir=self.root, prefix=".incoming-", delete=False
                ) as target:
                    temp_path = Path(target.name)
                    while True:
                        chunk = source_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        target.write(chunk)
            artifact_id = digest.hexdigest()
            destination = self.root / artifact_id[:2] / artifact_id
            ensure_private_dir(destination.parent)
            if destination.exists():
                assert temp_path is not None
                temp_path.unlink(missing_ok=True)
            else:
                assert temp_path is not None
                os.replace(temp_path, destination)
                ensure_private_file(destination)
            mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            return Artifact(
                id=artifact_id,
                original_path=str(source.resolve(strict=False)),
                stored_path=destination,
                mime_type=mime_type,
                size=info.st_size,
                created_at=utc_now(),
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _is_textual(artifact: Artifact) -> bool:
        return artifact.mime_type.startswith("text/") or artifact.mime_type in {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/x-yaml",
            "application/toml",
            "application/javascript",
        }

    def read_text(self, artifact: Artifact) -> tuple[str, bool]:
        if not self._is_textual(artifact):
            return "", False
        try:
            with artifact.stored_path.open("r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(self.max_text_chars + 1)
        except OSError as exc:
            raise ArtifactError(f"Nie można odczytać artefaktu {artifact.id}") from exc
        return content[: self.max_text_chars], len(content) > self.max_text_chars

    def render_for_prompt(
        self,
        artifact: Artifact,
        *,
        query: str = "",
        max_chars: int | None = None,
        chunk_chars: int = 1800,
        max_chunks: int = 4,
    ) -> str:
        header = (
            f'<attachment id="sha256:{artifact.id}" name="{Path(artifact.original_path).name}" '
            f'mime="{artifact.mime_type}" bytes="{artifact.size}">'
        )
        if not self._is_textual(artifact):
            return f"{header}\n[BINARNY ZAŁĄCZNIK — treść nie została wstrzyknięta]\n</attachment>"
        content, source_truncated = self.read_text(artifact)
        limit = min(self.max_text_chars, max_chars or self.max_text_chars)
        selected, selection_truncated = select_relevant_text(
            content,
            query,
            max_chars=limit,
            chunk_chars=chunk_chars,
            max_chunks=max_chunks,
        )
        suffix = "\n[TRUNCATED/SELECTED LOCALLY]" if source_truncated or selection_truncated else ""
        return f"{header}\n{selected}{suffix}\n</attachment>"
