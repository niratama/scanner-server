import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
BASE_DIR = Path(__file__).resolve().parent
TOKEN_PATH = BASE_DIR / "token.json"
ROOT_FOLDER_NAME = "DR-C125_Scans"
MAX_SUBFOLDER_DEPTH = 5
MAX_SUBFOLDER_SEGMENT_LENGTH = 64

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN_REFRESH_LOCK = threading.Lock()


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


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def get_drive_service():
    """token.json を読み込み、期限切れなら自動リフレッシュして Drive クライアントを返す。"""
    with _TOKEN_REFRESH_LOCK:
        if not TOKEN_PATH.exists():
            raise FileNotFoundError(
                "token.json が見つかりません。先に auth_drive.py を実行してください。"
            )

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    raise RuntimeError(
                        "トークンの自動更新に失敗しました。Google 側で失効した可能性があります。"
                        "auth_drive.py を再実行してください。"
                    ) from e

                # 0600 権限の一時ファイルへ書き込み後、原子的に差し替える
                _atomic_write_secret(TOKEN_PATH, creds.to_json())
            else:
                raise RuntimeError(
                    "有効な refresh_token がありません。auth_drive.py を再実行してください。"
                )

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def ensure_folder(
    service,
    folder_name: str = ROOT_FOLDER_NAME,
    parent_id: Optional[str] = None,
) -> str:
    """drive.file スコープ用：アプリ専用の保存フォルダを取得または新規作成し ID を返す。"""
    escaped_name = _escape_drive_query_value(folder_name)
    query_parts = [
        f"name = '{escaped_name}'",
        "mimeType = 'application/vnd.google-apps.folder'",
        "trashed = false",
    ]
    if parent_id:
        escaped_parent = _escape_drive_query_value(parent_id)
        query_parts.append(f"'{escaped_parent}' in parents")

    query = " and ".join(query_parts)
    res = service.files().list(q=query, fields="files(id, name)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    metadata: dict = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    created = service.files().create(body=metadata, fields="id").execute()
    return created["id"]


def normalize_subfolder_name(subfolder: str) -> str:
    """サブフォルダ名を正規化し、不正な文字・階層数・セグメント長を検証する。"""
    if _CONTROL_CHARS_RE.search(subfolder):
        raise ValueError("フォルダ名に使用できない制御文字が含まれています。")
    parts = [part.strip() for part in subfolder.replace("\\", "/").split("/") if part.strip()]
    if len(parts) > MAX_SUBFOLDER_DEPTH:
        raise ValueError(f"フォルダの階層は最大 {MAX_SUBFOLDER_DEPTH} 階層までです。")
    for part in parts:
        if part in (".", ".."):
            raise ValueError("フォルダ名に '.' や '..' は使用できません。")
        if len(part) > MAX_SUBFOLDER_SEGMENT_LENGTH:
            raise ValueError(
                f"各フォルダ名は {MAX_SUBFOLDER_SEGMENT_LENGTH} 文字以内で入力してください。"
            )
    return "/".join(parts)


def ensure_subfolder(service, subfolder: str = "") -> tuple[str, str]:
    """ルートフォルダ (DR-C125_Scans) 配下のサブフォルダを取得または作成し (id, 表示パス) を返す。"""
    root_id = ensure_folder(service, ROOT_FOLDER_NAME)
    normalized = normalize_subfolder_name(subfolder)
    if not normalized:
        return root_id, ROOT_FOLDER_NAME

    current_id = root_id
    for part in normalized.split("/"):
        current_id = ensure_folder(service, part, parent_id=current_id)
    return current_id, f"{ROOT_FOLDER_NAME}/{normalized}"


def list_subfolders(service=None) -> list[dict]:
    """DR-C125_Scans 直下のサブフォルダ一覧を返す。"""
    svc = service or get_drive_service()
    root_id = ensure_folder(svc, ROOT_FOLDER_NAME)
    escaped_root = _escape_drive_query_value(root_id)
    query = (
        f"'{escaped_root}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    res = (
        svc.files()
        .list(q=query, fields="files(id, name)", orderBy="name", pageSize=100)
        .execute()
    )
    return res.get("files", [])


def create_subfolder(name: str, service=None) -> dict:
    """DR-C125_Scans 配下にサブフォルダを作成（既存なら取得）して返す。"""
    normalized = normalize_subfolder_name(name)
    if not normalized:
        raise ValueError("フォルダ名を入力してください。")
    svc = service or get_drive_service()
    folder_id, display_path = ensure_subfolder(svc, normalized)
    return {"id": folder_id, "name": normalized, "path": display_path}


def upload_file(
    file_path: Path,
    *,
    mimetype: str,
    upload_name: Optional[str] = None,
    folder_id: Optional[str] = None,
    subfolder: str = "",
    service=None,
) -> dict:
    """任意のファイルを Google Drive へアップロードし、ファイル ID・閲覧 URL・保存先フォルダ名を返す。"""
    svc = service or get_drive_service()
    if folder_id:
        target_folder_id = folder_id
        folder_label = normalize_subfolder_name(subfolder) or ROOT_FOLDER_NAME
    else:
        target_folder_id, folder_label = ensure_subfolder(svc, subfolder)

    metadata = {
        "name": upload_name or file_path.name,
        "parents": [target_folder_id],
    }
    media = MediaFileUpload(
        str(file_path),
        mimetype=mimetype,
        resumable=True,
    )
    created = (
        svc.files()
        .create(body=metadata, media_body=media, fields="id, name, webViewLink")
        .execute()
    )
    created["folder"] = folder_label
    return created


def upload_pdf(
    pdf_path: Path,
    upload_name: Optional[str] = None,
    folder_id: Optional[str] = None,
    subfolder: str = "",
    service=None,
) -> dict:
    """PDF を Google Drive へアップロードし、ファイル ID・閲覧 URL・保存先フォルダ名を返す。"""
    return upload_file(
        pdf_path,
        mimetype="application/pdf",
        upload_name=upload_name,
        folder_id=folder_id,
        subfolder=subfolder,
        service=service,
    )


if __name__ == "__main__":
    # 疎通確認：保存先フォルダを作成・取得して ID を表示
    svc = get_drive_service()
    fid = ensure_folder(svc)
    print(f"接続成功！保存先フォルダ ID: {fid}")