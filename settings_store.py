import json
import uuid
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"

DEFAULT_SCAN_SETTINGS: dict[str, Any] = {
    "resolution": 300,
    "mode": "Color",
    "duplex": True,
    "skip_blank": True,
    "blank_sensitivity": "normal",
    "swdeskew": True,
    "rollerdeskew": True,
    "swcrop": True,
    "df_length": True,
    "stapledetect": False,
    "buffermode": True,
    "rotation": 0,
    "jpeg_quality": 0,
    "brightness": 0,
    "contrast": 0,
    "subfolder": "",
}

BUILTIN_PRESETS: list[dict[str, Any]] = [
    {
        "id": "standard_doc",
        "name": "標準書類",
        "description": "300dpi・カラー・両面・白紙除去",
        "builtin": True,
        "settings": {**DEFAULT_SCAN_SETTINGS},
    },
    {
        "id": "receipt",
        "name": "領収書",
        "description": "300dpi・カラー・片面・自動切抜",
        "builtin": True,
        "settings": {
            **DEFAULT_SCAN_SETTINGS,
            "duplex": False,
            "skip_blank": False,
            "df_length": False,
            "stapledetect": False,
            "subfolder": "領収書",
        },
    },
    {
        "id": "light_memo",
        "name": "軽量メモ",
        "description": "200dpi・グレー・両面・軽量圧縮",
        "builtin": True,
        "settings": {
            **DEFAULT_SCAN_SETTINGS,
            "resolution": 200,
            "mode": "Gray",
            "jpeg_quality": 75,
        },
    },
    {
        "id": "mono_text",
        "name": "白黒くっきり",
        "description": "300dpi・白黒2値・両面・白紙除去(強)",
        "builtin": True,
        "settings": {
            **DEFAULT_SCAN_SETTINGS,
            "mode": "Lineart",
            "blank_sensitivity": "high",
        },
    },
    {
        "id": "high_res_photo",
        "name": "高精細・写真",
        "description": "600dpi・カラー・片面・無劣化",
        "builtin": True,
        "settings": {
            **DEFAULT_SCAN_SETTINGS,
            "resolution": 600,
            "duplex": False,
            "skip_blank": False,
            "df_length": False,
            "jpeg_quality": 0,
        },
    },
]


def merge_with_defaults(settings: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_SCAN_SETTINGS)
    if isinstance(settings, dict):
        for key in DEFAULT_SCAN_SETTINGS:
            if key in settings:
                merged[key] = settings[key]
    return merged


def _read_store(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"last_used": dict(DEFAULT_SCAN_SETTINGS), "custom_presets": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid settings structure")
    except (OSError, ValueError):
        return {"last_used": dict(DEFAULT_SCAN_SETTINGS), "custom_presets": []}

    custom_presets = []
    for item in data.get("custom_presets", []):
        if isinstance(item, dict) and item.get("id") and item.get("name"):
            custom_presets.append(
                {
                    "id": str(item["id"]),
                    "name": str(item["name"]),
                    "description": str(item.get("description", "")),
                    "builtin": False,
                    "settings": merge_with_defaults(item.get("settings")),
                }
            )

    return {
        "last_used": merge_with_defaults(data.get("last_used")),
        "custom_presets": custom_presets,
    }


def _write_store(data: dict[str, Any], path: Path = SETTINGS_PATH) -> None:
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def get_settings_payload(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    store = _read_store(path)
    return {
        "defaults": dict(DEFAULT_SCAN_SETTINGS),
        "last_used": store["last_used"],
        "builtin_presets": BUILTIN_PRESETS,
        "custom_presets": store["custom_presets"],
    }


def save_last_used(settings: dict[str, Any], path: Path = SETTINGS_PATH) -> dict[str, Any]:
    store = _read_store(path)
    store["last_used"] = merge_with_defaults(settings)
    _write_store(store, path)
    return store["last_used"]


def save_custom_preset(
    name: str,
    settings: dict[str, Any],
    description: str = "",
    preset_id: str | None = None,
    path: Path = SETTINGS_PATH,
) -> dict[str, Any]:
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("プリセット名を入力してください。")

    store = _read_store(path)
    merged = merge_with_defaults(settings)
    target_id = preset_id.strip() if preset_id else ""

    if target_id:
        for existing in store["custom_presets"]:
            if existing["id"] == target_id:
                existing["name"] = cleaned_name
                existing["description"] = description.strip()
                existing["settings"] = merged
                _write_store(store, path)
                return existing

    new_preset = {
        "id": target_id or f"custom_{uuid.uuid4().hex[:8]}",
        "name": cleaned_name,
        "description": description.strip(),
        "builtin": False,
        "settings": merged,
    }
    store["custom_presets"].append(new_preset)
    _write_store(store, path)
    return new_preset


def delete_custom_preset(preset_id: str, path: Path = SETTINGS_PATH) -> bool:
    store = _read_store(path)
    before = len(store["custom_presets"])
    store["custom_presets"] = [
        p for p in store["custom_presets"] if p["id"] != preset_id
    ]
    if len(store["custom_presets"]) == before:
        return False
    _write_store(store, path)
    return True
