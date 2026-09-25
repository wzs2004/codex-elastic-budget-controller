#!/bin/sh
set -eu

CODEX_DIR=${CODEX_HOME:-"$HOME/.codex"}

case "$(uname -s)" in
  Darwin)
    LABEL=com.wzs2004.codex-elastic-budget-controller
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
    ;;
  Linux)
    systemctl --user disable --now codex-elastic-budget-controller.timer 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/codex-elastic-budget-controller.service"
    rm -f "$HOME/.config/systemd/user/codex-elastic-budget-controller.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    ;;
esac

rm -f "$CODEX_DIR/elastic-budget-controller.py" "$CODEX_DIR/elastic-budget-policy.json"
echo "控制器和自动任务已移除。config.toml 未自动回滚。"
echo "如需恢复配置，请从 $CODEX_DIR/elastic-budget-backups/ 选择备份。"
