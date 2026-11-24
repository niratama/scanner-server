import asyncio
import subprocess
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from drive_client import ROOT_FOLDER_NAME, create_subfolder, list_subfolders
from history_store import append_history, clear_history, load_history
from scan_pipeline import run_scan_job
from settings_store import (
    delete_custom_preset,
    get_settings_payload,
    resolve_preset_by_id,
    save_custom_preset,
    save_last_used,
    set_button_preset_id,
)

app = FastAPI(title="DR-C125 Scanner")

scan_lock = asyncio.Lock()
state = {
    "running": False,
    "message": "待機中",
    "last_error": None,
}


class ScanSettings(BaseModel):
    resolution: Literal[150, 200, 300, 400, 600] = 300
    mode: Literal["Color", "Gray", "Lineart"] = "Color"
    duplex: bool = True
    output_format: Literal["pdf", "jpeg", "png"] = "pdf"
    skip_blank: bool = True
    blank_sensitivity: Literal["low", "normal", "high"] = "normal"
    swdeskew: bool = True
    rollerdeskew: bool = True
    swcrop: bool = True
    df_length: bool = True
    stapledetect: bool = False
    buffermode: bool = True
    rotation: Literal[0, 90, 180, 270] = 0
    jpeg_quality: Literal[0, 60, 75, 85, 95] = 0
    brightness: int = Field(default=0, ge=-50, le=50)
    contrast: int = Field(default=0, ge=-50, le=50)
    subfolder: str = ""


class ScanRequest(ScanSettings):
    filename: str = ""
    preset_name: str = ""


class PresetCreateRequest(BaseModel):
    name: str
    description: str = ""
    settings: ScanSettings


class ButtonPresetRequest(BaseModel):
    preset_id: str


class FolderCreateRequest(BaseModel):
    name: str


async def _execute_scan(
    req: ScanRequest,
    *,
    trigger: str = "web",
    preset_name: str = "",
) -> dict | None:
    def update_msg(msg: str) -> None:
        state["message"] = msg

    try:
        result = await asyncio.to_thread(
            run_scan_job,
            resolution=req.resolution,
            mode=req.mode,
            duplex=req.duplex,
            output_format=req.output_format,
            skip_blank=req.skip_blank,
            blank_sensitivity=req.blank_sensitivity,
            swdeskew=req.swdeskew,
            rollerdeskew=req.rollerdeskew,
            swcrop=req.swcrop,
            df_length=req.df_length,
            stapledetect=req.stapledetect,
            buffermode=req.buffermode,
            rotation=req.rotation,
            jpeg_quality=req.jpeg_quality,
            brightness=req.brightness,
            contrast=req.contrast,
            subfolder=req.subfolder,
            filename=req.filename,
            trigger=trigger,
            preset_name=preset_name or req.preset_name,
            on_progress=update_msg,
        )
        saved = append_history(result)
        state["message"] = (
            f"完了: {saved['name']} ({saved['kept_pages']}ページ / {saved['size_mb']}MB)"
        )
        state["last_error"] = None
        return saved
    except Exception as e:
        state["message"] = "エラーが発生しました"
        state["last_error"] = str(e)
        return None
    finally:
        state["running"] = False
        scan_lock.release()


@app.post("/api/scan")
async def start_scan(req: ScanRequest):
    if scan_lock.locked():
        raise HTTPException(status_code=409, detail="現在スキャン実行中です")
    await scan_lock.acquire()
    try:
        save_last_used(req.model_dump(exclude={"filename", "preset_name"}))
    except OSError:
        pass
    state["running"] = True
    state["message"] = "スキャン開始..."
    state["last_error"] = None
    asyncio.create_task(_execute_scan(req, trigger="web", preset_name=req.preset_name))
    return {"status": "started"}


@app.post("/api/trigger/button")
async def trigger_button_scan(wait: bool = Query(default=False)):
    """DR-C125 本体スタートボタン（scanbd 等）から選択済みプロファイルでスキャンを実行する。"""
    if scan_lock.locked():
        raise HTTPException(status_code=409, detail="現在スキャン実行中です")

    preset = resolve_preset_by_id()
    req = ScanRequest(**preset["settings"], filename="", preset_name=preset["name"])

    await scan_lock.acquire()
    state["running"] = True
    state["message"] = f"本体ボタン起動 ({preset['name']})..."
    state["last_error"] = None

    if wait:
        result = await _execute_scan(req, trigger="button", preset_name=preset["name"])
        if result is None:
            raise HTTPException(
                status_code=500,
                detail=state["last_error"] or "スキャン中にエラーが発生しました",
            )
        return {
            "status": "completed",
            "preset_id": preset["id"],
            "preset_name": preset["name"],
            "result": result,
        }

    asyncio.create_task(_execute_scan(req, trigger="button", preset_name=preset["name"]))
    return {
        "status": "started",
        "preset_id": preset["id"],
        "preset_name": preset["name"],
    }


@app.get("/api/status")
async def get_status():
    return {
        "running": state["running"],
        "message": state["message"],
        "last_error": state["last_error"],
        "history": load_history(limit=20),
    }


@app.get("/api/history")
async def get_history(limit: int = Query(default=50, ge=1, le=100)):
    return {"history": load_history(limit=limit)}


@app.delete("/api/history")
async def delete_history():
    clear_history()
    return {"status": "cleared"}


@app.get("/api/settings")
async def get_settings():
    return get_settings_payload()


@app.put("/api/button-preset")
async def update_button_preset(req: ButtonPresetRequest):
    try:
        preset = set_button_preset_id(req.preset_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"button_preset_id": preset["id"], "preset": preset}


