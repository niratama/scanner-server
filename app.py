import asyncio
import logging
import subprocess
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from auth_store import SESSION_COOKIE_NAME, auth_manager, is_auth_enabled
from drive_client import (
    ROOT_FOLDER_NAME,
    create_subfolder,
    list_subfolders,
    normalize_subfolder_name,
)
from history_store import append_history, clear_history, load_history
from m5stick_bridge import M5StickBridge, build_state_payload
from scan_pipeline import is_scanner_online, run_scan_job
from settings_store import (
    delete_custom_preset,
    get_settings_payload,
    resolve_preset_by_id,
    save_custom_preset,
    save_last_used,
    set_button_preset_id,
)

logger = logging.getLogger("scanner_server")

STATE_CHANGING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
LOCALHOST_CLIENTS = {"127.0.0.1", "::1", "localhost"}

scan_lock = asyncio.Lock()
state = {
    "running": False,
    "powering_off": False,
    "message": "待機中",
    "last_error": None,
}
_app_loop: asyncio.AbstractEventLoop | None = None


def _current_m5_status() -> str:
    if state["powering_off"]:
        return "powering_off"
    if state["running"]:
        return "scanning"
    if not is_scanner_online():
        return "offline"
    return "idle"


def _build_m5_state() -> dict:
    qr_token, _ = auth_manager.get_qr_token()
    return build_state_payload(
        status=_current_m5_status(),
        qr_token=qr_token if is_auth_enabled() else "",
        settings_payload=get_settings_payload(),
    )


async def _schedule_poweroff(*, notify_m5: bool = True) -> tuple[bool, str]:
    """イベントループスレッド上でスキャンロック確認とシャットダウン予約を行う。"""
    if scan_lock.locked() or state["running"]:
        return False, "スキャン実行中のためシャットダウンできません"
    if state["powering_off"]:
        return False, "すでにシャットダウン処理中です"

    state["powering_off"] = True
    state["message"] = "シャットダウン処理中..."

    if notify_m5:
        m5_bridge.send_shutdown(reason="poweroff")

    async def _do_poweroff():
        await asyncio.sleep(1.5)
        await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-n", "/usr/bin/systemctl", "poweroff"],
            check=False,
        )

    asyncio.create_task(_do_poweroff())
    return True, "シャットダウンを開始しました"


def _on_m5_poweroff() -> tuple[bool, str]:
    """M5StickC 受信スレッドからの poweroff 要求をイベントループスレッドへ委譲する。"""
    loop = _app_loop
    if loop is None or not loop.is_running():
        return False, "サーバー準備中です"
    future = asyncio.run_coroutine_threadsafe(_schedule_poweroff(notify_m5=False), loop)
    try:
        return future.result(timeout=2.0)
    except Exception as exc:
        logger.warning("M5StickC からのシャットダウン要求処理に失敗しました: %s", exc)
        return False, "シャットダウン要求の処理に失敗しました"


def _on_m5_refresh_qr() -> None:
    auth_manager.get_qr_token(force_rotate=True)


def _on_m5_select_preset(preset_id: str) -> tuple[bool, str]:
    try:
        preset = set_button_preset_id(preset_id)
        return True, preset["name"]
    except ValueError as e:
        return False, str(e)


