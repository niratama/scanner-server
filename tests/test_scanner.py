import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_stub_modules():
    """開発環境に未導入の外部ライブラリがある場合、ロジック検証用にスタブを登録する。"""
    if "googleapiclient" not in sys.modules:
        google = types.ModuleType("google")
        auth = types.ModuleType("google.auth")
        exceptions = types.ModuleType("google.auth.exceptions")
        exceptions.RefreshError = type("RefreshError", (Exception,), {})
        transport = types.ModuleType("google.auth.transport")
        requests_mod = types.ModuleType("google.auth.transport.requests")
        requests_mod.Request = MagicMock()
        oauth2 = types.ModuleType("google.oauth2")
        credentials = types.ModuleType("google.oauth2.credentials")
        credentials.Credentials = MagicMock()

        gapi = types.ModuleType("googleapiclient")
        discovery = types.ModuleType("googleapiclient.discovery")
        discovery.build = MagicMock()
        http_mod = types.ModuleType("googleapiclient.http")
        http_mod.MediaFileUpload = MagicMock()

        sys.modules.update(
            {
                "google": google,
                "google.auth": auth,
                "google.auth.exceptions": exceptions,
                "google.auth.transport": transport,
                "google.auth.transport.requests": requests_mod,
                "google.oauth2": oauth2,
                "google.oauth2.credentials": credentials,
                "googleapiclient": gapi,
                "googleapiclient.discovery": discovery,
                "googleapiclient.http": http_mod,
            }
        )

    if "img2pdf" not in sys.modules:
        img2pdf_mod = types.ModuleType("img2pdf")
        img2pdf_mod.convert = MagicMock(return_value=b"%PDF-1.4\n")
        sys.modules["img2pdf"] = img2pdf_mod

    if "PIL" not in sys.modules:
        pil = types.ModuleType("PIL")
        image_mod = types.ModuleType("PIL.Image")
        image_mod.Transpose = types.SimpleNamespace(
            ROTATE_90="ROTATE_90",
            ROTATE_180="ROTATE_180",
            ROTATE_270="ROTATE_270",
        )
        image_mod.open = MagicMock()
        enhance_mod = types.ModuleType("PIL.ImageEnhance")
        enhance_mod.Brightness = MagicMock()
        enhance_mod.Contrast = MagicMock()
        pil.Image = image_mod
        pil.ImageEnhance = enhance_mod
        sys.modules["PIL"] = pil
        sys.modules["PIL.Image"] = image_mod
        sys.modules["PIL.ImageEnhance"] = enhance_mod


_install_stub_modules()

import auth_store
import drive_client
import history_store
import m5stick_bridge
import scan_pipeline
import settings_store


class TestSettingsStore(unittest.TestCase):
    def test_defaults_and_high_res_duplex_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            payload = settings_store.get_settings_payload(path)
            self.assertEqual(payload["last_used"]["resolution"], 300)
            self.assertEqual(payload["last_used"]["output_format"], "pdf")
            self.assertEqual(payload["button_preset_id"], "standard_doc")

            preset_map = {p["id"]: p for p in payload["builtin_presets"]}
            self.assertIn("high_res_duplex", preset_map)
            high_res = preset_map["high_res_duplex"]["settings"]
            self.assertEqual(high_res["resolution"], 600)
            self.assertEqual(high_res["mode"], "Color")
            self.assertTrue(high_res["duplex"])

            saved = settings_store.save_last_used(
                {"resolution": 600, "mode": "Gray", "output_format": "png", "subfolder": "領収書"},
                path=path,
            )
            self.assertEqual(saved["resolution"], 600)
            self.assertEqual(saved["output_format"], "png")
            self.assertEqual(saved["subfolder"], "領収書")

            reloaded = settings_store.get_settings_payload(path)
            self.assertEqual(reloaded["last_used"]["output_format"], "png")

    def test_custom_preset_and_button_preset_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            created = settings_store.save_custom_preset(
                name="確定申告",
                description="領収書用",
                settings={"resolution": 200, "duplex": False, "output_format": "jpeg", "subfolder": "税務"},
                path=path,
            )
            selected = settings_store.set_button_preset_id(created["id"], path=path)
            self.assertEqual(selected["id"], created["id"])

            resolved = settings_store.resolve_preset_by_id(path=path)
            self.assertEqual(resolved["name"], "確定申告")
            self.assertEqual(resolved["settings"]["output_format"], "jpeg")

            # カスタムプリセット削除時は標準プリセットへフォールバックする
            self.assertTrue(settings_store.delete_custom_preset(created["id"], path=path))
            payload = settings_store.get_settings_payload(path)
            self.assertEqual(payload["button_preset_id"], "standard_doc")

    def test_custom_preset_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            for i in range(settings_store.MAX_CUSTOM_PRESETS):
                settings_store.save_custom_preset(
                    name=f"preset_{i}",
                    settings={"resolution": 300},
                    path=path,
                )
            with self.assertRaises(ValueError):
                settings_store.save_custom_preset(
                    name="overflow",
                    settings={"resolution": 300},
                    path=path,
                )
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