@app.post("/api/presets")
async def create_preset(req: PresetCreateRequest):
    try:
        preset = save_custom_preset(
            name=req.name,
            description=req.description,
            settings=req.settings.model_dump(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"preset": preset}


@app.delete("/api/presets/{preset_id}")
async def remove_preset(preset_id: str):
    deleted = delete_custom_preset(preset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="指定されたプリセットが見つかりません")
    return {"status": "deleted", "id": preset_id}


@app.get("/api/folders")
async def get_folders():
    try:
        folders = await asyncio.to_thread(list_subfolders)
        return {"root": ROOT_FOLDER_NAME, "folders": folders, "error": None}
    except Exception as e:
        return {"root": ROOT_FOLDER_NAME, "folders": [], "error": str(e)}


@app.post("/api/folders")
async def add_folder(req: FolderCreateRequest):
    try:
        created = await asyncio.to_thread(create_subfolder, req.name)
        return {"folder": created}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"フォルダ作成に失敗しました: {e}")


@app.post("/api/poweroff")
async def poweroff():
    if scan_lock.locked():
        raise HTTPException(status_code=409, detail="スキャン実行中のためシャットダウンできません")

    async def _do_poweroff():
        await asyncio.sleep(1.5)
        subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "poweroff"], check=False)

    asyncio.create_task(_do_poweroff())
    return {"status": "powering_off"}


