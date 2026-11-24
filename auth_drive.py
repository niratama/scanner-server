from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

# 既存フォルダへ直接アップロードする場合は "https://www.googleapis.com/auth/drive" に変更
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"

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
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    print(f"トークンを保存しました: {TOKEN_PATH}")

if __name__ == "__main__":
    main()