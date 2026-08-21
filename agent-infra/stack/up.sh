#!/usr/bin/env bash
# up.sh — единый вход стека octo: Herdr-воркспейс + dsh web + tg-бот + смоук.
# Идемпотентен: повторный запуск не плодит воркспейсы и панели —
# панели опознаются по фактической команде процесса, не по имени.
#
#   agent-infra/stack/up.sh
#   OCTO_DSH_PORT=3082 agent-infra/stack/up.sh
#
# Подробности — herdr-dsh-instruction.md рядом.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${OCTO_DSH_PORT:-3081}"
LABEL="octo"
VENV="$HOME/.dsh-tg/venv"

say()  { printf '\033[1m[up]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[up] ОШИБКА:\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Node ^22.19 || >=24
command -v node >/dev/null || fail "node не найден; нужен ^22.19 || >=24"
NODEV="$(node -p 'process.versions.node')"
node -e 'const [M,m]=process.versions.node.split(".").map(Number);
process.exit((M===22&&m>=19)||M>=24?0:1)' \
  || fail "node $NODEV не подходит: нужен ^22.19 || >=24"
say "node $NODEV — ок"

# 2. Herdr-сервер жив
command -v herdr >/dev/null || fail "herdr не найден: https://herdr.dev"
herdr status server >/dev/null 2>&1 || fail "Herdr-сервер не запущен: открой herdr в терминале"
command -v jq >/dev/null || fail "нужен jq"
say "herdr $(herdr --version | awk '{print $2}') — сервер жив"

# ── помощники ────────────────────────────────────────────────────
ws_panes() { herdr pane list --workspace "$WS_ID" | jq -r '.result.panes[].pane_id'; }

pane_cmdline() { # все foreground-командные строки пейна
  herdr pane process-info --pane "$1" 2>/dev/null \
    | jq -r '.result.process_info.foreground_processes[]?.cmdline // empty'
}

find_pane_by_cmd() { # пейн, в котором крутится процесс с подстрокой $1
  local pat="$1" p
  for p in $(ws_panes); do
    if pane_cmdline "$p" | grep -qF "$pat"; then echo "$p"; return 0; fi
  done
  return 1
}

pane_free() { # свободен = foreground только шелл (или пусто)
  ! pane_cmdline "$1" | grep -vqE '(^|/)(-?zsh|-?bash|fish)( |$)'
}

# 3. Воркспейс octo: найти или создать
WS_ID="$(herdr workspace list \
  | jq -r --arg l "$LABEL" '.result.workspaces[] | select(.label==$l) | .workspace_id' \
  | head -1)"
if [ -n "$WS_ID" ]; then
  say "воркспейс $LABEL уже есть: $WS_ID"
else
  CREATED="$(herdr workspace create --cwd "$ROOT" --label "$LABEL" --no-focus)"
  WS_ID="$(jq -r '.result.workspace.workspace_id' <<<"$CREATED")"
  say "создан воркспейс $WS_ID (cwd=$ROOT)"
fi

# 4. Панель dsh web (инстанс на проект: cwd = корень репо)
WEB_PANE="$(find_pane_by_cmd "dsh web --port $PORT" || true)"
if [ -z "$WEB_PANE" ] && curl -sf -o /dev/null "http://127.0.0.1:$PORT"; then
  fail "порт $PORT занят чужим процессом вне воркспейса $LABEL — выбери другой: OCTO_DSH_PORT=<порт> $0"
fi
if [ -z "$WEB_PANE" ]; then
  for p in $(ws_panes); do
    if pane_free "$p"; then WEB_PANE="$p"; break; fi
  done
  if [ -z "$WEB_PANE" ]; then
    FIRST="$(ws_panes | head -1)"
    WEB_PANE="$(herdr pane split --pane "$FIRST" --direction down --cwd "$ROOT" --no-focus \
      | jq -r '.result.pane.pane_id')"
  fi
  herdr pane run "$WEB_PANE" "dsh web --port $PORT" >/dev/null
  say "запускаю dsh web --port $PORT в $WEB_PANE"
fi
for _ in $(seq 1 30); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT" && break
  sleep 1
done
curl -sf -o /dev/null "http://127.0.0.1:$PORT" \
  || fail "web-панель не поднялась за 30 с: herdr pane read $WEB_PANE --source recent-unwrapped"
herdr pane report-agent "$WEB_PANE" --source custom:octo-stack --agent dsh \
  --state idle --message "web ui :$PORT" >/dev/null 2>&1 || true
say "web-панель: http://127.0.0.1:$PORT (пейн $WEB_PANE)"

# 5. Панель tg-бота (если есть токен)
if [ -n "${TG_BOT_TOKEN:-}" ]; then
  if BOT_PANE="$(find_pane_by_cmd "dsh_tg.py")"; then
    say "бот уже крутится в $BOT_PANE — пропускаю"
  else
    BOT_PANE="$(herdr pane split --pane "$WEB_PANE" --direction down --cwd "$ROOT" --no-focus \
      | jq -r '.result.pane.pane_id')"
    if [ ! -x "$VENV/bin/python" ]; then
      say "готовлю venv бота ($VENV)"
      python3 -m venv "$VENV" && "$VENV/bin/pip" -q install aiogram
    fi
    herdr pane run "$BOT_PANE" \
      "TG_WORKSPACES=\"octo:$ROOT,home:\$HOME\" \"$VENV/bin/python\" \"$ROOT/agent-infra/stack/tg/dsh_tg.py\"" \
      >/dev/null
    say "бот запущен в $BOT_PANE (лог: herdr pane read $BOT_PANE --source recent-unwrapped)"
  fi
else
  say "TG_BOT_TOKEN не задан — панель бота пропущена (см. herdr-dsh-instruction.md §4)"
fi

# 6. Смоук
COUNT="$(herdr agent list | jq '.result.agents | length')"
[ "$COUNT" -ge 1 ] || fail "herdr agent list пуст — report-agent не сработал"
say "смоук: web :$PORT отвечает, агентов в стаде: $COUNT"
say "готово. Reattach с телефона: ssh + 'herdr'; web-туннель: ssh -L $PORT:127.0.0.1:$PORT"