HTML_UI = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DR-C125 スキャナー</title>
<style>
  :root {
    --brand-color: #1a73e8;
    --brand-soft: #e8f0fe;
    --brand-dark: #174ea6;
    --danger: #c5221f;
    --danger-soft: #fce8e6;
    --surface: #ffffff;
    --bg: #f4f6f8;
    --border: #dadce0;
    --text: #202124;
    --muted: #5f6368;
    color-scheme: light;
  }

  * { box-sizing: border-box; }

  body {
    font-family: system-ui, -apple-system, sans-serif;
    max-width: 520px;
    margin: 0 auto;
    padding: 16px;
    background: var(--bg);
    color: var(--text);
    accent-color: var(--brand-color);
  }

  h1 { font-size: 1.25rem; margin: 0 0 12px; }
  h2 { font-size: 0.95rem; margin: 0 0 10px; color: var(--text); }

  .card {
    background: var(--surface);
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 14px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }
  .card-header h2 { margin: 0; }

  .status {
    font-size: 0.95rem;
    padding: 10px 12px;
    border-radius: 8px;
    background: var(--brand-soft);
    color: var(--brand-dark);
    font-weight: 500;
  }

  .error {
    background: var(--danger-soft);
    color: #a50e0e;
    margin-top: 8px;
    padding: 10px 12px;
    border-radius: 8px;
    font-size: 0.9rem;
  }

  .hint {
    font-size: 0.8rem;
    color: var(--muted);
    margin-top: 4px;
  }

  label.field-label {
    display: block;
    font-size: 0.88rem;
    font-weight: 600;
    margin: 12px 0 4px;
  }

  select, input[type="text"] {
    width: 100%;
    padding: 10px;
    font-size: 1rem;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: #fff;
    color: var(--text);
  }

  input[type="range"] {
    width: 100%;
    margin: 6px 0 2px;
  }

  .row { display: flex; gap: 10px; }
  .row > div { flex: 1; }

  .inline-row {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .inline-row > select,
  .inline-row > input { flex: 1; min-width: 0; }

  /* Preset grid */
  .preset-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 12px;
    margin-bottom: 8px;
  }
  .preset-header span.title {
    font-size: 0.9rem;
    font-weight: 700;
  }
  .badge {
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--brand-soft);
    color: var(--brand-dark);
    font-weight: 600;
  }
  .badge.custom {
    background: #fef7e0;
    color: #b06000;
  }
  .tag {
    display: inline-block;
    font-size: 0.7rem;
    padding: 1px 6px;
    border-radius: 999px;
    background: #e8eaed;
    color: #3c4043;
    font-weight: 600;
    margin-right: 4px;
    vertical-align: middle;
  }
  .tag.btn-trigger {
    background: #e6f4ea;
    color: #137333;
  }

  .preset-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .preset-btn {
    width: 100%;
    margin: 0;
    padding: 10px;
    text-align: left;
    background: #f8f9fa;
    color: var(--text);
    border: 1.5px solid var(--border);
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.9rem;
    font-weight: 600;
    line-height: 1.3;
  }
  .preset-btn .preset-desc {
    display: block;
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--muted);
    margin-top: 2px;
  }
  .preset-btn[aria-pressed="true"] {
    background: var(--brand-soft);
    border-color: var(--brand-color);
    color: var(--brand-dark);
  }

  .summary-bar {
    margin-top: 10px;
    padding: 8px 10px;
    border-radius: 8px;
    background: #f8f9fa;
    border: 1px solid #e8eaed;
    font-size: 0.82rem;
    color: var(--muted);
    line-height: 1.45;
  }

  /* Buttons */
  button.primary {
    width: 100%;
    padding: 14px;
    font-size: 1.05rem;
    font-weight: 700;
    border: none;
    border-radius: 10px;
    cursor: pointer;
    margin-top: 14px;
    background: var(--brand-color);
    color: #fff;
  }
  button.primary:disabled { background: #9aa0a6; cursor: not-allowed; }

  button.secondary {
    padding: 10px 12px;
    font-size: 0.88rem;
    font-weight: 600;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: #fff;
    color: var(--text);
    cursor: pointer;
    white-space: nowrap;
  }
  button.secondary:hover { background: #f8f9fa; }

  button.danger-outline {
    width: 100%;
    padding: 10px;
    font-size: 0.95rem;
    font-weight: 700;
    border-radius: 10px;
    cursor: pointer;
    background: #fff;
    color: var(--danger);
    border: 1px solid var(--danger);
  }
  button.danger-outline:disabled { opacity: 0.5; cursor: not-allowed; }

  button.danger-solid {
    padding: 10px 14px;
    font-size: 0.92rem;
    font-weight: 700;
    border: none;
    border-radius: 8px;
    background: var(--danger);
    color: #fff;
    cursor: pointer;
  }

  button.small-danger {
    padding: 5px 10px;
    font-size: 0.78rem;
    font-weight: 600;
    border: 1px solid var(--danger);
    border-radius: 6px;
    background: #fff;
    color: var(--danger);
    cursor: pointer;
  }

  /* Checkboxes */
  .check-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 10px;
  }
  .check-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.92rem;
    font-weight: 500;
    cursor: pointer;
    margin: 0;
  }
  .check-item input[type="checkbox"] {
    width: 18px;
    height: 18px;
    margin: 0;
    flex-shrink: 0;
  }

  .slider-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.88rem;
    font-weight: 600;
    margin-top: 12px;
  }
  .slider-val {
    font-variant-numeric: tabular-nums;
    color: var(--brand-dark);
  }

  /* Accordion (modern-web-guidance: details + summary + ::details-content) */
  .accordion {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .accordion details {
    border: 1px solid #e8eaed;
    border-radius: 10px;
    background: #fff;
  }

  .accordion summary {
    list-style: none;
    display: flex;
    align-items: center;
    gap: 0.5ch;
    padding: 12px;
    font-size: 0.95rem;
    font-weight: 700;
    cursor: pointer;
    user-select: none;
  }
  .accordion summary::-webkit-details-marker { display: none; }
  .accordion summary::before {
    content: "";
    inline-size: 1em;
    block-size: 1em;
    flex-shrink: 0;
    background-color: currentColor;
    -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M6 9l6 6 6-6' fill='none' stroke='black' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") no-repeat center / contain;
    mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M6 9l6 6 6-6' fill='none' stroke='black' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") no-repeat center / contain;
    rotate: -90deg;
  }
  .accordion details[open] > summary::before {
    rotate: 0deg;
  }
  @media (prefers-reduced-motion: no-preference) {
    .accordion summary::before {
      transition: rotate 0.2s;
    }
  }

  .accordion details::details-content {
    interpolate-size: allow-keywords;
    overflow: clip;
    contain: layout;
  }
  .accordion details:not([open])::details-content {
    block-size: 0;
  }
  @media (prefers-reduced-motion: no-preference) {
    .accordion details::details-content {
      transition: block-size, content-visibility, display;
      transition-duration: 0.25s;
      transition-timing-function: ease-in-out;
      transition-behavior: allow-discrete;
    }
  }

  .details-body {
    padding: 0 12px 14px;
    border-top: 1px solid #f1f3f4;
  }

  /* Custom preset management list */
  .custom-preset-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 8px 0;
    border-top: 1px solid #f1f3f4;
    font-size: 0.88rem;
  }
  .custom-preset-item:first-child { border-top: none; }

  /* History */
  .hist-item {
    border-top: 1px solid #eee;
    padding: 8px 0;
    font-size: 0.9rem;
  }
  .hist-item:first-child { border-top: none; }
  .hist-meta {
    color: var(--muted);
    font-size: 0.8rem;
    margin-top: 2px;
  }
  .hist-files {
    margin-top: 4px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    font-size: 0.78rem;
  }
  a { color: var(--brand-color); text-decoration: none; font-weight: 600; }

  /* Native Modal Dialogs */
  dialog {
    border: none;
    border-radius: 14px;
    padding: 20px;
    max-width: 360px;
    width: calc(100% - 32px);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
  }
  dialog:modal {
    overscroll-behavior: contain;
  }
  dialog::backdrop {
    background: rgba(0, 0, 0, 0.45);
    overflow: hidden;
    overscroll-behavior: contain;
  }
  dialog h1 {
    font-size: 1.05rem;
    margin: 0 0 8px;
  }
  dialog p {
    font-size: 0.9rem;
    color: var(--muted);
    margin: 0 0 16px;
  }
  .dialog-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 14px;
  }

  /* Fallback for browsers without accent-color */
  @supports not (accent-color: var(--brand-color)) {
    .check-item {
      position: relative;
      padding-left: 1.8rem;
    }
    .check-item input[type="checkbox"] {
      position: absolute !important;
      clip-path: inset(50%) !important;
      overflow: hidden !important;
      width: 1px !important;
      height: 1px !important;
      margin: -1px !important;
      padding: 0 !important;
      border: 0 !important;
      white-space: nowrap !important;
    }
    .check-item input[type="checkbox"] + span::before {
      content: "";
      position: absolute;
      left: 0;
      top: 50%;
      transform: translateY(-50%);
      width: 1.15rem;
      height: 1.15rem;
      border: 2px solid #9aa0a6;
      background: #fff;
      border-radius: 4px;
    }
    .check-item input[type="checkbox"]:focus-visible + span::before {
      outline: 2px solid #000;
      outline-offset: 2px;
    }
    .check-item input[type="checkbox"]:checked + span::before {
      background-color: var(--brand-color);
      border-color: var(--brand-color);
    }
    .check-item input[type="checkbox"]:checked + span::after {
      content: "✓";
      position: absolute;
      left: 0.2rem;
      top: 50%;
      transform: translateY(-50%);
      color: #fff;
      font-weight: 700;
      font-size: 0.85rem;
    }
    input[type="range"] {
      -webkit-appearance: none;
      appearance: none;
      background: transparent;
    }
    input[type="range"]::-webkit-slider-runnable-track {
      width: 100%;
      height: 8px;
      background: linear-gradient(to right, var(--brand-color) var(--progress, 50%), #dadce0 var(--progress, 50%));
      border-radius: 4px;
    }
    input[type="range"]::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      height: 18px;
      width: 18px;
      border-radius: 50%;
      background: var(--brand-color);
      margin-top: -5px;
    }
  }
