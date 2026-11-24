import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "history.json"
MAX_HISTORY_ITEMS = 100


def _read_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except (OSError, ValueError):
        pass
    return []


def _write_history(items: list[dict[str, Any]], path: Path = HISTORY_PATH) -> None:
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = f.name
            f.write(json.dumps(items[:MAX_HISTORY_ITEMS], ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_history(limit: int = 30, path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    items = _read_history(path)
    if limit <= 0:
        return items
    return items[:limit]


def append_history(entry: dict[str, Any], path: Path = HISTORY_PATH) -> dict[str, Any]:
    record = dict(entry)
    record.setdefault("id", uuid.uuid4().hex[:12])
    items = _read_history(path)
    items.insert(0, record)
    _write_history(items[:MAX_HISTORY_ITEMS], path)
    return record


def clear_history(path: Path = HISTORY_PATH) -> None:
    _write_history([], path)
