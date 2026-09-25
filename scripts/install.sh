#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CODEX_DIR=${CODEX_HOME:-"$HOME/.codex"}
BACKUP_DIR="$CODEX_DIR/elastic-budget-backups/$(date +%Y%m%d-%H%M%S)"
FRESH_STATE=0

if [ "${1:-}" = "--fresh-state" ]; then
  FRESH_STATE=1
elif [ -n "${1:-}" ]; then
  echo "用法: $0 [--fresh-state]" >&2
  exit 2
fi

mkdir -p "$CODEX_DIR" "$BACKUP_DIR"
for name in config.toml elastic-budget-controller.py elastic-budget-policy.json elastic-budget-state.json; do
  if [ -f "$CODEX_DIR/$name" ]; then
    cp "$CODEX_DIR/$name" "$BACKUP_DIR/$name"
  fi
done

cp "$ROOT_DIR/elastic-budget-controller.py" "$CODEX_DIR/elastic-budget-controller.py"
cp "$ROOT_DIR/elastic-budget-policy.example.json" "$CODEX_DIR/elastic-budget-policy.json"
chmod 755 "$CODEX_DIR/elastic-budget-controller.py"

if [ "$FRESH_STATE" -eq 1 ]; then
  rm -f "$CODEX_DIR/elastic-budget-state.json"
fi

case "$(uname -s)" in
  Darwin)
    LABEL=com.wzs2004.codex-elastic-budget-controller
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$CODEX_DIR/elastic-budget-controller.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>120</integer>
  <key>StandardOutPath</key><string>$CODEX_DIR/elastic-budget-controller.out.log</string>
  <key>StandardErrorPath</key><string>$CODEX_DIR/elastic-budget-controller.err.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"

    LEGACY="$HOME/Library/LaunchAgents/com.incursa.codex-cost-config-guard.plist"
    if [ -f "$LEGACY" ] && grep -q 'cost-config-guard.py' "$LEGACY"; then
      cp "$LEGACY" "$BACKUP_DIR/"
      launchctl bootout "gui/$(id -u)/com.incursa.codex-cost-config-guard" 2>/dev/null || true
      mv "$LEGACY" "$LEGACY.disabled"
    fi
    SCHEDULER="macOS LaunchAgent（每 120 秒）"
    ;;
  Linux)
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"
    cat > "$SYSTEMD_DIR/codex-elastic-budget-controller.service" <<EOF
[Unit]
Description=Codex Elastic Budget Controller

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 $CODEX_DIR/elastic-budget-controller.py
EOF
    cat > "$SYSTEMD_DIR/codex-elastic-budget-controller.timer" <<EOF
[Unit]
Description=Run Codex Elastic Budget Controller periodically

[Timer]
OnBootSec=30
OnUnitActiveSec=120
Unit=codex-elastic-budget-controller.service

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now codex-elastic-budget-controller.timer
    SCHEDULER="systemd 用户定时器（每 120 秒）"
    ;;
  *)
    echo "已复制控制器，但当前系统未自动配置定时任务。" >&2
    SCHEDULER="未配置"
    ;;
esac

python3 "$CODEX_DIR/elastic-budget-controller.py" --force --verbose
echo
echo "安装完成：$CODEX_DIR"
echo "自动运行：$SCHEDULER"
echo "备份位置：$BACKUP_DIR"
echo "提示：新建一个 Codex 对话后，新的上下文窗口设置才会完整应用。"