</style>
</head>
<body>
  <h1>DR-C125 ネットワークスキャナー</h1>

  <div class="card">
    <div id="statusBox" class="status" role="status" aria-live="polite">待機中</div>
    <div id="errorBox" class="error" style="display:none;" role="alert"></div>

    <div class="preset-header">
      <span class="title">プリセット選択</span>
      <span id="activePresetBadge" class="badge">標準書類</span>
    </div>
    <div id="presetGrid" class="preset-grid" role="group" aria-label="スキャンプリセット"></div>
    <div id="settingsSummary" class="summary-bar"></div>

    <label class="field-label" for="filename">ファイル名（空欄なら日時で自動命名）</label>
    <input type="text" id="filename" placeholder="例: 領収書_202611">

    <label class="field-label" for="subfolder">保存先フォルダ（Google Drive）</label>
    <div class="inline-row">
      <select id="subfolder">
        <option value="">DR-C125_Scans (ルート)</option>
      </select>
      <button type="button" class="secondary" id="newFolderBtn">+ 新規</button>
      <button type="button" class="secondary" id="refreshFoldersBtn" aria-label="フォルダ一覧を再読み込み">↻</button>
    </div>
    <div id="folderHint" class="hint"></div>

    <button type="button" id="scanBtn" class="primary">スキャン開始 → Drive保存</button>
  </div>

  <div class="card">
    <section class="accordion" aria-labelledby="advancedHeading">
      <h2 id="advancedHeading">詳細設定（必要なときだけ調整）</h2>

      <details>
        <summary>基本・画質・保存形式</summary>
        <div class="details-body">
          <div class="row">
            <div>
              <label class="field-label" for="resolution">解像度</label>
              <select id="resolution" data-setting="resolution">
                <option value="150">150 dpi (超軽量)</option>
                <option value="200">200 dpi (軽量)</option>
                <option value="300" selected>300 dpi (標準)</option>
                <option value="400">400 dpi (高画質)</option>
                <option value="600">600 dpi (高精細)</option>
              </select>
            </div>
            <div>
              <label class="field-label" for="mode">カラーモード</label>
              <select id="mode" data-setting="mode">
                <option value="Color" selected>カラー</option>
                <option value="Gray">グレースケール</option>
                <option value="Lineart">白黒2値 (文字向け)</option>
              </select>
            </div>
          </div>

          <label class="field-label" for="output_format">保存形式</label>
          <select id="output_format" data-setting="output_format">
            <option value="pdf" selected>PDF（複数ページを1ファイルに結合）</option>
            <option value="jpeg">JPEG（ページごとに画像保存）</option>
            <option value="png">PNG（無劣化・ページごとに画像保存）</option>
          </select>

          <div class="check-list">
            <label class="check-item">
              <input type="checkbox" id="duplex" data-setting="duplex" checked>
              <span>両面スキャン</span>
            </label>
          </div>
        </div>
      </details>

      <details>
        <summary>補正・検知・白紙除去</summary>
        <div class="details-body">
          <div class="check-list">
            <label class="check-item">
              <input type="checkbox" id="skip_blank" data-setting="skip_blank" checked>
              <span>白紙を自動除去</span>
            </label>
          </div>
          <label class="field-label" for="blank_sensitivity">白紙除去の感度</label>
          <select id="blank_sensitivity" data-setting="blank_sensitivity">
            <option value="low">弱（薄い文字を残しやすい）</option>
            <option value="normal" selected>標準</option>
            <option value="high">強（裏写り・薄い汚れも除去）</option>
          </select>

          <div class="check-list">
            <label class="check-item">
              <input type="checkbox" id="swdeskew" data-setting="swdeskew" checked>
              <span>ソフトウェア傾き補正</span>
            </label>
            <label class="check-item">
              <input type="checkbox" id="rollerdeskew" data-setting="rollerdeskew" checked>
              <span>機械的傾き補正（ローラー）</span>
            </label>
            <label class="check-item">
              <input type="checkbox" id="swcrop" data-setting="swcrop" checked>
              <span>余白の自動切り抜き</span>
            </label>
            <label class="check-item">
              <input type="checkbox" id="df_length" data-setting="df_length" checked>
              <span>重送検知（2枚送り防止）</span>
            </label>
            <label class="check-item">
              <input type="checkbox" id="stapledetect" data-setting="stapledetect">
              <span>ホチキス検知</span>
            </label>
            <label class="check-item">
              <input type="checkbox" id="buffermode" data-setting="buffermode" checked>
              <span>バッファモード（高速先読み）</span>
            </label>
          </div>
        </div>
      </details>

      <details>
        <summary>画像・PDF 調整</summary>
        <div class="details-body">
          <div class="row">
            <div>
              <label class="field-label" for="rotation">ページの回転</label>
              <select id="rotation" data-setting="rotation">
                <option value="0" selected>回転なし (0°)</option>
                <option value="90">右に90°</option>
                <option value="180">180° (上下反転)</option>
                <option value="270">左に90°</option>
              </select>
            </div>
            <div>
              <label class="field-label" for="jpeg_quality">JPEG 画質 / 圧縮</label>
              <select id="jpeg_quality" data-setting="jpeg_quality">
                <option value="0" selected>そのまま (無劣化)</option>
                <option value="95">高画質 (95%)</option>
                <option value="85">標準 (85%)</option>
                <option value="75">軽量 (75%)</option>
                <option value="60">超軽量 (60%)</option>
              </select>
            </div>
          </div>

          <div class="slider-header">
            <label for="brightness">明るさ</label>
            <span id="brightnessVal" class="slider-val">0</span>
          </div>
          <input type="range" id="brightness" data-setting="brightness" min="-50" max="50" step="5" value="0">

          <div class="slider-header">
            <label for="contrast">コントラスト</label>
            <span id="contrastVal" class="slider-val">0</span>
          </div>
          <input type="range" id="contrast" data-setting="contrast" min="-50" max="50" step="5" value="0">

          <div class="hint" id="formatNote" style="display:none;"></div>
          <div style="margin-top: 10px;">
            <button type="button" class="secondary" id="resetImageBtn">画像調整をリセット</button>
          </div>
        </div>
      </details>

      <details>
        <summary>本体スタートボタン連動</summary>
        <div class="details-body">
          <label class="field-label" for="buttonPresetSelect">本体ボタン押下時に使うプロファイル</label>
          <div class="inline-row">
            <select id="buttonPresetSelect"></select>
            <button type="button" class="secondary" id="testButtonTriggerBtn">テスト実行</button>
          </div>
          <div id="buttonPresetHint" class="hint">
            DR-C125 本体のスタートボタン（scanbd 連動）を押したときに実行されるプリセットを選択します。
          </div>
        </div>
      </details>

      <details>
        <summary>プリセット管理</summary>
        <div class="details-body">
          <label class="field-label" for="newPresetName">現在の設定を新しいプリセットとして保存</label>
          <div class="inline-row">
            <input type="text" id="newPresetName" placeholder="例: 確定申告レシート">
            <button type="button" class="secondary" id="savePresetBtn">保存</button>
          </div>
          <div id="customPresetList" style="margin-top: 10px;"></div>
        </div>
      </details>
    </section>
  </div>

  <div class="card">
    <div class="card-header">
      <h2>スキャン履歴（サーバー保存）</h2>
      <button type="button" id="clearHistoryBtn" class="small-danger">履歴をクリア</button>
    </div>
    <div id="historyList" style="color:var(--muted); font-size:0.9rem;">まだ履歴はありません</div>
  </div>

  <div class="card">
    <button type="button" id="powerBtn" class="danger-outline">DG-CANPC を安全にシャットダウン</button>
  </div>

  <dialog id="poweroffDialog" aria-labelledby="poweroffTitle">
    <h1 id="poweroffTitle">シャットダウンの確認</h1>
    <p>DG-CANPC の電源を切りますか？スキャン完了後に実行してください。</p>
    <form method="dialog" class="dialog-actions">
      <button value="cancel" class="secondary" autofocus>キャンセル</button>
      <button value="confirm" class="danger-solid">電源を切る</button>
    </form>
  </dialog>

  <dialog id="clearHistoryDialog" aria-labelledby="clearHistoryTitle">
    <h1 id="clearHistoryTitle">履歴のクリア</h1>
    <p>サーバーに保存されているスキャン履歴をすべて削除しますか？（Google Drive 上のファイルは削除されません）</p>
    <form method="dialog" class="dialog-actions">
      <button value="cancel" class="secondary" autofocus>キャンセル</button>
      <button value="confirm" class="danger-solid">削除する</button>
    </form>
  </dialog>

  <dialog id="folderDialog" aria-labelledby="folderDialogTitle" closedby="any">
    <h1 id="folderDialogTitle">保存先フォルダを作成</h1>
    <p>DR-C125_Scans 配下にサブフォルダを作成します。</p>
    <form method="dialog" id="folderForm">
      <input type="text" id="newFolderName" placeholder="例: 領収書 または 2026/領収書" autofocus>
      <div class="dialog-actions">
        <button value="cancel" type="submit" class="secondary" formnovalidate>キャンセル</button>
        <button value="create" type="submit" class="primary" style="width:auto;margin:0;padding:10px 16px;font-size:0.92rem;">作成</button>
      </div>
    </form>
  </dialog>

