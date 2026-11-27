import json
import logging
import os
import socket
import threading
import time
from typing import Any, Callable

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - optional in minimal test envs
    serial = None  # type: ignore[assignment]
    list_ports = None  # type: ignore[assignment]

logger = logging.getLogger("scanner_server.m5stick")

DEFAULT_BAUDRATE = 115200
HEARTBEAT_INTERVAL_SEC = 3.0
RECONNECT_INTERVAL_SEC = 2.0
MAX_PRESETS_IN_PAYLOAD = 20

# M5StickC (初代: FTDI FT2232 / CP2104 / CH9102) の代表的な USB VID
PREFERRED_VIDS = {0x0403, 0x10C4, 0x1A86, 0x303A}


def detect_lan_ip() -> str:
    """デフォルトルートに紐づく LAN IP を取得する（外部へパケットは送信しない）。"""
    env_ip = os.environ.get("SCANNER_HOST_IP", "").strip()
    if env_ip:
        return env_ip
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        sock.close()
    return "127.0.0.1"


def build_base_url() -> tuple[str, str]:
    """(base_url, display_ip) を返す。"""
    explicit = os.environ.get("SCANNER_BASE_URL", "").strip().rstrip("/")
    if explicit:
        host_part = explicit.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return explicit, host_part
    ip = detect_lan_ip()
    port = os.environ.get("SCANNER_PORT", "8000").strip() or "8000"
    if port == "80":
        return f"http://{ip}", ip
    return f"http://{ip}:{port}", ip


def find_m5stick_port() -> str | None:
    """M5StickC の USB シリアルポートを自動検出する。"""
    explicit = os.environ.get("M5STICK_PORT", "").strip()
    if explicit:
        return explicit
    if list_ports is None:
        return None

    ports = list(list_ports.comports())
    if not ports:
        return None

    def _score(port_info: Any) -> tuple[int, str]:
        dev = getattr(port_info, "device", "") or ""
        vid = getattr(port_info, "vid", None)
        desc = (getattr(port_info, "description", "") or "").lower()
        score = 0
        if vid in PREFERRED_VIDS:
            score += 10
        if any(k in desc for k in ("m5", "ftdi", "cp210", "ch910", "usb serial", "uart")):
            score += 5
        if dev.startswith(("/dev/ttyUSB", "/dev/ttyACM")):
            score += 2
        return (-score, dev)

    candidates = [
        p
        for p in ports
        if (getattr(p, "device", "") or "").startswith(("/dev/ttyUSB", "/dev/ttyACM", "COM"))
    ]
    if not candidates:
        return None
    candidates.sort(key=_score)
    return candidates[0].device


def build_state_payload(
    *,
    status: str,
    qr_token: str,
    settings_payload: dict[str, Any],
    base_url: str | None = None,
    display_ip: str | None = None,
) -> dict[str, Any]:
    """M5StickC へ送信する状態同期 JSON ペイロードを組み立てる。"""
    if base_url is None or display_ip is None:
        base_url, display_ip = build_base_url()

    all_presets = [
        *settings_payload.get("builtin_presets", []),
        *settings_payload.get("custom_presets", []),
    ]
    button_preset_id = str(settings_payload.get("button_preset_id") or "")

    compact_presets: list[dict[str, str]] = []
    for p in all_presets[:MAX_PRESETS_IN_PAYLOAD]:
        compact_presets.append(
            {
                "id": str(p.get("id", "")),
                "name": str(p.get("name", ""))[:24],
            }
        )

    # 選択中プリセットが上限外だった場合は末尾に差し替える
    if button_preset_id and not any(p["id"] == button_preset_id for p in compact_presets):
        matched = next((p for p in all_presets if p.get("id") == button_preset_id), None)
        if matched:
            entry = {"id": str(matched["id"]), "name": str(matched["name"])[:24]}
            if compact_presets:
                compact_presets[-1] = entry
            else:
                compact_presets.append(entry)

    p_idx = 0
    p_name = ""
    for idx, item in enumerate(compact_presets):
        if item["id"] == button_preset_id:
            p_idx = idx
            p_name = item["name"]
            break
    if not p_name and compact_presets:
        p_name = compact_presets[0]["name"]
        button_preset_id = compact_presets[0]["id"]

    return {
        "t": "state",
        "st": status,
        "url": f"{base_url}/a/{qr_token}" if qr_token else base_url,
        "ip": display_ip,
        "p_id": button_preset_id,
        "p_name": p_name,
        "p_idx": p_idx,
        "p_cnt": len(compact_presets),
        "presets": compact_presets,
    }


