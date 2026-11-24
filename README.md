# DR-C125 ネットワークスキャナー（DG-CANPC + Canon DR-C125）

省電力ミニ PC（Diginnos DG-CANPC）をヘッドレス Ubuntu サーバー化し、USB 接続した **Canon DR-C125** をスマホのブラウザから操作して **両面スキャン → 白紙自動除去 → 画像調整 → PDF 化 → Google Drive 自動保存 → 安全シャットダウン** まで完結させる Web アプリケーションです。

---

## 主な機能

- **プリセット優先のスマホ UI**
  - ワンタップで切り替えられる組み込みプリセット 6 種類（`標準書類` / `高精細・両面` / `領収書` / `軽量メモ` / `白黒くっきり` / `高精細・写真`）を搭載。
  - 現在の設定に名前を付けて保存できる **カスタムプリセット**（作成・削除）と、**前回使用した設定の自動復元**（`settings.json` へ永続化）に対応。
  - 細かい設定は折りたたみの詳細設定セクションから必要なときだけ調整可能。
- **豊富なスキャン・画像調整オプション**
  - **解像度**: `150 / 200 / 300 / 400 / 600 dpi`
  - **カラーモード**: `カラー (Color)` / `グレースケール (Gray)` / `白黒2値 (Lineart)`
  - **保存形式**: `PDF（複数ページ結合）` / `JPEG（ページごとに画像保存）` / `PNG（無劣化・ページごとに画像保存）`
  - **両面 / 片面スキャン**: `ADF Duplex` / `ADF Front`
  - **白紙自動除去**: ON/OFF ＋ 感度 3 段階（`弱` / `標準` / `強`）
  - **スキャン補正・検知**: ソフトウェア傾き補正（`--swdeskew`）、機械的傾き補正（`--rollerdeskew`）、余白自動切り抜き（`--swcrop`）、重送検知（`--df-length`）、ホチキス検知（`--stapledetect`）、バッファモード（`--buffermode`）
  - **画像・PDF 調整**: ページ回転（`0° / 90° / 180° / 270°`）、JPEG 圧縮率（`そのまま(無劣化) / 95% / 85% / 75% / 60%`）、明るさ（`-50 〜 +50`）、コントラスト（`-50 〜 +50`）
- **サーバー側スキャン履歴の永続化**
  - スキャン結果（日時・ファイルリンク・保存先フォルダ・出力形式・ページ数・起動元など）をサーバー側の `history.json` に最大 100 件まで保存し、再起動後もブラウザから参照・クリア可能。
- **本体スタートボタン連動（プロファイル選択式）**
  - Web UI から本体ボタン押下時に実行するプリセット（組み込み／カスタムどちらも選択可）をいつでも変更可能。
  - `scanbd` から `scripts/button_trigger.sh` を呼び出すことで、スキャン完了まで USB 競合を防ぎながら自動実行可能。
- **Google Drive サブフォルダ管理**
  - `drive.file` スコープのまま、`DR-C125_Scans` 配下のサブフォルダ一覧取得・選択・新規作成（`2026/領収書` のような階層指定にも対応）が可能。
- **安全なリモートシャットダウン**
  - スキャン実行中は多重実行・シャットダウンをロックで防止し、完了後に確認ダイアログ経由で `systemctl poweroff` を実行可能。

---

## 環境・ハードウェア構成

| 項目 | 内容 |
|---|---|
| **ホスト機** | Diginnos DG-CANPC（Atom x7-Z8700 / 4GB RAM / 32GB eMMC / 64bit UEFI） |
| **OS** | Ubuntu (amd64, ヘッドレス運用 / `Asia/Tokyo`) |
| **スキャナー** | Canon DR-C125（USB ID: `1083:1640`, SANE バックエンド: `canon_dr`） |
| **作業ディレクトリ** | `~/scanner-app`（仮想環境: `.venv`） |
| **一時作業領域** | `/tmp/dr_c125_jobs`（`tmpfs` 1.5GB マウントで eMMC 書き込みを抑制） |

### DR-C125 実機仕様・制約

- **A4 サイズの明示指定が必須**: デフォルト高さが US レター（`279.364mm`）のため、A4 下端切れ防止として `--page-width=215.872 --page-height=297 -x 215.872 -y 297` を常時指定しています。
- **自動サイズ検知 (`--ald`) 非対応**: 最大サイズで読み込み、`--swcrop=yes` で余白を切り抜きます。
- **デバイス指定 (`-d`) 省略**: `/etc/sane.d/dll.conf` を `canon_dr` のみに限定済みのため、USB バス番号変動の影響を受けないよう `-d` は省略しています。
- **省メモリ設計**: 白紙判定は縮小グレースケールのヒストグラムで行い、画像調整も 1 ページずつ処理します。未加工時は元画像を再圧縮せず `img2pdf` で無劣化結合します（`Lineart` 時は PNG 出力）。

---

## ファイル構成