<script>
const SETTING_KEYS = [
  'resolution', 'mode', 'duplex', 'output_format', 'skip_blank', 'blank_sensitivity',
  'swdeskew', 'rollerdeskew', 'swcrop', 'df_length', 'stapledetect',
  'buffermode', 'rotation', 'jpeg_quality', 'brightness', 'contrast', 'subfolder'
];

let builtinPresets = [];
let customPresets = [];
let activePresetId = null;
let buttonPresetId = 'standard_doc';

function readSettingsFromUI() {
  return {
    resolution: Number(document.getElementById('resolution').value),
    mode: document.getElementById('mode').value,
    duplex: document.getElementById('duplex').checked,
    output_format: document.getElementById('output_format').value,
    skip_blank: document.getElementById('skip_blank').checked,
    blank_sensitivity: document.getElementById('blank_sensitivity').value,
    swdeskew: document.getElementById('swdeskew').checked,
    rollerdeskew: document.getElementById('rollerdeskew').checked,
    swcrop: document.getElementById('swcrop').checked,
    df_length: document.getElementById('df_length').checked,
    stapledetect: document.getElementById('stapledetect').checked,
    buffermode: document.getElementById('buffermode').checked,
    rotation: Number(document.getElementById('rotation').value),
    jpeg_quality: Number(document.getElementById('jpeg_quality').value),
    brightness: Number(document.getElementById('brightness').value),
    contrast: Number(document.getElementById('contrast').value),
    subfolder: document.getElementById('subfolder').value,
  };
}

function ensureSubfolderOption(folderName) {
  if (!folderName) return;
  const select = document.getElementById('subfolder');
  const exists = Array.from(select.options).some(opt => opt.value === folderName);
  if (!exists) {
    const opt = document.createElement('option');
    opt.value = folderName;
    opt.textContent = `DR-C125_Scans / ${folderName}`;
    select.appendChild(opt);
  }
}

