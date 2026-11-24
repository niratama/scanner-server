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
import scan_pipeline
import settings_store


class TestSettingsStore(unittest.TestCase):
    def test_defaults_and_last_used_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            payload = settings_store.get_settings_payload(path)
            self.assertEqual(payload["last_used"]["resolution"], 300)
            self.assertEqual(payload["last_used"]["mode"], "Color")
            self.assertGreaterEqual(len(payload["builtin_presets"]), 5)

            saved = settings_store.save_last_used(
                {"resolution": 600, "mode": "Gray", "subfolder": "領収書"},
                path=path,
            )
            self.assertEqual(saved["resolution"], 600)
            self.assertEqual(saved["mode"], "Gray")
            self.assertEqual(saved["subfolder"], "領収書")
            self.assertTrue(saved["swcrop"])

            reloaded = settings_store.get_settings_payload(path)
            self.assertEqual(reloaded["last_used"]["resolution"], 600)
            self.assertEqual(reloaded["last_used"]["subfolder"], "領収書")

    def test_custom_preset_crud(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            created = settings_store.save_custom_preset(
                name="確定申告",
                description="領収書用",
                settings={"resolution": 200, "duplex": False, "subfolder": "税務"},
                path=path,
            )
            self.assertFalse(created["builtin"])
            self.assertEqual(created["name"], "確定申告")
            self.assertEqual(created["settings"]["subfolder"], "税務")

            payload = settings_store.get_settings_payload(path)
            self.assertEqual(len(payload["custom_presets"]), 1)

            self.assertTrue(settings_store.delete_custom_preset(created["id"], path=path))
            self.assertFalse(settings_store.delete_custom_preset(created["id"], path=path))


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
        self.assertIn("--swdeskew=yes", cmd)
        self.assertIn("--rollerdeskew=yes", cmd)
        self.assertIn("--swcrop=yes", cmd)
        self.assertIn("--df-length=yes", cmd)
        self.assertIn("--stapledetect=no", cmd)
        self.assertIn("--buffermode=yes", cmd)
        self.assertIn("--format=jpeg", cmd)

    def test_build_scanimage_cmd_lineart_simplex(self):
        cmd = scan_pipeline.build_scanimage_cmd(
            batch_pattern="/tmp/page_%04d.png",
            resolution=150,
            mode="Lineart",
            duplex=False,
            swdeskew=False,
            rollerdeskew=False,
            swcrop=False,
            df_length=False,
            stapledetect=True,
            buffermode=False,
        )
        self.assertIn("--source=ADF Front", cmd)
        self.assertIn("--mode=Lineart", cmd)
        self.assertIn("--resolution=150", cmd)
        self.assertIn("--format=png", cmd)
        self.assertIn("--stapledetect=yes", cmd)
        self.assertIn("--df-length=no", cmd)
        self.assertIn("--buffermode=no", cmd)

    def test_sanitize_filename(self):
        self.assertEqual(scan_pipeline.sanitize_filename("領収書:2026/11"), "領収書_2026_11.pdf")
        self.assertEqual(scan_pipeline.sanitize_filename("doc.PDF"), "doc.PDF")
        self.assertTrue(scan_pipeline.sanitize_filename("   ").startswith("scan_"))


if __name__ == "__main__":
    unittest.main()
