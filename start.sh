#!/usr/bin/env bash
# ============================================================================
# 番茄小说本地一键启动脚本
# 功能：依赖检查 → 自动创建虚拟环境 → 安装/同步依赖 → 启动后端(+前端)
# 用法：
#   ./start.sh                 # 自动检测：有 npm 则同时启前后端，否则只启后端
#   ./start.sh --backend-only  # 只启动后端
#   ./start.sh --frontend-only # 只启动前端（需后端已运行或配置远程地址）
#   ./start.sh --no-frontend   # 强制不启前端
#   ./start.sh --port 8080     # 指定后端端口（默认 5000）
#   ./start.sh --reinstall     # 强制重新安装依赖
#   ./start.sh --check         # 仅做依赖检查，不启动服务
#   ./start.sh --help          # 查看帮助
# ============================================================================
set -eo pipefail

# ---------- 路径与默认配置 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/source/backend"
FRONTEND_DIR="$SCRIPT_DIR/source/frontend"
VENV_DIR="$BACKEND_DIR/.venv"
REQUIREMENTS="$BACKEND_DIR/requirements.txt"
PORT="${PORT:-5000}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=9          # 代码用了 PEP585（list[dict] 等），需 3.9+

# 运行模式（默认空=自动；后端+前端按检测结果启动）
MODE="auto"
REINSTALL=0
CHECK_ONLY=0
START_BACKEND=0
START_FRONTEND=0

# ---------- 颜色输出 ----------
if [[ -t 1 ]]; then
  C_RESET='\033[0m'; C_INFO='\033[36m'; C_OK='\033[32m'
  C_WARN='\033[33m'; C_ERR='\033[31m'; C_BOLD='\033[1m'
else
  C_RESET=''; C_INFO=''; C_OK=''; C_WARN=''; C_ERR=''; C_BOLD=''
fi
log()  { printf "${C_INFO}[%s]${C_RESET} %s\n" "$(date +%H:%M:%S)" "$*"; }
ok()   { printf "${C_OK}[✓]%s %s\n" "${C_RESET}" "$*"; }
warn() { printf "${C_WARN}[!]%s %s\n" "${C_RESET}" "$*"; }
err()  { printf "${C_ERR}[✗]%s %s\n" "${C_RESET}" "$*" >&2; }
title(){ printf "\n${C_BOLD}═══ %s ═══${C_RESET}\n" "$*"; }

# ---------- 参数解析 ----------
usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-only)   MODE="backend" ;;
    --frontend-only)  MODE="frontend" ;;
    --no-frontend)    MODE="backend" ;;
    --port)           PORT="$2"; shift ;;
    --reinstall)      REINSTALL=1 ;;
    --check)          CHECK_ONLY=1 ;;
    --help|-h)        usage ;;
    *) err "未知参数: $1（用 --help 查看用法）"; exit 1 ;;
  esac
  shift
done

# ---------- 1. Python 环境检查 ----------
check_python() {
  title "1/4 检查 Python 环境"
  if ! command -v python3 >/dev/null 2>&1; then
    err "未找到 python3，请先安装 Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+"
    exit 1
  fi
  PY_BIN="$(command -v python3)"
  PY_VER="$("$PY_BIN" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
  PY_MAJOR="$("$PY_BIN" -c 'import sys;print(sys.version_info[0])')"
  PY_MINOR="$("$PY_BIN" -c 'import sys;print(sys.version_info[1])')"
  if [[ "$PY_MAJOR" -lt "$MIN_PY_MAJOR" ]] || \
     [[ "$PY_MAJOR" -eq "$MIN_PY_MAJOR" && "$PY_MINOR" -lt "$MIN_PY_MINOR" ]]; then
    err "Python 版本过低：当前 $PY_VER，需要 ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+"
    exit 1
  fi
  ok "Python $PY_VER  ($PY_BIN)"

  # 检查 venv 模块
  if ! "$PY_BIN" -c 'import venv' 2>/dev/null; then
    err "Python 缺少 venv 模块。Debian/Ubuntu: sudo apt install python3-venv"
    exit 1
  fi
  ok "venv 模块可用"
}