function updateRangeProgress(el) {
  if (!CSS.supports('accent-color', 'auto')) {
    const min = Number(el.min);
    const max = Number(el.max);
    const pct = ((Number(el.value) - min) / (max - min)) * 100;
    el.style.setProperty('--progress', `${pct}%`);
  }
}

function syncDependentControls() {
  const skipBlank = document.getElementById('skip_blank').checked;
  document.getElementById('blank_sensitivity').disabled = !skipBlank;

  const isLineart = document.getElementById('mode').value === 'Lineart';
  const isPng = document.getElementById('output_format').value === 'png';
  document.getElementById('jpeg_quality').disabled = isLineart || isPng;
  document.getElementById('brightness').disabled = isLineart;
  document.getElementById('contrast').disabled = isLineart;

  const note = document.getElementById('formatNote');
  if (isLineart) {
    note.textContent = '※ 白黒2値 (Lineart) では JPEG 圧縮・明るさ・コントラストは無効です。';
    note.style.display = 'block';
  } else if (isPng) {
    note.textContent = '※ PNG 出力は無劣化のため JPEG 圧縮設定は無効です。';
    note.style.display = 'block';
  } else {
    note.style.display = 'none';
  }

  const bEl = document.getElementById('brightness');
  const cEl = document.getElementById('contrast');
  document.getElementById('brightnessVal').textContent = Number(bEl.value) > 0 ? `+${bEl.value}` : bEl.value;
  document.getElementById('contrastVal').textContent = Number(cEl.value) > 0 ? `+${cEl.value}` : cEl.value;
  updateRangeProgress(bEl);
  updateRangeProgress(cEl);
}

function settingsEqual(a, b) {
  return SETTING_KEYS.every(k => String(a[k]) === String(b[k]));
}

function findMatchingPreset(settings) {
  const all = [...builtinPresets, ...customPresets];
  return all.find(p => settingsEqual(p.settings, settings)) || null;
}

function renderSummary() {
  const s = readSettingsFromUI();
  const modeMap = { Color: 'カラー', Gray: 'グレー', Lineart: '白黒2値' };
  const sensMap = { low: '弱', normal: '標準', high: '強' };
  const parts = [
    `${s.resolution}dpi`,
    modeMap[s.mode] || s.mode,
    s.duplex ? '両面' : '片面',
    s.output_format.toUpperCase(),
    s.skip_blank ? `白紙除去(${sensMap[s.blank_sensitivity]})` : '白紙除去OFF',
  ];
  if (s.rotation !== 0) parts.push(`回転${s.rotation}°`);
  if (s.mode !== 'Lineart' && s.output_format !== 'png' && s.jpeg_quality > 0) {
    parts.push(`画質${s.jpeg_quality}%`);
  }
  if (s.mode !== 'Lineart' && (s.brightness !== 0 || s.contrast !== 0)) {
    parts.push(`明${s.brightness >= 0 ? '+' : ''}${s.brightness}/濃${s.contrast >= 0 ? '+' : ''}${s.contrast}`);
  }
  parts.push(s.subfolder ? `📁 ${s.subfolder}` : '📁 ルート');

  document.getElementById('settingsSummary').textContent = parts.join(' ・ ');

  const matched = findMatchingPreset(s);
  activePresetId = matched ? matched.id : null;
  const badge = document.getElementById('activePresetBadge');
  if (matched) {
    badge.textContent = matched.name;
    badge.className = 'badge';
  } else {
    badge.textContent = 'カスタム調整中';
    badge.className = 'badge custom';
  }
  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.setAttribute('aria-pressed', String(btn.dataset.presetId === activePresetId));
  });
}

function applySettingsToUI(settings) {
  ensureSubfolderOption(settings.subfolder || '');
  document.getElementById('resolution').value = String(settings.resolution);
  document.getElementById('mode').value = settings.mode;
  document.getElementById('duplex').checked = Boolean(settings.duplex);
  document.getElementById('output_format').value = settings.output_format || 'pdf';
  document.getElementById('skip_blank').checked = Boolean(settings.skip_blank);
  document.getElementById('blank_sensitivity').value = settings.blank_sensitivity;
  document.getElementById('swdeskew').checked = Boolean(settings.swdeskew);
  document.getElementById('rollerdeskew').checked = Boolean(settings.rollerdeskew);
  document.getElementById('swcrop').checked = Boolean(settings.swcrop);
  document.getElementById('df_length').checked = Boolean(settings.df_length);
  document.getElementById('stapledetect').checked = Boolean(settings.stapledetect);
  document.getElementById('buffermode').checked = Boolean(settings.buffermode);
  document.getElementById('rotation').value = String(settings.rotation);
  document.getElementById('jpeg_quality').value = String(settings.jpeg_quality);
  document.getElementById('brightness').value = String(settings.brightness);
  document.getElementById('contrast').value = String(settings.contrast);
  document.getElementById('subfolder').value = settings.subfolder || '';
  syncDependentControls();
  renderSummary();
}

function renderButtonPresetSelect() {
  const select = document.getElementById('buttonPresetSelect');
  select.replaceChildren();
  const all = [...builtinPresets, ...customPresets];
  for (const preset of all) {
    const opt = document.createElement('option');
    opt.value = preset.id;
    opt.textContent = preset.builtin ? preset.name : `${preset.name} (カスタム)`;
    select.appendChild(opt);
  }
  if (all.some(p => p.id === buttonPresetId)) {
    select.value = buttonPresetId;
  }
}