```text
.
├── app.py               # FastAPI サーバー & スマホ向け HTML UI
├── scan_pipeline.py     # scanimage 実行・白紙除去・画像調整・PDF 化・アップロード統合
├── drive_client.py      # Google Drive API クライアント（自動リフレッシュ・サブフォルダ管理・アップロード）
├── auth_drive.py        # 初回 OAuth 認証用スクリプト（token.json 生成）
├── settings_store.py    # 組み込み／カスタムプリセット・前回設定・本体ボタン設定の永続化
├── history_store.py     # サーバー側スキャン履歴（history.json）の永続化
├── pyproject.toml       # Python プロジェクト・依存パッケージ定義（uv 管理）
├── uv.lock              # 依存バージョンロックファイル
├── scripts/
│   ├── setup.sh              # 自動セットアップスクリプト（OS 最適化・SANE・uv・systemd・scanbd）
│   ├── button_trigger.sh     # scanbd から呼び出す本体ボタン連動スクリプト
│   └── scanbd_canon_dr.conf  # scanbd 用 Canon DR-C125 設定サンプル
├── tests/
│   └── test_scanner.py  # ユニットテスト
├── credentials.json     # （要配置・非公開）Google Cloud OAuth クライアント認証情報
├── token.json           # （自動生成・非公開）OAuth トークン
├── settings.json        # （自動生成）カスタムプリセット・前回設定・本体ボタンプロファイル
└── history.json         # （自動生成）スキャン履歴
```

---

## セットアップ手順

### 自動セットアップ（推奨）

`scripts/setup.sh` を実行すると、依存パッケージ導入・eMMC 保護（`tmpfs` / `journald` / `zram-tools`）・SANE 設定・`uv` による `.venv` 構築（`uv sync --frozen --no-cache`）・`sudoers`・`systemd` 常駐化・UFW 許可まで一括で設定できます（冪等実行に対応）。

```bash
# 基本構成（Web UI のみ）
sudo ./scripts/setup.sh

# 本体スタートボタン連動（scanbd + scanbm 共存構成）もまとめて設定する場合
sudo ./scripts/setup.sh --with-scanbd
```

主なオプション:
- `--with-scanbd`: `scanbd` 導入・`button_trigger.sh` 配置・`inetd` / `scanbm` による Web UI 共存構成まで自動化
- `--user <USER>`: サービス実行ユーザーを明示指定（既定: `sudo` 呼び出し元ユーザー）
- `--port <PORT>`: 待受ポートを指定（既定: `8000`）
- `--skip-os-tuning`: `tmpfs` / `journald` / `zram-tools` の設定をスキップ

セットアップ完了後、初回のみ Google Drive 認証を行います（下記「5. Google Drive API 初回認証」）。

---

### 手動セットアップ（参考）

#### 1. OS パッケージ導入とユーザーグループ設定

```bash
sudo apt-get update
sudo apt-get install -y sane-utils curl zram-tools
sudo usermod -aG scanner,plugdev <your-user>
```

#### 2. eMMC 保護・OS 最適化（`tmpfs` / `journald`）

```bash
sudo mkdir -p /tmp/dr_c125_jobs
sudo chmod 1777 /tmp/dr_c125_jobs
echo "tmpfs /tmp/dr_c125_jobs tmpfs defaults,noatime,size=1500M,mode=1777 0 0" | sudo tee -a /etc/fstab
sudo mount /tmp/dr_c125_jobs

sudo mkdir -p /etc/systemd/journald.conf.d
printf "[Journal]\nSystemMaxUse=50M\n" | sudo tee /etc/systemd/journald.conf.d/scanner-size.conf
sudo systemctl restart systemd-journald
```

#### 3. SANE 設定（Web UI のみ利用する場合）

不要なバックエンドを無効化し、`canon_dr` のみに絞ることで認識を高速化します（`scanbd` を使う場合は手順 8 で `net` 構成へ切り替えます）。

```bash
sudo cp -a /etc/sane.d/dll.conf /etc/sane.d/dll.conf.bak
echo "canon_dr" | sudo tee /etc/sane.d/dll.conf
if [ -f /etc/sane.d/dll.d/airscan ]; then
  sudo mv /etc/sane.d/dll.d/airscan /etc/sane.d/dll.d/airscan.disabled
fi
scanimage -L   # Canon DR-C125 が検出されることを確認
```

#### 4. Python 仮想環境と依存パッケージの導入（`uv`）

```bash
cd ~/scanner-app
curl -LsSf https://astral.sh/uv/install.sh | sh   # 未導入の場合
uv sync --frozen --no-cache
```

#### 5. Google Drive API 初回認証

Google Cloud Console で OAuth 同意画面を「本番環境（In Production）」へ公開し、クライアントシークレットを `credentials.json`（`chmod 600`）として配置します。

```bash
# SSH ポートフォワーディング例: ssh -L 8080:127.0.0.1:8080 <your-user>@<host>
uv run python auth_drive.py
uv run python drive_client.py   # 疎通確認（DR-C125_Scans フォルダの取得・作成）
```