m5_bridge = M5StickBridge(
    get_state_fn=_build_m5_state,
    on_refresh_qr=_on_m5_refresh_qr,
    on_select_preset=_on_m5_select_preset,
    on_poweroff=_on_m5_poweroff,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _app_loop
    _app_loop = asyncio.get_running_loop()
    auth_manager.get_qr_token()
    m5_bridge.start()
    try:
        yield
    finally:
        reason = "poweroff" if state["powering_off"] else "restart"
        m5_bridge.stop(shutdown_reason=reason)
        _app_loop = None


app = FastAPI(title="DR-C125 Scanner", lifespan=lifespan)


PROXY_FORWARD_HEADERS = (
    "x-forwarded-for",
    "forwarded",
    "x-real-ip",
    "x-forwarded-host",
    "x-forwarded-proto",
)


def _is_local_request(request: Request) -> bool:
    client = request.client
    if not (client and client.host in LOCALHOST_CLIENTS):
        return False
    return not any(request.headers.get(h) for h in PROXY_FORWARD_HEADERS)


def _is_https_request(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _apply_security_headers(response, *, no_store: bool = False):
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if no_store:
        response.headers.setdefault("Cache-Control", "no-store")
    if "text/html" in response.headers.get("content-type", ""):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
        )
    return response


def _render_auth_page(title: str, lead: str, sub: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - DR-C125 スキャナー</title>
<style>
  body {{
    font-family: system-ui, -apple-system, sans-serif;
    max-width: 440px;
    margin: 0 auto;
    padding: 32px 16px;
    background: #f4f6f8;
    color: #202124;
  }}
  .card {{
    background: #ffffff;
    border-radius: 14px;
    padding: 24px 20px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
    text-align: center;
  }}
  .icon {{
    font-size: 2.4rem;
    margin-bottom: 8px;
  }}
  h1 {{
    font-size: 1.2rem;
    margin: 0 0 10px;
  }}
  p {{
    font-size: 0.92rem;
    color: #5f6368;
    line-height: 1.6;
    margin: 0 0 10px;
  }}
  .sub {{
    font-size: 0.82rem;
    color: #80868b;
    margin-top: 12px;
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon" aria-hidden="true">&#128274;</div>
    <h1>{title}</h1>
    <p>{lead}</p>
    <p class="sub">{sub}</p>
  </div>
</body>
</html>
"""


AUTH_REQUIRED_HTML = _render_auth_page(
    "QR 認証が必要です",
    "スキャナー横の M5StickC に表示されている QR コードをスマホのカメラで読み取ってください。",
    "認証すると、このブラウザでは 30 日間ログイン状態が維持されます。",
)

AUTH_EXPIRED_HTML = _render_auth_page(
    "QR コードの有効期限が切れています",
    "この QR コードはすでに使用されたか、有効期限が切れています。M5StickC に表示されている最新の QR コードをもう一度読み取ってください。",
    "M5StickC の前面ボタン（BtnA）を押すと新しい QR コードを手動発行できます。",
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    is_auth_path = path.startswith(("/a/", "/api/auth/"))

    if request.method in STATE_CHANGING_METHODS:
        if not request.headers.get("x-requested-with"):
            return _apply_security_headers(
                JSONResponse(
                    status_code=403,
                    content={"detail": "不正なリクエストです（CSRF 保護）"},
                ),
                no_store=is_auth_path,
            )
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if urlparse(origin).netloc != host:
                return _apply_security_headers(
                    JSONResponse(
                        status_code=403,
                        content={"detail": "クロスオリジンリクエストは許可されていません"},
                    ),
                    no_store=is_auth_path,
                )

    if is_auth_enabled():
        is_qr_login = request.method == "GET" and path.startswith("/a/")
        is_local_button = (
            request.method == "POST"
            and path == "/api/trigger/button"
            and _is_local_request(request)
        )
        if not is_qr_login and not is_local_button:
            session_token = request.cookies.get(SESSION_COOKIE_NAME)
            if auth_manager.verify_session(session_token) is None:
                if path.startswith("/api/"):
                    response = JSONResponse(
                        status_code=401,
                        content={"detail": "認証が必要です。M5StickC の QR コードを読み取ってください。"},
                    )
                else:
                    response = HTMLResponse(content=AUTH_REQUIRED_HTML, status_code=401)
                return _apply_security_headers(response, no_store=True)

    response = await call_next(request)
    return _apply_security_headers(response, no_store=is_auth_path)


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
    subfolder: str = Field(default="", max_length=200)


class ScanRequest(ScanSettings):
    filename: str = Field(default="", max_length=120)
    preset_name: str = Field(default="", max_length=80)


class PresetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=120)
    settings: ScanSettings


class ButtonPresetRequest(BaseModel):
    preset_id: str = Field(min_length=1, max_length=64)


class FolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


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
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        logger.warning("スキャンジョブが失敗しました: %s", e)
        state["message"] = "エラーが発生しました"
        state["last_error"] = str(e)
        return None
    except Exception:
        logger.exception("スキャン処理中に予期しないエラーが発生しました")
        state["message"] = "エラーが発生しました"
        state["last_error"] = "スキャン処理中にエラーが発生しました。サーバーログを確認してください。"
        return None
    finally:
        state["running"] = False
        scan_lock.release()
        m5_bridge.notify_state_changed()


@app.post("/api/scan")
async def start_scan(req: ScanRequest):
    try:
        normalize_subfolder_name(req.subfolder)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if state["powering_off"]:
        raise HTTPException(status_code=409, detail="シャットダウン処理中のためスキャンできません")
    if scan_lock.locked():
        raise HTTPException(status_code=409, detail="現在スキャン実行中です")

    await scan_lock.acquire()
    try:
        try:
            save_last_used(req.model_dump(exclude={"filename", "preset_name"}))
        except OSError:
            pass
        state["running"] = True
        state["message"] = "スキャン開始..."
        state["last_error"] = None
        m5_bridge.notify_state_changed()
        asyncio.create_task(_execute_scan(req, trigger="web", preset_name=req.preset_name))
    except Exception:
        state["running"] = False
        scan_lock.release()
        m5_bridge.notify_state_changed()
        raise
    return {"status": "started"}


@app.post("/api/trigger/button")
async def trigger_button_scan(wait: bool = Query(default=False)):
    """DR-C125 本体スタートボタン（scanbd 等）から選択済みプロファイルでスキャンを実行する。"""
    if state["powering_off"]:
        raise HTTPException(status_code=409, detail="シャットダウン処理中のためスキャンできません")
    if scan_lock.locked():
        raise HTTPException(status_code=409, detail="現在スキャン実行中です")

    preset = resolve_preset_by_id()
    req = ScanRequest(**preset["settings"], filename="", preset_name=preset["name"])

    await scan_lock.acquire()
    try:
        state["running"] = True
        state["message"] = f"本体ボタン起動 ({preset['name']})..."
        state["last_error"] = None
        m5_bridge.notify_state_changed()

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
    except HTTPException:
        raise
    except Exception:
        state["running"] = False
        if scan_lock.locked():
            scan_lock.release()
        m5_bridge.notify_state_changed()
        raise
    return {
        "status": "started",
        "preset_id": preset["id"],
        "preset_name": preset["name"],
    }


@app.get("/api/status")
async def get_status():
    settings = get_settings_payload()
    return {
        "running": state["running"],
        "powering_off": state["powering_off"],
        "scanner_online": is_scanner_online(),
        "message": state["message"],
        "last_error": state["last_error"],
        "button_preset_id": settings["button_preset_id"],
        "m5stick_connected": m5_bridge.connected_port is not None,
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
    m5_bridge.notify_state_changed()
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
    m5_bridge.notify_state_changed()
    return {"preset": preset}


@app.delete("/api/presets/{preset_id}")
async def remove_preset(preset_id: str):
    deleted = delete_custom_preset(preset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="指定されたプリセットが見つかりません")
    m5_bridge.notify_state_changed()
    return {"status": "deleted", "id": preset_id}


@app.get("/api/folders")
async def get_folders():
    try:
        folders = await asyncio.to_thread(list_subfolders)
        return {"root": ROOT_FOLDER_NAME, "folders": folders, "error": None}
    except (FileNotFoundError, RuntimeError) as e:
        logger.warning("フォルダ一覧取得エラー: %s", e)
        return {"root": ROOT_FOLDER_NAME, "folders": [], "error": str(e)}
    except Exception:
        logger.exception("フォルダ一覧取得中に予期しないエラーが発生しました")
        return {
            "root": ROOT_FOLDER_NAME,
            "folders": [],
            "error": "Google Drive からのフォルダ取得に失敗しました",
        }


@app.post("/api/folders")
async def add_folder(req: FolderCreateRequest):
    try:
        created = await asyncio.to_thread(create_subfolder, req.name)
        return {"folder": created}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (FileNotFoundError, RuntimeError) as e:
        logger.warning("フォルダ作成エラー: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        logger.exception("フォルダ作成中に予期しないエラーが発生しました")
        raise HTTPException(
            status_code=500,
            detail="フォルダ作成に失敗しました。サーバーログを確認してください。",
        )


@app.get("/a/{token}")
async def qr_login(token: str, request: Request):
    """M5StickC に表示されたワンタイム QR トークンで 30 日セッション Cookie を発行する。"""
    if not is_auth_enabled():
        return RedirectResponse(url="/", status_code=303)
    session = auth_manager.consume_qr_token(token)
    if session is None:
        return HTMLResponse(content=AUTH_EXPIRED_HTML, status_code=401)
    m5_bridge.notify_state_changed()
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session["token"],
        max_age=session["max_age"],
        httponly=True,
        secure=_is_https_request(request),
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/api/auth/session")
async def get_auth_session(request: Request):
    enabled = is_auth_enabled()
    if not enabled:
        return {
            "auth_enabled": False,
            "authenticated": True,
            "expires_at": None,
            "remaining_seconds": None,
            "active_sessions": 0,
            "m5stick_connected": m5_bridge.connected_port is not None,
        }
    info = auth_manager.verify_session(request.cookies.get(SESSION_COOKIE_NAME))
    if info is None:
        raise HTTPException(status_code=401, detail="セッションが無効です")
    return {
        "auth_enabled": True,
        "authenticated": True,
        "expires_at": info["expires_at"],
        "remaining_seconds": info["remaining_seconds"],
        "active_sessions": info["active_sessions"],
        "m5stick_connected": m5_bridge.connected_port is not None,
    }


@app.post("/api/auth/refresh")
async def refresh_auth_session(request: Request):
    """スマホ UI から現在のセッションを 30 日間延長（トークンもローテーション）する。"""
    if not is_auth_enabled():
        return {"auth_enabled": False, "refreshed": False}
    refreshed = auth_manager.refresh_session(request.cookies.get(SESSION_COOKIE_NAME))
    if refreshed is None:
        raise HTTPException(status_code=401, detail="セッションが無効です")
    resp = JSONResponse(
        content={
            "auth_enabled": True,
            "refreshed": True,
            "expires_at": refreshed["expires_at"],
            "remaining_seconds": refreshed["remaining_seconds"],
            "active_sessions": refreshed["active_sessions"],
        }
    )
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=refreshed["token"],
        max_age=refreshed["max_age"],
        httponly=True,
        secure=_is_https_request(request),
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
async def logout_auth_session(request: Request):
    if is_auth_enabled():
        auth_manager.revoke_session(request.cookies.get(SESSION_COOKIE_NAME))
    resp = JSONResponse(content={"status": "logged_out"})
    resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return resp


@app.post("/api/poweroff")
async def poweroff():
    ok, detail = await _schedule_poweroff()
    if not ok:
        raise HTTPException(status_code=409, detail=detail)
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
  <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:12px;">
    <h1 style="margin:0;">DR-C125 ネットワークスキャナー</h1>
    <span id="scannerPowerBadge" class="badge" style="white-space:nowrap;">確認中...</span>
  </div>

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

  <div class="card" id="sessionCard" style="display:none;">
    <div class="card-header">
      <h2>認証セッション</h2>
      <span id="sessionRemainingBadge" class="badge">確認中...</span>
    </div>
    <div class="hint" id="sessionInfoText" style="margin:0 0 10px;">セッション情報を読み込み中...</div>
    <div class="inline-row">
      <button type="button" id="refreshSessionBtn" class="secondary" style="flex:1;">セッションを 30 日延長</button>
      <button type="button" id="logoutBtn" class="small-danger" style="padding:10px 12px;font-size:0.85rem;">ログアウト</button>
    </div>
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

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('X-Requested-With', 'scanner-ui');
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401) {
    window.location.reload();
  }
  return res;
}

function safeExternalUrl(raw) {
  if (!raw || typeof raw !== 'string') return null;
  try {
    const parsed = new URL(raw);
    return parsed.protocol === 'https:' ? parsed.href : null;
  } catch {
    return null;
  }
}

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
    const safeUrl = safeExternalUrl(h.url);
    if (safeUrl) {
      const a = document.createElement('a');
      a.href = safeUrl;
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
        const fileHref = safeExternalUrl(f.url);
        if (!fileHref) return;
        const link = document.createElement('a');
        link.href = fileHref;
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
    const res = await apiFetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();
    const busy = Boolean(data.running || data.powering_off);
    const scannerOnline = data.scanner_online !== false;
    const powerBadge = document.getElementById('scannerPowerBadge');
    if (scannerOnline) {
      powerBadge.textContent = 'スキャナー ON';
      powerBadge.style.background = '#e6f4ea';
      powerBadge.style.color = '#137333';
    } else {
      powerBadge.textContent = 'スキャナー OFF';
      powerBadge.style.background = '#fef7e0';
      powerBadge.style.color = '#b06000';
    }
    const statusText = (!busy && !scannerOnline)
      ? `${data.message}（スキャナーの電源が OFF です）`
      : data.message;
    document.getElementById('statusBox').textContent = statusText;
    document.getElementById('scanBtn').disabled = busy || !scannerOnline;
    document.getElementById('powerBtn').disabled = busy;
    document.getElementById('testButtonTriggerBtn').disabled = busy || !scannerOnline;
    if (data.button_preset_id && data.button_preset_id !== buttonPresetId) {
      buttonPresetId = data.button_preset_id;
      const sel = document.getElementById('buttonPresetSelect');
      if (sel) sel.value = buttonPresetId;
    }
    showError(data.last_error);
    renderHistory(data.history);
  } catch (e) {
    document.getElementById('statusBox').textContent = 'サーバーに接続できません（電源OFFまたは再起動中）';
  }
}

function renderSessionInfo(data) {
  const card = document.getElementById('sessionCard');
  if (!data || !data.auth_enabled) {
    card.style.display = 'none';
    return;
  }
  card.style.display = 'block';
  const badge = document.getElementById('sessionRemainingBadge');
  const info = document.getElementById('sessionInfoText');
  const remSec = Number(data.remaining_seconds || 0);
  const remDays = Math.max(0, Math.ceil(remSec / 86400));
  badge.textContent = `残り約 ${remDays} 日`;
  if (data.expires_at) {
    const exp = new Date(data.expires_at * 1000);
    const pad = n => String(n).padStart(2, '0');
    const expStr = `${exp.getFullYear()}/${pad(exp.getMonth() + 1)}/${pad(exp.getDate())} ${pad(exp.getHours())}:${pad(exp.getMinutes())}`;
    info.textContent = `有効期限: ${expStr} まで（有効端末: ${data.active_sessions || 1}）`;
  } else {
    info.textContent = 'ログイン中';
  }
}

async function loadSessionInfo() {
  try {
    const res = await apiFetch('/api/auth/session');
    if (!res.ok) return;
    const data = await res.json();
    renderSessionInfo(data);
  } catch (e) {
    // ignore transient network errors
  }
}

async function refreshSession() {
  const btn = document.getElementById('refreshSessionBtn');
  const info = document.getElementById('sessionInfoText');
  btn.disabled = true;
  try {
    const res = await apiFetch('/api/auth/refresh', { method: 'POST' });
    if (!res.ok) {
      showError('セッションの更新に失敗しました');
      return;
    }
    const data = await res.json();
    renderSessionInfo(data);
    info.textContent = `${info.textContent} — 更新しました`;
    showError(null);
  } catch (e) {
    showError('セッション更新中に通信エラーが発生しました');
  } finally {
    btn.disabled = false;
  }
}

async function logoutSession() {
  try {
    await apiFetch('/api/auth/logout', { method: 'POST' });
  } finally {
    window.location.reload();
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
    const res = await apiFetch('/api/scan', {
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
    const res = await apiFetch('/api/button-preset', {
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
    const res = await apiFetch('/api/trigger/button', { method: 'POST' });
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
    const res = await apiFetch('/api/presets', {
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
    const res = await apiFetch(`/api/presets/${encodeURIComponent(presetId)}`, { method: 'DELETE' });
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
    const res = await apiFetch('/api/folders', {
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
document.getElementById('refreshSessionBtn').addEventListener('click', refreshSession);
document.getElementById('logoutBtn').addEventListener('click', logoutSession);
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
  const res = await apiFetch('/api/poweroff', { method: 'POST' });
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
  const res = await apiFetch('/api/history', { method: 'DELETE' });
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
  loadSessionInfo();
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