function renderPresets() {
  const grid = document.getElementById('presetGrid');
  grid.replaceChildren();

  const all = [...builtinPresets, ...customPresets];
  for (const preset of all) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'preset-btn';
    btn.dataset.presetId = preset.id;
    btn.setAttribute('aria-pressed', String(preset.id === activePresetId));

    const nameSpan = document.createElement('span');
    nameSpan.textContent = preset.name;
    btn.appendChild(nameSpan);

    const descSpan = document.createElement('span');
    descSpan.className = 'preset-desc';
    descSpan.textContent = preset.description || `${preset.settings.resolution}dpi・${preset.settings.mode}`;
    btn.appendChild(descSpan);

    btn.addEventListener('click', () => applySettingsToUI(preset.settings));
    grid.appendChild(btn);
  }

  renderButtonPresetSelect();

  const list = document.getElementById('customPresetList');
  list.replaceChildren();
  if (customPresets.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'hint';
    empty.textContent = '保存済みのカスタムプリセットはありません';
    list.appendChild(empty);
    return;
  }

  for (const preset of customPresets) {
    const row = document.createElement('div');
    row.className = 'custom-preset-item';
    const label = document.createElement('span');
    label.textContent = preset.name;
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'small-danger';
    delBtn.textContent = '削除';
    delBtn.addEventListener('click', () => deletePreset(preset.id));
    row.append(label, delBtn);
    list.appendChild(row);
  }
}

async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    builtinPresets = data.builtin_presets || [];
    customPresets = data.custom_presets || [];
    buttonPresetId = data.button_preset_id || 'standard_doc';
    renderPresets();
    applySettingsToUI(data.last_used || data.defaults);
  } catch (e) {
    showError('設定の読み込みに失敗しました');
  }
}

async function loadFolders() {
  const hint = document.getElementById('folderHint');
  const select = document.getElementById('subfolder');
  const current = select.value;
  hint.textContent = 'フォルダ一覧を取得中...';
  try {
    const res = await fetch('/api/folders');
    const data = await res.json();
    select.replaceChildren();
    const rootOpt = document.createElement('option');
    rootOpt.value = '';
    rootOpt.textContent = `${data.root || 'DR-C125_Scans'} (ルート)`;
    select.appendChild(rootOpt);

    for (const f of (data.folders || [])) {
      const opt = document.createElement('option');
      opt.value = f.name;
      opt.textContent = `${data.root || 'DR-C125_Scans'} / ${f.name}`;
      select.appendChild(opt);
    }
    ensureSubfolderOption(current);
    select.value = current;
    hint.textContent = data.error ? `Drive未接続: ${data.error}` : '';
  } catch (e) {
    hint.textContent = 'フォルダ一覧の取得に失敗しました';
  }
}

function showError(msg) {
  const errBox = document.getElementById('errorBox');
  if (msg) {
    errBox.textContent = msg;
    errBox.style.display = 'block';
  } else {
    errBox.style.display = 'none';
  }
}

function renderHistory(items) {
  const hist = document.getElementById('historyList');
  hist.replaceChildren();
  if (!items || items.length === 0) {
    hist.textContent = 'まだ履歴はありません';
    return;
  }
  for (const h of items) {
    const item = document.createElement('div');
    item.className = 'hist-item';

    const titleDiv = document.createElement('div');
    if (h.output_format) {
      const fmtTag = document.createElement('span');
      fmtTag.className = 'tag';
      fmtTag.textContent = String(h.output_format).toUpperCase();
      titleDiv.appendChild(fmtTag);
    }
    if (h.trigger === 'button') {
      const btnTag = document.createElement('span');
      btnTag.className = 'tag btn-trigger';
      btnTag.textContent = '本体ボタン';
      titleDiv.appendChild(btnTag);
    }
    if (h.url) {
      const a = document.createElement('a');
      a.href = h.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = h.name;
      titleDiv.appendChild(a);
    } else {
      const span = document.createElement('span');
      span.textContent = h.name;
      titleDiv.appendChild(span);
    }

    const metaDiv = document.createElement('div');
    metaDiv.className = 'hist-meta';
    const presetPart = h.preset_name ? `[${h.preset_name}] ` : '';
    const folderPart = h.folder ? `📁 ${h.folder} / ` : '';
    metaDiv.textContent = `${presetPart}${folderPart}${h.finished_at} / ${h.kept_pages}p (白紙除外:${h.removed_blank_pages}) / ${h.size_mb}MB`;

    item.append(titleDiv, metaDiv);

    if (Array.isArray(h.files) && h.files.length > 1) {
      const filesDiv = document.createElement('div');
      filesDiv.className = 'hist-files';
      h.files.forEach((f, idx) => {
        if (!f.url) return;
        const link = document.createElement('a');
        link.href = f.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = `p${idx + 1}`;
        filesDiv.appendChild(link);
      });
      item.appendChild(filesDiv);
    }

    hist.appendChild(item);
  }
}

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    document.getElementById('statusBox').textContent = data.message;
    document.getElementById('scanBtn').disabled = data.running;
    document.getElementById('powerBtn').disabled = data.running;
    document.getElementById('testButtonTriggerBtn').disabled = data.running;
    showError(data.last_error);
    renderHistory(data.history);
  } catch (e) {
    document.getElementById('statusBox').textContent = 'サーバーに接続できません（電源OFFまたは再起動中）';
  }
}

async function startScan() {
  const settings = readSettingsFromUI();
  const matched = findMatchingPreset(settings);
  const payload = {
    ...settings,
    filename: document.getElementById('filename').value,
    preset_name: matched ? matched.name : 'カスタム',
  };
  try {
    const res = await fetch('/api/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      document.getElementById('filename').value = '';
      showError(null);
      fetchStatus();
    } else {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || 'スキャン開始に失敗しました');
    }
  } catch (e) {
    showError('サーバーへの通信に失敗しました');
  }
}

async function updateButtonPreset(presetId) {
  const hint = document.getElementById('buttonPresetHint');
  try {
    const res = await fetch('/api/button-preset', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset_id: presetId }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || '本体ボタン設定の更新に失敗しました');
      return;
    }
    buttonPresetId = data.button_preset_id;
    hint.textContent = `本体ボタン連動プロファイルを「${data.preset.name}」に設定しました。`;
    showError(null);
  } catch (e) {
    showError('本体ボタン設定の更新中に通信エラーが発生しました');
  }
}

