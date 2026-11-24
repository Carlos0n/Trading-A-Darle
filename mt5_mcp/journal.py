"""Journal append-only (JSONL) de cada intento de orden, resultado y evento de riesgo."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def record(self, event: str, **data: Any) -> dict:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # errors="backslashreplace": un carácter no representable (p. ej. medio par sustituto) no impide anotar
        with self.path.open("a", encoding="utf-8", errors="backslashreplace") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def tail(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[dict] = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
