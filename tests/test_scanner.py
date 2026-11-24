import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

import drive_client
import history_store
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


if __name__ == "__main__":
    unittest.main()
