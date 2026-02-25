from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_codex_binary() -> tuple[str | None, str]:
    env_path = str(os.environ.get("AGENTPR_CODEX_BIN", "")).strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if _is_executable_file(candidate):
            return str(candidate), "env:AGENTPR_CODEX_BIN"
        return None, f"AGENTPR_CODEX_BIN is not executable: {candidate}"

    path_hit = shutil.which("codex")
    if path_hit:
        return path_hit, "PATH"

    cursor_candidates = discover_cursor_codex_binaries()
    if cursor_candidates:
        return str(cursor_candidates[0]), "cursor-extension"

    return None, "not found in PATH or known Cursor extension paths"


def discover_cursor_codex_binaries() -> list[Path]:
    extensions_root = Path.home() / ".cursor" / "extensions"
    if not extensions_root.exists():
        return []

    patterns = (
        "openai.chatgpt-*-universal/bin/macos-aarch64/codex",
        "openai.chatgpt-*/bin/macos-aarch64/codex",
        "openai.chatgpt-*-universal/bin/linux-x64/codex",
        "openai.chatgpt-*/bin/linux-x64/codex",
    )

    hits: list[Path] = []
    for pattern in patterns:
        for item in extensions_root.glob(pattern):
            if _is_executable_file(item):
                hits.append(item)

    unique_hits = list(dict.fromkeys(hits))
    unique_hits.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return unique_hits


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)
