#!/usr/bin/env bash
# 启动微信适配器。默认在 tmux 会话里跑，方便随时查看/扫码。
#   ./run.sh          后台 tmux 启动
#   ./run.sh -f       前台启动
#   ./run.sh --check  只做链路自检
#   其余参数原样透传给 owux
set -euo pipefail

cd "$(dirname "$0")/.."
SESSION=oc-owux
BIN=.venv/bin/owux
# 一切数据都落在工作目录（默认 ~/.config/open-webui-weixin/），代码目录保持只读。
# 想换地方就加 --dir，例：CMD=("$BIN" --dir /data/owux)
CMD=("$BIN")

if [[ ! -x "$BIN" ]]; then
    echo "缺少 .venv/bin/owux，请先执行: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
fi

if [[ "${1:-}" == "-f" ]]; then
    shift
    exec "${CMD[@]}" "$@"
fi

if [[ "${1:-}" == "--check" ]]; then
    exec "${CMD[@]}" --check
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "适配器已在 tmux 会话 $SESSION 中运行。"
    echo "  查看二维码/日志: tmux attach -t $SESSION   （脱离: Ctrl-b d）"
    echo "  不附着看末尾:   tmux capture-pane -t $SESSION -p -S -40 | tail -30"
    echo "  停止:           tmux kill-session -t $SESSION"
    exit 0
fi

tmux new-session -d -s "$SESSION" -c "$PWD" "${CMD[*]}"
echo "已在 tmux 会话 $SESSION 启动。"
echo "如首次运行，用以下命令查看终端里的微信二维码并扫码："
echo "  tmux attach -t $SESSION"
echo "（二维码实测约 2 分钟过期，程序会自动换新，持续等待扫码成功）"