class M5StickBridge:
    """M5StickC と USB シリアル（JSON Lines）で双方向通信するバックグラウンドブリッジ。"""

    def __init__(
        self,
        *,
        get_state_fn: Callable[[], dict[str, Any]],
        on_refresh_qr: Callable[[], None],
        on_select_preset: Callable[[str], tuple[bool, str]],
        on_poweroff: Callable[[], tuple[bool, str]],
        baudrate: int = DEFAULT_BAUDRATE,
    ) -> None:
        self._get_state_fn = get_state_fn
        self._on_refresh_qr = on_refresh_qr
        self._on_select_preset = on_select_preset
        self._on_poweroff = on_poweroff
        self._baudrate = baudrate

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._write_lock = threading.Lock()
        self._ser: Any = None
        self._thread: threading.Thread | None = None
        self._connected_port: str | None = None
        self._last_error: str | None = None

    @property
    def connected_port(self) -> str | None:
        return self._connected_port

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        if os.environ.get("M5STICK_ENABLED", "1").strip().lower() in {"0", "false", "off"}:
            logger.warning("M5StickC ブリッジは環境変数により無効化されています。")
            return
        if serial is None:
            self._last_error = "pyserial が未導入です (uv sync を実行してください)"
            logger.warning("M5StickC ブリッジを起動できません: %s", self._last_error)
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="m5stick-serial-bridge",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, shutdown_reason: str = "restart") -> None:
        """サーバー終了時にシャットダウン画面通知を送ってからクローズする。"""
        self.send_shutdown(reason=shutdown_reason)
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._close_serial()

    def notify_state_changed(self) -> None:
        """サーバー状態やトークン・プリセットが変わったときに即座に再送する。"""
        self._wake_event.set()

    def send_shutdown(self, *, reason: str = "poweroff") -> bool:
        return self._send_json({"t": "shutdown", "reason": reason})

    def _open_serial(self, port: str) -> Any:
        # ESP32 の自動リセットを避けるため、open 前に DTR/RTS を False にしておく
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = self._baudrate
        ser.timeout = 0.2
        ser.write_timeout = 1.0
        ser.dtr = False
        ser.rts = False
        ser.open()
        return ser

    def _close_serial(self) -> None:
        with self._write_lock:
            ser = self._ser
            self._ser = None
            self._connected_port = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _send_json(self, payload: dict[str, Any]) -> bool:
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with self._write_lock:
            ser = self._ser
            if ser is None:
                return False
            try:
                ser.write(line)
                ser.flush()
                return True
            except Exception as exc:
                logger.warning("M5StickC へのシリアル送信に失敗しました: %s", exc)
                return False

    def handle_incoming_line(self, raw_line: str) -> dict[str, Any] | None:
        """受信した 1 行の JSON コマンドを処理する（ユニットテストからも直接呼び出し可能）。"""
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None

        cmd = str(msg.get("cmd") or "").strip()
        if cmd == "hello":
            state_payload = self._get_state_fn()
            self._send_json(state_payload)
            return state_payload

        if cmd == "refresh_qr":
            self._on_refresh_qr()
            state_payload = self._get_state_fn()
            self._send_json(state_payload)
            return state_payload

        if cmd == "select_preset":
            preset_id = str(msg.get("id") or "").strip()
            ok, detail = self._on_select_preset(preset_id)
            notice = {"t": "notice", "ok": ok, "msg": detail}
            self._send_json(notice)
            state_payload = self._get_state_fn()
            self._send_json(state_payload)
            return notice

        if cmd == "poweroff":
            ok, detail = self._on_poweroff()
            if ok:
                payload = {"t": "shutdown", "reason": "poweroff"}
                self._send_json(payload)
                return payload
            notice = {"t": "notice", "ok": False, "msg": detail}
            self._send_json(notice)
            return notice

        return None

    def _run_loop(self) -> None:
        last_sent = 0.0
        last_logged_err: str | None = None
        while not self._stop_event.is_set():
            if self._ser is None:
                port = find_m5stick_port()
                if not port:
                    err = "M5StickC のシリアルポート (/dev/ttyUSB* 等) が見つかりません"
                    self._last_error = err
                    if err != last_logged_err:
                        logger.warning("%s", err)
                        last_logged_err = err
                    self._stop_event.wait(RECONNECT_INTERVAL_SEC)
                    continue
                try:
                    ser = self._open_serial(port)
                except Exception as exc:
                    err = f"M5StickC シリアル接続失敗 ({port}): {exc}"
                    self._last_error = err
                    if err != last_logged_err:
                        logger.warning("%s", err)
                        last_logged_err = err
                    self._stop_event.wait(RECONNECT_INTERVAL_SEC)
                    continue

                with self._write_lock:
                    self._ser = ser
                    self._connected_port = port
                self._last_error = None
                last_logged_err = None
                logger.warning("M5StickC に接続しました: %s", port)
                last_sent = 0.0
                self._wake_event.set()

            try:
                now = time.monotonic()
                if self._wake_event.is_set() or (now - last_sent) >= HEARTBEAT_INTERVAL_SEC:
                    self._wake_event.clear()
                    if not self._send_json(self._get_state_fn()):
                        raise OSError("serial write failed")
                    last_sent = now

                ser = self._ser
                if ser is None:
                    continue
                raw = ser.readline()
                if raw:
                    decoded = raw.decode("utf-8", errors="ignore")
                    self.handle_incoming_line(decoded)
            except Exception as exc:
                logger.warning("M5StickC シリアル接続が切断されました: %s", exc)
                self._close_serial()
                self._stop_event.wait(RECONNECT_INTERVAL_SEC)
