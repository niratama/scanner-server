#!/usr/bin/env bash
set -euo pipefail

# Canon DR-C125 ネットワークスキャナー 自動セットアップスクリプト（Ubuntu 向け・冪等実行対応）

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_USER="${SUDO_USER:-$(id -un)}"
PORT="8000"
WITH_SCANBD=0
SKIP_OS_TUNING=0

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/setup.sh [OPTIONS]

Options:
  --with-scanbd     scanbd + scanbm.socket を導入し、本体スタートボタン連動を設定する
  --user <USER>     サービス実行ユーザーを指定（既定: sudo 呼び出し元ユーザー）
  --port <PORT>     Web UI / API の待受ポート（既定: 8000）
  --skip-os-tuning  tmpfs / journald / zram の OS 最適化をスキップする
  -h, --help        このヘルプを表示
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-scanbd)
      WITH_SCANBD=1
      shift
      ;;
    --user)
      APP_USER="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --skip-os-tuning)
      SKIP_OS_TUNING=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "このスクリプトは root 権限（sudo）で実行してください。" >&2
  echo "例: sudo ./scripts/setup.sh" >&2
  exit 1
fi

if [[ -z "${APP_USER}" ]] || ! id "${APP_USER}" >/dev/null 2>&1; then
  echo "有効な実行ユーザーが見つかりません: ${APP_USER}" >&2
  exit 1
fi

