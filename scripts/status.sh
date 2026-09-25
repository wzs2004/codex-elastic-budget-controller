#!/bin/sh
set -eu

CODEX_DIR=${CODEX_HOME:-"$HOME/.codex"}
CONFIG="$CODEX_DIR/config.toml"
STATE="$CODEX_DIR/elastic-budget-state.json"

python3 - "$CONFIG" "$STATE" <<'PY'
import json, pathlib, re, sys

config_path, state_path = map(pathlib.Path, sys.argv[1:])
config = config_path.read_text() if config_path.exists() else ""
state = json.loads(state_path.read_text()) if state_path.exists() else {}

def value(name):
    match = re.search(rf"(?m)^{re.escape(name)}\s*=\s*(.+?)\s*$", config)
    return match.group(1).strip('"') if match else "未设置"

metrics = state.get("metrics") or {}
configured_window = value("model_context_window")
print("弹性调节器状态")
print(f"- 当前档位：{state.get('current_profile', '尚未运行')}")
print(f"- 上下文窗口：{configured_window} tokens")
print(f"- 自动压缩点：{value('model_auto_compact_token_limit')} tokens")
print(f"- 推理强度：{value('model_reasoning_effort')}")
print(f"- 最近评估：{state.get('last_evaluated_at', '无')}")
try:
    reported_window = float(metrics.get("reported_context_window") or 0)
    window_mismatch = reported_window and not 0.75 <= reported_window / float(configured_window) <= 1.25
except (TypeError, ValueError, ZeroDivisionError):
    window_mismatch = False
if metrics.get("legacy_session") or window_mismatch:
    print("- 当前旧对话：已隔离；请新建 Codex 对话以应用新窗口")
else:
    print("- 当前对话：可参与弹性评估")
PY

case "$(uname -s)" in
  Darwin)
    if launchctl print "gui/$(id -u)/com.wzs2004.codex-elastic-budget-controller" >/dev/null 2>&1; then
      echo "- 自动运行：已启用（macOS，每 120 秒）"
    else
      echo "- 自动运行：未启用"
    fi
    ;;
  Linux)
    if systemctl --user is-enabled codex-elastic-budget-controller.timer >/dev/null 2>&1; then
      echo "- 自动运行：已启用（Linux，每 120 秒）"
    else
      echo "- 自动运行：未启用"
    fi
    ;;
esac
