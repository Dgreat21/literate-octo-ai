#!/usr/bin/env python3
"""
dsh-tg — телеграм-бот-конструктор для DeepSeek Harness.

Крутится на твоей машине, ходит в Telegram long polling'ом — входящие порты
пробрасывать не нужно. Конструктор на инлайн-кнопках: выбрал воркспейс и
модель, дальше просто пишешь задачи текстом.

  python3 -m venv ~/.dsh-tg/venv && ~/.dsh-tg/venv/bin/pip install aiogram
  export TG_BOT_TOKEN=...                       # от @BotFather
  export TG_ALLOWED_USERS=123456789             # свои id через запятую
  export TG_WORKSPACES="octo:/path/to/repo,home:$HOME"
  ~/.dsh-tg/venv/bin/python dsh_tg.py

Полная инструкция и слои удалённого доступа — ../herdr-dsh-instruction.md.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ──────────────────────────── конфиг ────────────────────────────

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]

# ВАЖНО: агент имеет bash на твоей машине. Пускать только себя.
# Пустой список = бот отклоняет всех (fail-closed).
# Свой id спроси у @userinfobot.
ALLOWED_USERS: set[int] = {
    int(x) for x in os.environ.get("TG_ALLOWED_USERS", "").replace(" ", "").split(",") if x
}

# имя:путь через запятую; только этот белый список доступен с телефона
WORKSPACES: dict[str, str] = dict(
    pair.split(":", 1)
    for pair in os.environ.get(
        "TG_WORKSPACES", f"home:{os.path.expanduser('~')}"
    ).split(",")
    if ":" in pair
)

# provider/model; headless сам флага модели не имеет — применяется --patch-оверлеем
MODELS: list[str] = [
    m for m in os.environ.get(
        "TG_MODELS",
        "deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro",
    ).split(",") if m
]

# "cli"  — вызывает `dsh --profile headless`, использует твою текущую установку.
# "sdk"  — deepseek-harness-sdk со своим встроенным Node-рантаймом.
#          Только Linux x64/arm64 и macOS 14+ на Apple Silicon.
RUNNER = os.environ.get("DSH_TG_RUNNER", "cli")

# dsh может не быть в PATH пейна (npx-установка): up.sh передаёт полный путь
DSH_BIN: list[str] = shlex.split(os.environ.get("DSH_BIN", "dsh"))

SESSION_ROOT = os.path.expanduser("~/.dsh-tg/sessions")
TIMEOUT_SEC = int(os.environ.get("DSH_TG_TIMEOUT", "900"))

# Необязательно: репортить состояние в Herdr, если бот запущен внутри панели.
HERDR_SOURCE = "custom:dsh-tg"

# ─────────────────────────── состояние ──────────────────────────


@dataclass
class ChatState:
    workspace: str | None = None
    model: str = MODELS[0]
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    busy: bool = False


STATES: dict[int, ChatState] = {}


def state_for(chat_id: int) -> ChatState:
    return STATES.setdefault(chat_id, ChatState())


# ──────────────────────────── herdr ─────────────────────────────


def herdr_report(**flags: str) -> None:
    """No-op вне Herdr. Те же report-agent, что и в bin/dsh-herdr.

    Допустимые --state: idle | working | blocked | unknown.
    Статуса `done` НЕТ — завершение репортится как idle с --message.
    """
    pane = os.environ.get("HERDR_PANE_ID")
    binary = os.environ.get("HERDR_BIN_PATH")
    if os.environ.get("HERDR_ENV") != "1" or not pane or not binary:
        return
    argv = [binary, "pane", "report-agent", pane, "--source", HERDR_SOURCE, "--agent", "dsh"]
    for key, value in flags.items():
        argv += [f"--{key.replace('_', '-')}", value]
    subprocess.run(argv, capture_output=True, check=False)


# ──────────────────────────── раннеры ───────────────────────────


def model_patch(model: str) -> str:
    """provider/model → путь к patch-оверлею профиля (см. dsh --patch)."""
    provider, name = model.split("/", 1)
    fd, path = tempfile.mkstemp(prefix="dsh-tg-model", suffix=".yml")
    with os.fdopen(fd, "w") as fh:
        fh.write(
            f"- id: agent-default-model\n  config:\n"
            f"    provider: {provider}\n    model: {name}\n"
        )
    return path


def run_via_cli(prompt: str, st: ChatState) -> str:
    """Один headless-прогон. Точный контракт CLI, ничего не угадываем."""
    patch = model_patch(st.model)
    try:
        proc = subprocess.run(
            [*DSH_BIN, "--profile", "headless", "--patch", patch, prompt],
            cwd=WORKSPACES[st.workspace],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
        )
    finally:
        os.unlink(patch)
    if proc.returncode != 0:
        return f"⚠️ dsh вышел с кодом {proc.returncode}\n\n{proc.stderr.strip()[:1500]}"
    return proc.stdout.strip() or "(пустой ответ)"


_harnesses: dict[str, object] = {}


def run_via_sdk(prompt: str, st: ChatState) -> str:
    """
    Держит один рантайм на воркспейс и переиспользует его между задачами.
    Тот же session_id сохраняет bash-процесс сессии: cwd, экспортированные
    переменные и функции шелла переживают сообщения.

    Сигнатуру run() сверь со своей версией SDK — она в developer preview:
        python -c "import inspect, deepseek_harness as d; \
                   print(inspect.signature(d.DeepSeekHarness.run))"
    """
    from deepseek_harness import DeepSeekHarness

    harness = _harnesses.get(st.workspace)
    if harness is None:
        harness = DeepSeekHarness(model=st.model.split("/", 1)[1])
        _harnesses[st.workspace] = harness

    result = harness.run(
        prompt,
        workspace=WORKSPACES[st.workspace],
        session_root=SESSION_ROOT,
        session_id=st.session_id,
    )
    return str(result).strip() or "(пустой ответ)"


def run_task(prompt: str, st: ChatState) -> str:
    return run_via_sdk(prompt, st) if RUNNER == "sdk" else run_via_cli(prompt, st)


# ────────────────────────── конструктор ─────────────────────────


def kb_main(st: ChatState) -> InlineKeyboardMarkup:
    ws = st.workspace or "не выбран"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📁 {ws}", callback_data="pick:ws")],
            [InlineKeyboardButton(text=f"🧠 {st.model}", callback_data="pick:model")],
            [InlineKeyboardButton(text="🆕 новая сессия", callback_data="do:new")],
        ]
    )


def kb_options(kind: str, values: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=v, callback_data=f"set:{kind}:{v}")] for v in values
        ]
        + [[InlineKeyboardButton(text="← назад", callback_data="do:back")]]
    )


dp = Dispatcher()


@dp.message(Command("start", "menu"))
async def cmd_start(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_for(msg.chat.id)
    await msg.answer(
        "Конструктор dsh. Выбери воркспейс и модель, потом просто пиши задачи.",
        reply_markup=kb_main(st),
    )


@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(cq: CallbackQuery) -> None:
    kind = cq.data.split(":", 1)[1]
    values = list(WORKSPACES) if kind == "ws" else MODELS
    await cq.message.edit_reply_markup(reply_markup=kb_options(kind, values))
    await cq.answer()


@dp.callback_query(F.data.startswith("set:"))
async def cb_set(cq: CallbackQuery) -> None:
    _, kind, value = cq.data.split(":", 2)
    st = state_for(cq.message.chat.id)
    if kind == "ws":
        st.workspace = value
    else:
        st.model = value
    await cq.message.edit_reply_markup(reply_markup=kb_main(st))
    await cq.answer(f"→ {value}")


@dp.callback_query(F.data.in_({"do:back", "do:new"}))
async def cb_do(cq: CallbackQuery) -> None:
    st = state_for(cq.message.chat.id)
    if cq.data == "do:new":
        st.session_id = uuid.uuid4().hex[:12]
    await cq.message.edit_reply_markup(reply_markup=kb_main(st))
    await cq.answer("сессия сброшена" if cq.data == "do:new" else None)


# ─────────────────────────── исполнение ─────────────────────────


def allowed(msg: Message) -> bool:
    if msg.from_user and msg.from_user.id in ALLOWED_USERS:
        return True
    print(f"отклонён user_id={msg.from_user.id if msg.from_user else '?'}")
    return False


@dp.message(F.text & ~F.text.startswith("/"))
async def on_task(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_for(msg.chat.id)

    if not st.workspace:
        await msg.answer("Сначала выбери воркспейс.", reply_markup=kb_main(st))
        return
    if st.busy:
        await msg.answer("Задача ещё крутится — дождись ответа или /new.")
        return

    st.busy = True
    started = time.monotonic()
    herdr_report(state="working", message=msg.text[:120])
    note = await msg.answer("⏳ работаю…")

    try:
        answer = await asyncio.to_thread(run_task, msg.text, st)
        herdr_report(state="idle", message="готово")
    except subprocess.TimeoutExpired:
        answer = f"⚠️ таймаут {TIMEOUT_SEC} с"
        herdr_report(state="blocked", message="таймаут")
    except Exception as exc:  # noqa: BLE001
        answer = f"⚠️ {type(exc).__name__}: {exc}"
        herdr_report(state="blocked", message=str(exc)[:120])
    finally:
        st.busy = False

    elapsed = int(time.monotonic() - started)
    await note.delete()
    for i in range(0, len(answer), 3900):
        await msg.answer(answer[i : i + 3900])
    await msg.answer(f"⌛ {elapsed} с · {st.workspace} · {st.model} · {st.session_id}")


async def main() -> None:
    if not ALLOWED_USERS:
        print("TG_ALLOWED_USERS пуст — бот отклонит всех. Впиши свой id.")
    os.makedirs(SESSION_ROOT, exist_ok=True)
    herdr_report(state="idle", message="бот запущен, жду задач")
    await dp.start_polling(Bot(BOT_TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