if [[ ! "${PORT}" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "無効なポート番号です: ${PORT}" >&2
  exit 1
fi

log() {
  echo "==> $*"
}

backup_once() {
  local target="$1"
  if [[ -f "${target}" && ! -f "${target}.bak" ]]; then
    cp -a "${target}" "${target}.bak"
  fi
}

log "セットアップ開始 (APP_DIR=${APP_DIR}, APP_USER=${APP_USER}, PORT=${PORT}, WITH_SCANBD=${WITH_SCANBD})"

# 1. システムパッケージ導入
log "1/7 システムパッケージを導入しています..."
APT_PACKAGES=(sane-utils curl)
if [[ "${SKIP_OS_TUNING}" -eq 0 ]]; then
  APT_PACKAGES+=(zram-tools)
fi
if [[ "${WITH_SCANBD}" -eq 1 ]]; then
  APT_PACKAGES+=(scanbd)
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq "${APT_PACKAGES[@]}"

# 2. ユーザーグループ付与
log "2/7 ユーザー ${APP_USER} を scanner / plugdev グループへ追加しています..."
for grp in scanner plugdev; do
  if getent group "${grp}" >/dev/null 2>&1; then
    usermod -aG "${grp}" "${APP_USER}"
  fi
done

# 3. eMMC 保護・OS 最適化（tmpfs / journald）
if [[ "${SKIP_OS_TUNING}" -eq 0 ]]; then
  log "3/7 eMMC 保護設定（tmpfs / journald）を適用しています..."
  mkdir -p /tmp/dr_c125_jobs
  chmod 1777 /tmp/dr_c125_jobs
  FSTAB_LINE="tmpfs /tmp/dr_c125_jobs tmpfs defaults,noatime,size=1500M,mode=1777 0 0"
  if ! grep -Fq "/tmp/dr_c125_jobs" /etc/fstab; then
    echo "${FSTAB_LINE}" >> /etc/fstab
  fi
  if ! mountpoint -q /tmp/dr_c125_jobs; then
    mount /tmp/dr_c125_jobs
  fi

  mkdir -p /etc/systemd/journald.conf.d
  cat <<'EOF' > /etc/systemd/journald.conf.d/scanner-size.conf
[Journal]
SystemMaxUse=50M
EOF
  systemctl restart systemd-journald || true
else
  log "3/7 OS 最適化をスキップしました (--skip-os-tuning)"
fi

# 4. SANE / scanbd 設定
log "4/7 SANE 設定を構成しています..."
if [[ -f /etc/sane.d/dll.d/airscan ]]; then
  mv /etc/sane.d/dll.d/airscan /etc/sane.d/dll.d/airscan.disabled
fi

if [[ "${WITH_SCANBD}" -eq 1 ]]; then
  log "    scanbd + scanbm 共存構成を設定しています..."
  backup_once /etc/scanbd/dll.conf
  echo "canon_dr" > /etc/scanbd/dll.conf

  backup_once /etc/sane.d/dll.conf
  echo "net" > /etc/sane.d/dll.conf

  backup_once /etc/sane.d/net.conf
  if ! grep -Eq '^[[:space:]]*localhost([[:space:]]|$)' /etc/sane.d/net.conf; then
    echo "localhost" >> /etc/sane.d/net.conf
  fi

  mkdir -p /etc/scanbd/scripts /etc/scanbd/scanner.d
  install -m 755 "${APP_DIR}/scripts/button_trigger.sh" /etc/scanbd/scripts/button_trigger.sh
  cat <<EOF > /etc/scanbd/scanner.d/canon_dr.conf
device canon_dr {
    filter = "^canon_dr.*"
    desc = "Canon DR-C125"

    action start {
        filter = "^start$"
        numerical-trigger {
            from-value = 0
            to-value   = 1
        }
        desc   = "Start button scan"
        script = "/etc/scanbd/scripts/button_trigger.sh"
    }
}
EOF

  backup_once /etc/scanbd/scanbd.conf
  if ! grep -Fq "include(scanner.d/canon_dr.conf)" /etc/scanbd/scanbd.conf; then
    printf '\ninclude(scanner.d/canon_dr.conf)\n' >> /etc/scanbd/scanbd.conf
  fi
else
  backup_once /etc/sane.d/dll.conf
  echo "canon_dr" > /etc/sane.d/dll.conf
fi

# 5. uv 導入 & Python 仮想環境 (.venv) の同期
log "5/7 uv を使って Python 仮想環境 (.venv) を構築しています..."
APP_HOME="$(getent passwd "${APP_USER}" | cut -d: -f6)"
sudo -u "${APP_USER}" env HOME="${APP_HOME}" bash -euo pipefail -c "
  export PATH=\"${APP_HOME}/.local/bin:${APP_HOME}/.cargo/bin:\$PATH\"
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  cd '${APP_DIR}'
  if [[ -f uv.lock ]]; then
    uv sync --frozen --no-cache
  else
    uv sync --no-cache
  fi
"
chmod +x "${APP_DIR}/scripts/button_trigger.sh"

# 6. シャットダウン権限 (sudoers) 設定
log "6/7 シャットダウン用 sudoers ルールを設定しています..."
SUDOERS_TMP="$(mktemp)"
cat <<EOF > "${SUDOERS_TMP}"
${APP_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl poweroff
EOF
visudo -cf "${SUDOERS_TMP}"
install -m 440 -o root -g root "${SUDOERS_TMP}" /etc/sudoers.d/scanner-poweroff
rm -f "${SUDOERS_TMP}"

# 7. systemd サービス登録 & 起動
log "7/7 systemd サービスを登録・起動しています..."
cat <<EOF > /etc/systemd/system/dr-c125-scanner.service
[Unit]
Description=Canon DR-C125 Network Scanner Web Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=scanner
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dr-c125-scanner.service
systemctl restart dr-c125-scanner.service

if [[ "${WITH_SCANBD}" -eq 1 ]]; then
  SCANBM_BIN="$(command -v scanbm || echo /usr/sbin/scanbm)"
  if [[ ! -f /lib/systemd/system/scanbm.socket && ! -f /usr/lib/systemd/system/scanbm.socket ]]; then
    log "    scanbm.socket / scanbm@.service を作成しています..."
    cat <<'EOF' > /etc/systemd/system/scanbm.socket
[Unit]
Description=scanbd/scanbm incoming socket

[Socket]
ListenStream=6566
Accept=yes
MaxConnections=1

[Install]
WantedBy=sockets.target
EOF
    cat <<EOF > /etc/systemd/system/scanbm@.service
[Unit]
Description=Scanner Service (scanbm)
Requires=scanbm.socket

[Service]
ExecStart=${SCANBM_BIN}
User=saned
Group=scanner
StandardInput=socket
StandardOutput=socket
StandardError=journal
Environment=SANE_CONFIG_DIR=/etc/scanbd
EOF
    systemctl daemon-reload
  fi

  # saned.socket が 6566/tcp を掴んでいると scanbm.socket と競合するため無効化
  systemctl disable --now saned.socket >/dev/null 2>&1 || true
  systemctl enable --now scanbm.socket
  systemctl enable --now scanbd.service
  systemctl restart scanbd.service
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" >/dev/null
  log "UFW で ${PORT}/tcp を許可しました。"
fi

echo ""
echo "============================================================"
echo "セットアップが完了しました！"
echo "  Web UI: http://$(hostname -I | awk '{print $1}'):${PORT}"
echo "============================================================"
if [[ ! -f "${APP_DIR}/token.json" ]]; then
  echo ""
  echo "[残りの手動手順: Google Drive 初回認証]"
  echo "  1. Google Cloud Console の OAuth クライアント JSON を以下に配置:"
  echo "     ${APP_DIR}/credentials.json (chmod 600)"
  echo "  2. 初回認証を実行:"
  echo "     cd ${APP_DIR} && uv run python auth_drive.py && uv run python drive_client.py"
fi
