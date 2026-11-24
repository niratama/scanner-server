import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"

DEFAULT_BUTTON_PRESET_ID = "standard_doc"
MAX_CUSTOM_PRESETS = 50

DEFAULT_SCAN_SETTINGS: dict[str, Any] = {
    "resolution": 300,
    "mode": "Color",
    "duplex": True,
    "output_format": "pdf",
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
        "description": "300dpi・カラー・両面・PDF",
        "builtin": True,
        "settings": {**DEFAULT_SCAN_SETTINGS},
    },
    {
        "id": "high_res_duplex",
        "name": "高精細・両面",
        "description": "600dpi・カラー・両面・無劣化",
        "builtin": True,
        "settings": {
            **DEFAULT_SCAN_SETTINGS,
            "resolution": 600,
            "mode": "Color",
            "duplex": True,
            "skip_blank": True,
            "jpeg_quality": 0,
        },
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
        "description": "600dpi・カラー・片面・PNG",
        "builtin": True,
        "settings": {
            **DEFAULT_SCAN_SETTINGS,
            "resolution": 600,
            "duplex": False,
            "output_format": "png",
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
        return {
            "last_used": dict(DEFAULT_SCAN_SETTINGS),
            "custom_presets": [],
            "button_preset_id": DEFAULT_BUTTON_PRESET_ID,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid settings structure")
    except (OSError, ValueError):
        return {
            "last_used": dict(DEFAULT_SCAN_SETTINGS),
            "custom_presets": [],
            "button_preset_id": DEFAULT_BUTTON_PRESET_ID,
        }

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

    button_preset_id = str(data.get("button_preset_id") or DEFAULT_BUTTON_PRESET_ID)
    valid_ids = {p["id"] for p in BUILTIN_PRESETS} | {p["id"] for p in custom_presets}
    if button_preset_id not in valid_ids:
        button_preset_id = DEFAULT_BUTTON_PRESET_ID

    return {
        "last_used": merge_with_defaults(data.get("last_used")),
        "custom_presets": custom_presets,
        "button_preset_id": button_preset_id,
    }


def _write_store(data: dict[str, Any], path: Path = SETTINGS_PATH) -> None:
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
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
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


def get_settings_payload(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    store = _read_store(path)
    return {
        "defaults": dict(DEFAULT_SCAN_SETTINGS),
        "last_used": store["last_used"],
        "builtin_presets": BUILTIN_PRESETS,
        "custom_presets": store["custom_presets"],
        "button_preset_id": store["button_preset_id"],
    }


def save_last_used(settings: dict[str, Any], path: Path = SETTINGS_PATH) -> dict[str, Any]:
    store = _read_store(path)
    store["last_used"] = merge_with_defaults(settings)
    _write_store(store, path)
    return store["last_used"]


def resolve_preset_by_id(
    preset_id: str | None = None, path: Path = SETTINGS_PATH
) -> dict[str, Any]:
    store = _read_store(path)
    target_id = preset_id or store["button_preset_id"]
    for preset in (*BUILTIN_PRESETS, *store["custom_presets"]):
        if preset["id"] == target_id:
            return preset
    return BUILTIN_PRESETS[0]


def set_button_preset_id(preset_id: str, path: Path = SETTINGS_PATH) -> dict[str, Any]:
    store = _read_store(path)
    for preset in (*BUILTIN_PRESETS, *store["custom_presets"]):
        if preset["id"] == preset_id:
            store["button_preset_id"] = preset_id
            _write_store(store, path)
            return preset
    raise ValueError(f"指定されたプリセットが見つかりません: {preset_id}")


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

    if len(store["custom_presets"]) >= MAX_CUSTOM_PRESETS:
        raise ValueError(f"カスタムプリセットは最大 {MAX_CUSTOM_PRESETS} 件まで保存できます。")

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
    if store["button_preset_id"] == preset_id:
        store["button_preset_id"] = DEFAULT_BUTTON_PRESET_ID
    _write_store(store, path)
    return True
