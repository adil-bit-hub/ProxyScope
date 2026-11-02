#!/usr/bin/env bash
#
# ProxyScope 启动脚本 (Linux / macOS)
#
# 用法:
#   ./start.sh                     使用默认端口启动
#   ./start.sh --listen 0.0.0.0    允许局域网其他机器连入代理
#   ./start.sh --help              查看全部 Python 层参数
#
# 常用环境变量:
#   PROXY_PORT=18888   代理端口
#   WEB_PORT=18787     Web 界面端口
#   LISTEN=127.0.0.1   代理监听地址（需要外部访问改成 0.0.0.0）
#   WEB_HOST=127.0.0.1 Web 界面监听地址
#   DATA_DIR=./data    数据目录（证书 + 数据库）
#   PYTHON=python3     指定解释器
#
set -euo pipefail

# ---------- 进入脚本所在目录，保证相对路径可用 ----------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------- 可配置项 ----------
PROXY_PORT="${PROXY_PORT:-18888}"
WEB_PORT="${WEB_PORT:-18787}"
LISTEN="${LISTEN:-127.0.0.1}"
WEB_HOST="${WEB_HOST:-127.0.0.1}"
DATA_DIR="${DATA_DIR:-$SCRIPT_DIR/data}"
VERIFY_UPSTREAM="${VERIFY_UPSTREAM:-0}"
MAX_FLOWS="${MAX_FLOWS:-200000}"
TIMEOUT="${TIMEOUT:-60}"

# ---------- 颜色（非终端时自动关闭） ----------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
  C_CYN=$'\033[36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_YEL=''; C_CYN=''; C_DIM=''; C_RST=''
fi

info()  { printf '%s[*]%s %s\n' "$C_CYN" "$C_RST" "$*"; }
ok()    { printf '%s[+]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
die()   { printf '%s[x]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }

# ---------- 1. 定位 Python ----------
find_python() {
  local candidates=()
  [ -n "${PYTHON:-}" ] && candidates+=("$PYTHON")
  candidates+=(python3 python)

  for cand in "${candidates[@]}"; do
    command -v "$cand" >/dev/null 2>&1 || continue
    # 需要 >= 3.9（asyncio.start_tls 等特性）
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)" || die "未找到 Python 3.9+，请先安装：
    Debian/Ubuntu : sudo apt install python3
    CentOS/RHEL   : sudo yum install python3
    macOS         : brew install python3
  或用环境变量指定：PYTHON=/usr/local/bin/python3.11 ./start.sh"

PY_VER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "Python: $PY ($PY_VER)"

# ---------- 2. 检查 openssl（HTTPS 解密必需） ----------
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      # 用户只是想看参数说明，跳过后续环境检查，直接交给 Python
      export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
      export PYTHONIOENCODING="utf-8"
      exec "$PY" -m proxyscope --help
      ;;
  esac
done

if [ -n "${PROXYSCOPE_OPENSSL:-}" ] && [ -x "${PROXYSCOPE_OPENSSL}" ]; then
  ok "OpenSSL: $PROXYSCOPE_OPENSSL"
elif command -v openssl >/dev/null 2>&1; then
  ok "OpenSSL: $(command -v openssl) ($(openssl version 2>/dev/null | cut -d' ' -f1-2))"
else
  warn "未找到 openssl —— HTTP 流量仍可抓，但 HTTPS 无法解密。"
  warn "安装: sudo apt install openssl  或  sudo yum install openssl"
fi

# ---------- 3. 可选依赖提示（缺失不影响主流程） ----------
MISSING_OPT="$("$PY" - <<'EOF' 2>/dev/null
mods = []
try:
    import brotli
except ImportError:
    mods.append("brotli")
try:
    import zstandard
except ImportError:
    mods.append("zstandard")
print(" ".join(mods))
EOF
)"
if [ -n "${MISSING_OPT// /}" ]; then
  warn "缺少可选依赖 (${MISSING_OPT})，对应压缩格式的响应体不会被解压。"
  warn "需要的话: $PY -m pip install ${MISSING_OPT}"
fi

# ---------- 4. 端口占用检查 ----------
# 注意: 这里让 awk 自己判定并返回状态码，不用 grep -q。
# 因为 set -o pipefail 下 grep -q 提前退出会给上游 awk 发 SIGPIPE，
# 管道整体返回 141，把"端口已占用"误判成"端口空闲"。
port_in_use() {
  local port="$1"
  local out
  if command -v ss >/dev/null 2>&1; then
    out="$(ss -ltn 2>/dev/null || true)"
  elif command -v netstat >/dev/null 2>&1; then
    out="$(netstat -ltn 2>/dev/null || true)"
  else
    return 1   # 没有检测工具，视为空闲
  fi
  # 第 4 列是本地地址，形如 127.0.0.1:18888 / *:18888 / [::]:18888
  printf '%s\n' "$out" | awk -v p=":$port" '
    $4 ~ /:/ && substr($4, length($4) - length(p) + 1) == p { found = 1; exit }
    END { exit(found ? 0 : 1) }
  '
}

if port_in_use "$PROXY_PORT"; then
  die "代理端口 ${PROXY_PORT} 已被占用，请换端口：
     PROXY_PORT=18889 ./start.sh"
fi
if port_in_use "$WEB_PORT"; then
  die "Web 端口 ${WEB_PORT} 已被占用，请换端口：
     WEB_PORT=18788 ./start.sh"
fi

# ---------- 5. 数据目录 ----------
mkdir -p "$DATA_DIR" || die "无法创建数据目录: $DATA_DIR"

# ---------- 6. 组装参数 ----------
ARGS=(
  --listen "$LISTEN"
  --port "$PROXY_PORT"
  --web-host "$WEB_HOST"
  --web-port "$WEB_PORT"
  --data-dir "$DATA_DIR"
  --max-flows "$MAX_FLOWS"
  --timeout "$TIMEOUT"
)
if [ "$VERIFY_UPSTREAM" = "1" ]; then
  ARGS+=(--verify-upstream)
fi

export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING="utf-8"
export PYTHONUNBUFFERED="1"

# ---------- 7. 提示如何信任 CA ----------
CA_FILE="$DATA_DIR/certs/proxyscope-ca.crt"
if [ ! -f "$CA_FILE" ]; then
  info "首次运行，将自动生成根证书：$CA_FILE"
fi

printf '\n'
info "代理监听 : ${LISTEN}:${PROXY_PORT}"
info "Web 界面 : http://${WEB_HOST}:${WEB_PORT}"
info "数据目录 : ${DATA_DIR}"

# 监听非回环地址时提醒风险：这等于开放一个无鉴权的抓包代理
case "$LISTEN" in
  127.*|localhost|::1) : ;;
  *)
    warn "代理已监听在 ${LISTEN}，除本机外其他机器也能连入。"
    warn "任何人都能通过它上网，且流量会被记录 —— 仅在受信任的内网使用。"
    ;;
esac
case "$WEB_HOST" in
  127.*|localhost|::1) : ;;
  *)
    warn "Web 界面监听在 ${WEB_HOST}，且没有登录认证，"
    warn "抓到的内容（含 Cookie / Token）对网络内所有人可见。建议用 SSH 隧道转发访问。"
    ;;
esac

printf '%s\n' "${C_DIM}  停止服务: Ctrl+C${C_RST}"
printf '\n'

# ---------- 8. 启动 ----------
exec "$PY" -m proxyscope "${ARGS[@]}" "$@"