async function testButtonTrigger() {
  try {
    const res = await fetch('/api/trigger/button', { method: 'POST' });
    if (res.ok) {
      showError(null);
      fetchStatus();
    } else {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || '本体ボタンテスト実行に失敗しました');
    }
  } catch (e) {
    showError('サーバーへの通信に失敗しました');
  }
}

async function saveCustomPreset() {
  const input = document.getElementById('newPresetName');
  const name = input.value.trim();
  if (!name) {
    showError('プリセット名を入力してください');
    return;
  }
  const settings = readSettingsFromUI();
  const modeMap = { Color: 'カラー', Gray: 'グレー', Lineart: '白黒2値' };
  const desc = `${settings.resolution}dpi・${modeMap[settings.mode]}・${settings.duplex ? '両面' : '片面'}・${settings.output_format.toUpperCase()}`;
  try {
    const res = await fetch('/api/presets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description: desc, settings }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || 'プリセット保存に失敗しました');
      return;
    }
    const data = await res.json();
    customPresets.push(data.preset);
    input.value = '';
    showError(null);
    renderPresets();
    renderSummary();
  } catch (e) {
    showError('プリセット保存中に通信エラーが発生しました');
  }
}

async function deletePreset(presetId) {
  try {
    const res = await fetch(`/api/presets/${encodeURIComponent(presetId)}`, { method: 'DELETE' });
    if (res.ok) {
      customPresets = customPresets.filter(p => p.id !== presetId);
      if (buttonPresetId === presetId) {
        buttonPresetId = 'standard_doc';
      }
      renderPresets();
      renderSummary();
    }
  } catch (e) {
    showError('プリセット削除に失敗しました');
  }
}

async function createFolder(name) {
  const hint = document.getElementById('folderHint');
  hint.textContent = 'フォルダを作成中...';
  try {
    const res = await fetch('/api/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || 'フォルダ作成に失敗しました');
      hint.textContent = '';
      return;
    }
    ensureSubfolderOption(data.folder.name);
    document.getElementById('subfolder').value = data.folder.name;
    hint.textContent = `フォルダ「${data.folder.name}」を選択しました`;
    renderSummary();
  } catch (e) {
    showError('フォルダ作成中に通信エラーが発生しました');
    hint.textContent = '';
  }
}

// Event bindings
document.getElementById('scanBtn').addEventListener('click', startScan);
document.getElementById('savePresetBtn').addEventListener('click', saveCustomPreset);
document.getElementById('refreshFoldersBtn').addEventListener('click', loadFolders);
document.getElementById('buttonPresetSelect').addEventListener('change', e => updateButtonPreset(e.target.value));
document.getElementById('testButtonTriggerBtn').addEventListener('click', testButtonTrigger);
document.getElementById('resetImageBtn').addEventListener('click', () => {
  document.getElementById('rotation').value = '0';
  document.getElementById('jpeg_quality').value = '0';
  document.getElementById('brightness').value = '0';
  document.getElementById('contrast').value = '0';
  syncDependentControls();
  renderSummary();
});

for (const el of document.querySelectorAll('[data-setting], #subfolder')) {
  el.addEventListener('input', () => {
    syncDependentControls();
    renderSummary();
  });
  el.addEventListener('change', () => {
    syncDependentControls();
    renderSummary();
  });
}

// Poweroff modal dialog
const poweroffDialog = document.getElementById('poweroffDialog');
document.getElementById('powerBtn').addEventListener('click', () => poweroffDialog.showModal());
poweroffDialog.addEventListener('close', async () => {
  const choice = poweroffDialog.returnValue;
  poweroffDialog.returnValue = '';
  if (choice !== 'confirm') return;
  const res = await fetch('/api/poweroff', { method: 'POST' });
  if (res.ok) {
    document.getElementById('statusBox').textContent = 'シャットダウンを実行しました。数秒後に電源が切れます。';
  }
});

// Clear history modal dialog
const clearHistoryDialog = document.getElementById('clearHistoryDialog');
document.getElementById('clearHistoryBtn').addEventListener('click', () => clearHistoryDialog.showModal());
clearHistoryDialog.addEventListener('close', async () => {
  const choice = clearHistoryDialog.returnValue;
  clearHistoryDialog.returnValue = '';
  if (choice !== 'confirm') return;
  const res = await fetch('/api/history', { method: 'DELETE' });
  if (res.ok) {
    renderHistory([]);
  }
});

// Folder create modal dialog
const folderDialog = document.getElementById('folderDialog');
const newFolderInput = document.getElementById('newFolderName');
document.getElementById('newFolderBtn').addEventListener('click', () => {
  newFolderInput.value = '';
  folderDialog.showModal();
});
folderDialog.addEventListener('close', () => {
  const choice = folderDialog.returnValue;
  folderDialog.returnValue = '';
  if (choice === 'create' && newFolderInput.value.trim()) {
    createFolder(newFolderInput.value.trim());
  }
});

// Light-dismiss fallback for browsers without closedby="any"
if (!('closedBy' in HTMLDialogElement.prototype)) {
  const outside = (e, dialog) => {
    const r = dialog.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  };
  for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
    let pressedOutside = false;
    dialog.addEventListener('pointerdown', e => { pressedOutside = outside(e, dialog); });
    dialog.addEventListener('pointerup', e => { if (pressedOutside && outside(e, dialog)) dialog.close(); });
  }
}

(async function init() {
  await loadSettings();
  loadFolders();
  fetchStatus();
  setInterval(fetchStatus, 2000);
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_UI