# ---------- 2. 虚拟环境 ----------
ensure_venv() {
  title "2/4 准备虚拟环境"
  if [[ ! -d "$VENV_DIR" ]]; then
    log "创建虚拟环境: $VENV_DIR"
    "$PY_BIN" -m venv "$VENV_DIR"
    ok "虚拟环境已创建"
  else
    ok "虚拟环境已存在: $VENV_DIR"
  fi
  # 激活（在本 shell 生效）
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install --upgrade pip --quiet
  ok "pip 已就绪: $(pip --version)"
}

# ---------- 3. 依赖安装与检查 ----------
check_and_install_deps() {
  title "3/4 依赖检查与安装"
  if [[ ! -f "$REQUIREMENTS" ]]; then
    err "依赖文件不存在: $REQUIREMENTS"
    exit 1
  fi

  # 解析依赖名（去掉 == 版本与注释）
  mapfile -t DEPS < <(grep -vE '^\s*#|^\s*$' "$REQUIREMENTS" | sed -E 's/==.*//; s/\[.*\]//; s/^\s+//; s/\s+$//')

  # 是否需要安装：--reinstall 或 任一包缺失
  NEED_INSTALL=0
  if [[ "$REINSTALL" -eq 1 ]]; then
    NEED_INSTALL=1
    warn "--reinstall 指定，将重新安装全部依赖"
  else
    for dep in "${DEPS[@]}"; do
      # 包名 → import 名映射（部分包名与模块名不同）
      case "$dep" in
        python-docx) imp="docx" ;;
        ebooklib)    imp="ebooklib" ;;
        Pillow)      imp="PIL" ;;
        psycopg2-binary|psycopg2) imp="psycopg2" ;;
        flask-cors)  imp="flask_cors" ;;
        flask-sqlalchemy) imp="flask_sqlalchemy" ;;
        *)           imp="$dep" ;;
      esac
      if ! python -c "import $imp" 2>/dev/null; then
        warn "缺失依赖: $dep（import $imp 失败）"
        NEED_INSTALL=1
        break
      fi
    done
  fi

  if [[ "$NEED_INSTALL" -eq 1 ]]; then
    log "安装依赖: pip install -r requirements.txt"
    pip install -r "$REQUIREMENTS" --quiet
    ok "依赖安装完成"
  else
    ok "依赖完整，跳过安装"
  fi

  # 最终校验：逐个 import 报告
  log "依赖校验明细："
  FAIL_COUNT=0
  for dep in "${DEPS[@]}"; do
    case "$dep" in
      python-docx) imp="docx" ;;
      ebooklib)    imp="ebooklib" ;;
      Pillow)      imp="PIL" ;;
      psycopg2-binary|psycopg2) imp="psycopg2" ;;
      flask-cors)  imp="flask_cors" ;;
      flask-sqlalchemy) imp="flask_sqlalchemy" ;;
      *)           imp="$dep" ;;
    esac
    if python -c "import $imp" 2>/dev/null; then
      printf "   ${C_OK}[✓]%s %-22s (import %s)\n" "${C_RESET}" "$dep" "$imp"
    else
      printf "   ${C_ERR}[✗]%s %-22s (import %s 失败)\n" "${C_RESET}" "$dep" "$imp"
      FAIL_COUNT=$((FAIL_COUNT+1))
    fi
  done
  if [[ "$FAIL_COUNT" -gt 0 ]]; then
    err "有 $FAIL_COUNT 个依赖校验失败，请手动排查: pip install -r requirements.txt"
    exit 1
  fi
}

# ---------- 4. 启动服务 ----------
PIDS=()