class TestHistoryStore(unittest.TestCase):
    def test_append_load_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            self.assertEqual(history_store.load_history(path=path), [])

            history_store.append_history(
                {"name": "scan_1.pdf", "output_format": "pdf", "trigger": "web"},
                path=path,
            )
            history_store.append_history(
                {"name": "scan_2.png", "output_format": "png", "trigger": "button"},
                path=path,
            )

            items = history_store.load_history(limit=10, path=path)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["name"], "scan_2.png")
            self.assertEqual(items[0]["trigger"], "button")
            self.assertIn("id", items[0])

            history_store.clear_history(path=path)
            self.assertEqual(history_store.load_history(path=path), [])


class TestDriveClient(unittest.TestCase):
    def test_normalize_subfolder_name(self):
        self.assertEqual(drive_client.normalize_subfolder_name(" /領収書/2026/ "), "領収書/2026")
        self.assertEqual(drive_client.normalize_subfolder_name("  "), "")

    def test_normalize_subfolder_name_validation(self):
        with self.assertRaises(ValueError):
            drive_client.normalize_subfolder_name("a/b/c/d/e/f")
        with self.assertRaises(ValueError):
            drive_client.normalize_subfolder_name("../secret")
        with self.assertRaises(ValueError):
            drive_client.normalize_subfolder_name("valid/./sub")
        with self.assertRaises(ValueError):
            drive_client.normalize_subfolder_name("bad\x00name")
        with self.assertRaises(ValueError):
            drive_client.normalize_subfolder_name("a" * (drive_client.MAX_SUBFOLDER_SEGMENT_LENGTH + 1))

    def test_atomic_write_secret_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token.json"
            drive_client._atomic_write_secret(token_path, '{"token": "dummy"}')
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"token": "dummy"}')
            self.assertEqual(stat.S_IMODE(os.stat(token_path).st_mode), 0o600)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_ensure_subfolder_nested(self):
        svc = MagicMock()
        files_api = svc.files.return_value
        files_api.list.return_value.execute.side_effect = [
            {"files": [{"id": "root_id", "name": "DR-C125_Scans"}]},
            {"files": []},
            {"files": []},
        ]
        files_api.create.return_value.execute.side_effect = [
            {"id": "year_id"},
            {"id": "receipt_id"},
        ]

        folder_id, display_path = drive_client.ensure_subfolder(svc, "2026/領収書")
        self.assertEqual(folder_id, "receipt_id")
        self.assertEqual(display_path, "DR-C125_Scans/2026/領収書")


