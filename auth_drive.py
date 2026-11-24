import os
import tempfile
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

# 既存フォルダへ直接アップロードする場合は "https://www.googleapis.com/auth/drive" に変更
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"


def _atomic_write_secret(path: Path, content: str) -> None:
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
            os.chmod(tmp_path, 0o600)
            f.write(content)
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


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_PATH), SCOPES
    )
    # SSH トンネル経由でローカル PC のブラウザからコールバックを受け取る
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=8080,
        open_browser=False,
        access_type="offline",
        prompt="consent",
    )
    _atomic_write_secret(TOKEN_PATH, creds.to_json())
    print(f"トークンを保存しました: {TOKEN_PATH}")

if __name__ == "__main__":
    main()