cleanup() {
  title "正在停止服务"
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      log "已发送终止信号: PID $pid"
    fi
  done
  wait 2>/dev/null || true
  ok "已退出"
}
trap cleanup EXIT INT TERM

start_backend() {
  title "4/4 启动后端 (端口 $PORT)"
  cd "$BACKEND_DIR"
  export PORT="$PORT"
  log "启动: python app.py  (工作目录: $BACKEND_DIR)"
  # 后台运行，日志直接输出到当前终端
  python app.py &
  BACKEND_PID=$!
  PIDS+=("$BACKEND_PID")
  log "后端 PID: $BACKEND_PID"

  # 健康检查（最多等 15 秒）
  log "健康检查中..."
  for i in $(seq 1 15); do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      err "后端进程已退出，请查看上方日志"
      exit 1
    fi
    if curl -sf "http://127.0.0.1:${PORT}/api/templates" >/dev/null 2>&1 \
       || curl -sf "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
      ok "后端已就绪: http://127.0.0.1:${PORT}"
      return 0
    fi
    sleep 1
  done
  warn "后端 15 秒内未响应健康检查（可能仍在初始化数据库），继续运行中..."
}

start_frontend() {
  title "启动前端 (Vite dev server)"
  if ! command -v npm >/dev/null 2>&1; then
    warn "未找到 npm，跳过前端启动（可手动用浏览器访问后端）"
    return 0
  fi
  if [[ ! -d "$FRONTEND_DIR" ]]; then
    warn "前端目录不存在: $FRONTEND_DIR，跳过"
    return 0
  fi
  cd "$FRONTEND_DIR"
  # 自动 npm install
  if [[ ! -d node_modules ]]; then
    log "首次运行，安装前端依赖: npm install"
    npm install --silent 2>&1 | tail -5 || { warn "npm install 失败，跳过前端"; return 0; }
  fi
  log "启动: npm run dev"
  npm run dev &
  FRONTEND_PID=$!
  PIDS+=("$FRONTEND_PID")
  log "前端 PID: $FRONTEND_PID"
  sleep 2
  ok "前端启动中，默认地址: http://localhost:5173（以终端实际输出为准）"
}

# ---------- 主流程 ----------
main() {
  echo -e "${C_BOLD}番茄小说本地启动器${C_RESET}  (端口=$PORT, 模式=$MODE)"
  echo ""

  # 仅前端模式跳过 Python 检查
  if [[ "$MODE" != "frontend" ]]; then
    check_python
    ensure_venv
    check_and_install_deps
  fi

  # --check 只检查不启动
  if [[ "$CHECK_ONLY" -eq 1 ]]; then
    title "依赖检查完成（--check 模式，不启动服务）"
    ok "环境就绪，可执行 ./start.sh 启动"
    exit 0
  fi

  # 决定启动哪些服务
  case "$MODE" in
    backend)
      START_BACKEND=1
      ;;
    frontend)
      START_FRONTEND=1
      ;;
    auto)
      START_BACKEND=1
      # 有 npm 且前端目录存在才自动启前端
      if command -v npm >/dev/null 2>&1 && [[ -d "$FRONTEND_DIR" ]]; then
        START_FRONTEND=1
      else
        warn "未检测到 npm，仅启动后端"
      fi
      ;;
  esac

  if [[ "$START_BACKEND" -eq 1 ]]; then start_backend; fi
  if [[ "$START_FRONTEND" -eq 1 ]]; then start_frontend; fi

  title "服务已启动  (Ctrl+C 停止全部)"
  [[ "$START_BACKEND" -eq 1 ]]  && echo -e "  后端: ${C_OK}http://127.0.0.1:${PORT}${C_RESET}"
  [[ "$START_FRONTEND" -eq 1 ]] && echo -e "  前端: ${C_OK}http://localhost:5173${C_RESET}"
  echo ""

  # 前台等待任一子进程退出
  wait
}

main "$@"