class TestScanPipeline(unittest.TestCase):
    def test_build_scanimage_cmd_defaults(self):
        cmd = scan_pipeline.build_scanimage_cmd(batch_pattern="/tmp/page_%04d.jpg")
        self.assertEqual(cmd[0], "scanimage")
        self.assertNotIn("-d", cmd)
        self.assertIn("--page-width=215.872", cmd)
        self.assertIn("--page-height=297", cmd)
        self.assertIn("--source=ADF Duplex", cmd)
        self.assertIn("--format=jpeg", cmd)

    def test_build_scanimage_cmd_png_and_lineart(self):
        cmd_png = scan_pipeline.build_scanimage_cmd(
            batch_pattern="/tmp/page_%04d.png",
            resolution=600,
            mode="Color",
            output_format="png",
        )
        self.assertIn("--format=png", cmd_png)

        cmd_lineart = scan_pipeline.build_scanimage_cmd(
            batch_pattern="/tmp/page_%04d.png",
            resolution=150,
            mode="Lineart",
            duplex=False,
            output_format="pdf",
            stapledetect=True,
        )
        self.assertIn("--source=ADF Front", cmd_lineart)
        self.assertIn("--format=png", cmd_lineart)
        self.assertIn("--stapledetect=yes", cmd_lineart)

    def test_sanitize_filename_formats(self):
        self.assertEqual(scan_pipeline.sanitize_filename("領収書:2026/11", "pdf"), "領収書_2026_11.pdf")
        self.assertEqual(scan_pipeline.sanitize_filename("photo.jpg", "png"), "photo.png")
        self.assertEqual(scan_pipeline.sanitize_filename("doc.PDF", "jpeg"), "doc.jpg")
        self.assertTrue(scan_pipeline.sanitize_filename("   ", "png").endswith(".png"))

    def test_sanitize_base_name_security(self):
        self.assertTrue(scan_pipeline.sanitize_base_name("..").startswith("scan_"))
        self.assertTrue(scan_pipeline.sanitize_base_name(".").startswith("scan_"))
        self.assertEqual(scan_pipeline.sanitize_base_name("a\x00b\r\nc"), "abc")
        long_name = scan_pipeline.sanitize_base_name("x" * 200)
        self.assertEqual(len(long_name), scan_pipeline.MAX_BASE_NAME_LENGTH)

    def test_run_scan_job_timeout_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_root = Path(tmp)
            with (
                patch.object(scan_pipeline, "JOBS_ROOT", jobs_root),
                patch.object(
                    scan_pipeline.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(cmd=["scanimage"], timeout=600),
                ),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    scan_pipeline.run_scan_job(skip_blank=False)
                self.assertIn("タイムアウト", str(ctx.exception))
            self.assertEqual(list(jobs_root.iterdir()), [])

    def test_run_scan_job_streams_pdf_and_isolates_job_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_root = Path(tmp)
            observed_modes: list[int] = []

            def fake_scan(cmd, **kwargs):
                batch_arg = next(a for a in cmd if a.startswith("--batch="))
                batch_path = Path(batch_arg.split("=", 1)[1])
                observed_modes.append(stat.S_IMODE(os.stat(batch_path.parent).st_mode))
                (batch_path.parent / "page_0001.jpg").write_bytes(b"\xff\xd8\xff\xd9")
                return MagicMock(returncode=0, stdout="", stderr="")

            def fake_img2pdf(paths, outputstream=None):
                self.assertIsNotNone(outputstream)
                outputstream.write(b"%PDF-1.4\n")

            with (
                patch.object(scan_pipeline, "JOBS_ROOT", jobs_root),
                patch.object(scan_pipeline.subprocess, "run", side_effect=fake_scan),
                patch.object(scan_pipeline.img2pdf, "convert", side_effect=fake_img2pdf),
                patch.object(
                    scan_pipeline,
                    "upload_pdf",
                    return_value={
                        "name": "test.pdf",
                        "webViewLink": "https://drive.google.com/file/d/123/view",
                        "folder": "DR-C125_Scans",
                    },
                ),
            ):
                result = scan_pipeline.run_scan_job(
                    skip_blank=False,
                    filename="test",
                )
            self.assertEqual(observed_modes, [0o700])
            self.assertEqual(result["name"], "test.pdf")
            self.assertEqual(list(jobs_root.iterdir()), [])


class TestAuthStore(unittest.TestCase):
    def test_qr_token_onetime_and_session_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_path = Path(tmp) / "auth_sessions.json"
            clock = [1_700_000_000.0]
            mgr = auth_store.AuthManager(sessions_path, now_fn=lambda: clock[0])

            qr1, _ = mgr.get_qr_token()
            self.assertEqual(len(qr1), 12)

            # ワンタイム消費成功 -> セッション発行 & QR トークン自動更新
            session = mgr.consume_qr_token(qr1)
            self.assertIsNotNone(session)
            self.assertEqual(session["max_age"], auth_store.SESSION_TTL_SECONDS)
            self.assertEqual(stat.S_IMODE(os.stat(sessions_path).st_mode), 0o600)

            # 同じ QR トークンは再利用不可
            self.assertIsNone(mgr.consume_qr_token(qr1))
            qr2, _ = mgr.get_qr_token()
            self.assertNotEqual(qr1, qr2)

            # 別インスタンスでも 30 日セッションを復元できる（再起動耐性）
            mgr_reloaded = auth_store.AuthManager(sessions_path, now_fn=lambda: clock[0])
            info = mgr_reloaded.verify_session(session["token"])
            self.assertIsNotNone(info)
            self.assertEqual(info["remaining_seconds"], auth_store.SESSION_TTL_SECONDS)

            # 20 日経過後にスマホからセッション延長 -> トークン更新 & 残り 30 日に延長
            clock[0] += 20 * 24 * 60 * 60
            refreshed = mgr_reloaded.refresh_session(session["token"])
            self.assertIsNotNone(refreshed)
            self.assertNotEqual(refreshed["token"], session["token"])
            self.assertIsNone(mgr_reloaded.verify_session(session["token"]))
            self.assertEqual(refreshed["remaining_seconds"], auth_store.SESSION_TTL_SECONDS)

            # さらに 25 日経過しても延長済みなので有効
            clock[0] += 25 * 24 * 60 * 60
            self.assertIsNotNone(mgr_reloaded.verify_session(refreshed["token"]))

            # ログアウトで即時無効化
            self.assertTrue(mgr_reloaded.revoke_session(refreshed["token"]))
            self.assertIsNone(mgr_reloaded.verify_session(refreshed["token"]))

    def test_qr_token_expiry_and_session_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_path = Path(tmp) / "auth_sessions.json"
            clock = [1_700_000_000.0]
            mgr = auth_store.AuthManager(
                sessions_path,
                session_ttl_seconds=100,
                qr_token_ttl_seconds=10,
                now_fn=lambda: clock[0],
            )
            qr1, _ = mgr.get_qr_token()
            clock[0] += 11
            # 期限切れ QR は拒否される
            self.assertIsNone(mgr.consume_qr_token(qr1))

            qr2, _ = mgr.get_qr_token()
            self.assertNotEqual(qr1, qr2)
            session = mgr.consume_qr_token(qr2)
            self.assertIsNotNone(session)

            clock[0] += 101
            self.assertIsNone(mgr.verify_session(session["token"]))


class TestM5StickBridge(unittest.TestCase):
    def test_build_state_payload_and_command_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            payload = m5stick_bridge.build_state_payload(
                status="idle",
                qr_token="AbCdEf123456",
                settings_payload=settings_store.get_settings_payload(settings_path),
                base_url="http://192.168.1.50:8000",
                display_ip="192.168.1.50",
            )
            self.assertEqual(payload["t"], "state")
            self.assertEqual(payload["url"], "http://192.168.1.50:8000/a/AbCdEf123456")
            self.assertEqual(payload["p_id"], "standard_doc")
            self.assertGreaterEqual(payload["p_cnt"], 6)

            sent_lines: list[bytes] = []

            class FakeSerial:
                def write(self, data: bytes) -> int:
                    sent_lines.append(data)
                    return len(data)

                def flush(self) -> None:
                    pass

                def close(self) -> None:
                    pass

            refreshed_calls = []
            poweroff_calls = []
            bridge = m5stick_bridge.M5StickBridge(
                get_state_fn=lambda: m5stick_bridge.build_state_payload(
                    status="idle",
                    qr_token="Tok123456789",
                    settings_payload=settings_store.get_settings_payload(settings_path),
                    base_url="http://192.168.1.50:8000",
                    display_ip="192.168.1.50",
                ),
                on_refresh_qr=lambda: refreshed_calls.append(True),
                on_select_preset=lambda pid: (
                    True,
                    settings_store.set_button_preset_id(pid, path=settings_path)["name"],
                ),
                on_poweroff=lambda: (poweroff_calls.append(True) or True, "ok"),
            )
            bridge._ser = FakeSerial()

            bridge.handle_incoming_line('{"cmd":"hello"}\n')
            bridge.handle_incoming_line('{"cmd":"refresh_qr"}\n')
            bridge.handle_incoming_line('{"cmd":"select_preset","id":"receipt"}\n')
            bridge.handle_incoming_line('{"cmd":"poweroff"}\n')

            self.assertEqual(len(refreshed_calls), 1)
            self.assertEqual(len(poweroff_calls), 1)
            self.assertEqual(
                settings_store.get_settings_payload(settings_path)["button_preset_id"],
                "receipt",
            )
            decoded = [line.decode("utf-8").strip() for line in sent_lines]
            self.assertTrue(any('"t":"shutdown","reason":"poweroff"' in d for d in decoded))


if __name__ == "__main__":
    unittest.main()