#### 6. シャットダウン権限 (`sudoers`) の設定

`<your-user>` は実行ユーザー名に置き換えてください。

```bash
echo "<your-user> ALL=(ALL) NOPASSWD: /usr/bin/systemctl poweroff" | sudo tee /etc/sudoers.d/scanner-poweroff
sudo chmod 440 /etc/sudoers.d/scanner-poweroff
sudo visudo -c
```

#### 7. `systemd` 常駐化

`/etc/systemd/system/dr-c125-scanner.service` を作成します（`<your-user>` は実行ユーザー名に置き換えてください）。

```ini
[Unit]
Description=Canon DR-C125 Network Scanner Web Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<your-user>
Group=scanner
WorkingDirectory=/home/<your-user>/scanner-app
ExecStart=/home/<your-user>/scanner-app/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

サービスを有効化し、必要に応じてファイアウォールでポート `8000/tcp` を許可します。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dr-c125-scanner.service
sudo systemctl status dr-c125-scanner.service
# UFW が有効な場合
sudo ufw allow 8000/tcp
```

スマホから `http://<DG-CANPCのIP>:8000` へアクセスして利用します。

#### 8. 本体スタートボタン連動 (`scanbd` / 任意)

`scanbd` は `saned` ユーザーで動作し USB デバイスを常時監視するため、Web UI の `scanimage` と共存させるには `scanbd`（`canon_dr`）＋ `scanimage`（`net` → `localhost`）構成にします。

1. `scanbd` を導入し、トリガースクリプトと設定ファイルを配置します。
   ```bash
   sudo apt-get install -y scanbd
   sudo mkdir -p /etc/scanbd/scripts /etc/scanbd/scanner.d
   sudo install -m 755 ~/scanner-app/scripts/button_trigger.sh /etc/scanbd/scripts/button_trigger.sh
   sudo cp ~/scanner-app/scripts/scanbd_canon_dr.conf /etc/scanbd/scanner.d/canon_dr.conf
   ```
2. `/etc/scanbd/scanbd.conf` の末尾に `include` を追加します。
   ```bash
   echo 'include(scanner.d/canon_dr.conf)' | sudo tee -a /etc/scanbd/scanbd.conf
   ```
3. `scanbd` と SANE クライアントのバックエンドを分離します。
   ```bash
   echo "canon_dr" | sudo tee /etc/scanbd/dll.conf
   echo "net" | sudo tee /etc/sane.d/dll.conf
   grep -Eq '^[[:space:]]*localhost([[:space:]]|$)' /etc/sane.d/net.conf || echo "localhost" | sudo tee -a /etc/sane.d/net.conf
   ```
4. サービスを有効化・再起動します（Ubuntu の `scanbd` パッケージでは `openbsd-inetd` が `/etc/inetd.conf` の `sane-port` で 6566/tcp を待ち受けます）。
   ```bash
   sudo systemctl enable --now openbsd-inetd.service 2>/dev/null || true
   sudo systemctl enable --now scanbd.service
   sudo systemctl restart scanbd.service
   ```
5. Web UI の「本体スタートボタン連動」から、ボタン押下時に実行したいプリセットを選択します。
   - 動作確認ログ: `journalctl -t dr-c125-button -n 20 --no-pager`

---

## API エンドポイント

| メソッド | パス | 概要 |
|---|---|---|
| `GET` | `/` | スマホ向け Web UI を返却 |
| `POST` | `/api/scan` | スキャンジョブを開始（前回設定を自動保存） |
| `POST` | `/api/trigger/button` | 本体ボタン用に選択済みプリセットでスキャンを実行（`?wait=true` で完了待機） |
| `GET` | `/api/status` | 実行状態・進捗メッセージ・エラー・直近 20 件の履歴を取得 |
| `GET` | `/api/history` | サーバー保存済みのスキャン履歴を取得（`?limit=50`） |
| `DELETE` | `/api/history` | サーバー保存済みのスキャン履歴をクリア |
| `GET` | `/api/settings` | 既定値・前回設定・組み込み／カスタムプリセット・本体ボタン設定を取得 |
| `PUT` | `/api/button-preset` | 本体ボタン連動用プリセット ID を更新 |
| `POST` | `/api/presets` | カスタムプリセットを保存 |
| `DELETE` | `/api/presets/{preset_id}` | カスタムプリセットを削除 |
| `GET` | `/api/folders` | `DR-C125_Scans` 直下のサブフォルダ一覧を取得 |
| `POST` | `/api/folders` | `DR-C125_Scans` 配下にサブフォルダを作成 |
| `POST` | `/api/poweroff` | ホスト機を安全にシャットダウン |

---

## テストの実行

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

---

## 今後の拡張候補

- **AI 自動ファイル命名**: スキャン 1 ページ目の画像を Gemini API へ送信し、書類の日付・タイトルから PDF ファイル名を自動生成する機能。
