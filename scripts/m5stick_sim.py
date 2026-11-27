#!/usr/bin/env python3
"""WSL / 開発環境向け M5StickC シリアル接続シミュレーター＆テストツール。

DG-CANPC 実機やスキャナー本体なしで、M5StickC の画面遷移・QR 表示・
プロファイル切替・シャットダウン画面・ハートビート断などを対話的に検証できます。

使用例:
  uv run python scripts/m5stick_sim.py
  uv run python scripts/m5stick_sim.py --port /dev/ttyUSB0 --ip 192.168.100.123
"""

import argparse
import json
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("エラー: pyserial が見つかりません。`uv sync` を実行してください。", file=sys.stderr)
    sys.exit(1)

from auth_store import QR_TOKEN_BYTES
from m5stick_bridge import DEFAULT_BAUDRATE, build_state_payload, find_m5stick_port
from settings_store import BUILTIN_PRESETS


def _list_available_ports() -> list[str]:
    return [f"{p.device} ({p.description})" for p in list_ports.comports()]


class M5StickSimulator:
    def __init__(self, port: str, baudrate: int, ip: str, http_port: int) -> None:
        self.port = port
        self.baudrate = baudrate
        self.ip = ip
        self.http_port = http_port
        self.status = "idle"
        self.qr_token = secrets.token_urlsafe(QR_TOKEN_BYTES)
        self.button_preset_id = BUILTIN_PRESETS[0]["id"]
        self.presets = [dict(p) for p in BUILTIN_PRESETS]
        self.heartbeat_enabled = True

        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._ser: serial.Serial | None = None

    def _base_url(self) -> str:
        return f"http://{self.ip}:{self.http_port}"

    def build_state(self) -> dict[str, Any]:
        settings_payload = {
            "builtin_presets": self.presets,
            "custom_presets": [],
            "button_preset_id": self.button_preset_id,
        }
        return build_state_payload(
            status=self.status,
            qr_token=self.qr_token,
            settings_payload=settings_payload,
            base_url=self._base_url(),
            display_ip=self.ip,
        )

    def send_json(self, payload: dict[str, Any], *, quiet: bool = False) -> None:
        ser = self._ser
        if ser is None:
            return
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            ser.write(line.encode("utf-8"))
            ser.flush()
        if not quiet:
            print(f"[TX] {line.strip()}")

    def open(self) -> None:
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = self.baudrate
        ser.timeout = 0.2
        ser.write_timeout = 1.0
        ser.dtr = False
        ser.rts = False
        ser.open()
        self._ser = ser
        print(f"接続しました: {self.port} ({self.baudrate} bps)")

    def close(self) -> None:
        self._stop_event.set()
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _handle_rx(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        print(f"\n[RX] {line}")
        if not line.startswith("{"):
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return

        cmd = str(msg.get("cmd") or "")
        if cmd == "hello":
            self.send_json(self.build_state())
        elif cmd == "refresh_qr":
            self.qr_token = secrets.token_urlsafe(QR_TOKEN_BYTES)
            self.send_json(self.build_state())
        elif cmd == "select_preset":
            pid = str(msg.get("id") or "")
            matched = next((p for p in self.presets if p["id"] == pid), None)
            if matched:
                self.button_preset_id = pid
                self.send_json({"t": "notice", "ok": True, "msg": matched["name"]})
                self.send_json(self.build_state())
            else:
                self.send_json({"t": "notice", "ok": False, "msg": "不明なID"})
        elif cmd == "poweroff":
            if self.status == "scanning":
                self.send_json({"t": "notice", "ok": False, "msg": "スキャン中"})
            else:
                self.status = "powering_off"
                self.heartbeat_enabled = False
                self.send_json({"t": "shutdown", "reason": "poweroff"})
                print("※ シミュレーターのため WSL 本体はシャットダウンしません（ハートビートを停止しました）。")

    def _io_loop(self) -> None:
        last_hb = 0.0
        while not self._stop_event.is_set():
            try:
                now = time.monotonic()
                if self.heartbeat_enabled and (now - last_hb) >= 3.0:
                    self.send_json(self.build_state(), quiet=True)
                    last_hb = now
                ser = self._ser
                if ser is None:
                    break
                raw = ser.readline()
                if raw:
                    self._handle_rx(raw.decode("utf-8", errors="ignore"))
            except Exception as exc:
                print(f"\n[ERROR] シリアル通信エラー: {exc}", file=sys.stderr)
                self._stop_event.set()
                break

    def run_interactive(self) -> None:
        self.open()
        io_thread = threading.Thread(target=self._io_loop, daemon=True)
        io_thread.start()

        self.send_json(self.build_state())
        self.print_help()

        try:
            while not self._stop_event.is_set():
                try:
                    raw_cmd = input("sim> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not raw_cmd:
                    continue

                parts = raw_cmd.split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""

                if cmd in {"quit", "exit", "q"}:
                    break
                elif cmd == "help":
                    self.print_help()
                elif cmd == "idle":
                    self.status = "idle"
                    self.heartbeat_enabled = True
                    self.send_json(self.build_state())
                elif cmd == "offline":
                    self.status = "offline"
                    self.heartbeat_enabled = True
                    self.send_json(self.build_state())
                elif cmd == "scan":
                    self.status = "scanning"
                    self.heartbeat_enabled = True
                    self.send_json(self.build_state())
                elif cmd == "qr":
                    self.qr_token = secrets.token_urlsafe(QR_TOKEN_BYTES)
                    self.send_json(self.build_state())
                elif cmd == "ip" and arg:
                    self.ip = arg
                    self.send_json(self.build_state())
                elif cmd == "preset" and arg:
                    if any(p["id"] == arg for p in self.presets):
                        self.button_preset_id = arg
                        self.send_json(self.build_state())
                    else:
                        ids = ", ".join(p["id"] for p in self.presets)
                        print(f"プリセットIDが見つかりません。候補: {ids}")
                elif cmd == "notice" and arg:
                    self.send_json({"t": "notice", "ok": True, "msg": arg})
                elif cmd == "shutdown":
                    self.status = "powering_off"
                    self.heartbeat_enabled = False
                    self.send_json({"t": "shutdown", "reason": "poweroff"})
                    print("ハートビートを停止しました（idle で再開）。")
                elif cmd == "restart":
                    self.heartbeat_enabled = False
                    self.send_json({"t": "shutdown", "reason": "restart"})
                    print("ハートビートを停止しました（idle でサーバー再起動を模擬できます）。")
                elif cmd == "hb":
                    if arg in {"on", "1"}:
                        self.heartbeat_enabled = True
                    elif arg in {"off", "0"}:
                        self.heartbeat_enabled = False
                    else:
                        self.heartbeat_enabled = not self.heartbeat_enabled
                    print(f"ハートビート: {'ON' if self.heartbeat_enabled else 'OFF'}")
                else:
                    print("不明なコマンドです。`help` を入力してください。")
        finally:
            self.close()
            print("終了しました。")

    @staticmethod
    def print_help() -> None:
        print(
            """
コマンド一覧:
  idle             待機中状態を送信
  offline          スキャナ電源OFF状態を送信
  scan             スキャン中状態を送信（電源オフのロック確認）
  qr               QR トークンを手動更新
  ip <IP>          表示 IP を変更（例: ip 192.168.100.123）
  preset <id>      選択中プロファイルを変更（例: preset receipt）
  notice <text>    通知バナーをテスト表示
  shutdown         シャットダウン中画面 (poweroff) を送信
  restart          サーバー停止画面 (restart) を送信（idle で復帰）
  hb [on|off]      3秒ハートビートの ON/OFF（OFF でタイムアウト確認）
  help / quit      ヘルプ表示 / 終了
"""
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="M5StickC シリアル接続シミュレーター")
    parser.add_argument("--port", help="シリアルポート（例: /dev/ttyUSB0）。省略時は自動検出")
    parser.add_argument("--baudrate", type=int, default=DEFAULT_BAUDRATE)
    parser.add_argument("--ip", default="192.168.100.123", help="テスト表示する IP アドレス")
    parser.add_argument("--http-port", type=int, default=8000, help="QR URL のポート番号")
    parser.add_argument("--list-ports", action="store_true", help="検出されたシリアルポート一覧を表示")
    args = parser.parse_args()

    if args.list_ports:
        ports = _list_available_ports()
        if not ports:
            print("シリアルポートが見つかりません（WSL では usbipd attach --wsl が必要です）。")
        else:
            for p in ports:
                print(p)
        return 0

    port = args.port or find_m5stick_port()
    if not port:
        print("エラー: M5StickC のシリアルポートが見つかりません。", file=sys.stderr)
        ports = _list_available_ports()
        if ports:
            print("検出済みポート:", file=sys.stderr)
            for p in ports:
                print(f"  - {p}", file=sys.stderr)
        else:
            print(
                "ヒント: Windows 側で `usbipd list` → `usbipd attach --wsl --busid <BUSID>` を実行して WSL に接続してください。",
                file=sys.stderr,
            )
        return 1

    sim = M5StickSimulator(port=port, baudrate=args.baudrate, ip=args.ip, http_port=args.http_port)
    sim.run_interactive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
