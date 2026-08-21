#!/usr/bin/env python3
"""
dsh-tg — телеграм-бот-конструктор для DeepSeek Harness.

Крутится на твоей машине, ходит в Telegram long polling'ом — входящие порты
пробрасывать не нужно. В одном боте живёт несколько сессий: у каждой свой
воркспейс, модель и настройки; текст уходит в ту, что сейчас прикреплена.

  python3 -m venv ~/.dsh-tg/venv && ~/.dsh-tg/venv/bin/pip install aiogram
  export TG_BOT_TOKEN=...                       # от @BotFather
  export TG_ALLOWED_USERS=123456789             # свои id через запятую
  export TG_WORKSPACES="octo:/path/to/repo,home:$HOME"
  ~/.dsh-tg/venv/bin/python dsh_tg.py

Полная инструкция и слои удалённого доступа — ../herdr-dsh-instruction.md.
"""

from __future__ import annotations

import asyncio
import contextlib
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

# provider/model; headless сам флага модели не имеет — применяется --patch-оверлеем.
# Провайдер по умолчанию именно `deepseek-official`: это маршрут родного адаптера
# dsh-llm-deepseek. Имя `deepseek` принадлежит каталогу pi-ai и молчит, пока в
# settings.yaml нет секции llm-pi-ai.
MODELS: list[str] = [
    m for m in os.environ.get(
        "TG_MODELS",
        "deepseek-official/deepseek-v4-flash,deepseek-official/deepseek-v4-pro",
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

# как часто перерисовывается живая плашка прогона (редактирование сообщения —
# без нотификации, поэтому частить дешевле, чем присылать новые сообщения)
PROGRESS_EVERY = int(os.environ.get("DSH_TG_PROGRESS", "15"))

# sdk-раннер: путь к своей cordis-композиции (иначе берётся встроенная).
# Эффорт/права/подача инструментов в sdk задаются именно там — см. инструкцию §6.
SDK_CORDIS = os.environ.get("DSH_TG_CORDIS") or None

# Необязательно: репортить состояние в Herdr, если бот запущен внутри панели.
HERDR_SOURCE = "custom:dsh-tg"

# ───────────────────── словари настроек (сверено с dsh) ─────────────────────
#
# Значения не выдуманы, а взяты из пакетов проверенной ревизии:
#   effort  — config.reasoningEffort у ряда `llm-deepseek` (off | high | max);
#   tools   — config.mode у ряда `tools` (native | code | both), headless
#             читает его из DSH_TOOLS_MODE;
#   права   — DSH_PERMISSION_MODE: пресеты ряда `permission` в dsh-base
#             (read-only | workspace-write | danger-full-access).
# «по профилю» = бот ничего не навязывает, решает профиль/композиция.

INHERIT = "по профилю"

OPTIONS: dict[str, list[str]] = {
    "effort": [INHERIT, "off", "high", "max"],
    "tools": [INHERIT, "native", "code", "both"],
    "perm": [INHERIT, "read-only", "workspace-write", "danger-full-access"],
}

LABELS = {
    "ws": "воркспейс",
    "model": "модель",
    "effort": "усилие",
    "tools": "инструменты",
    "perm": "права",
}


@dataclass(frozen=True)
class Caps:
    """Что раннер умеет на самом деле — по этому гасятся кнопки и подписи."""

    memory: bool  # помнит ли контекст между задачами одной сессии
    steps: bool  # видны ли шаги прогона
    stop: bool  # можно ли прервать прогон
    applies: frozenset[str]  # какие настройки раннер реально применяет


CAPS: dict[str, Caps] = {
    # headless на каждую задачу создаёт свежего агента (README dsh-headless:
    # «creates one fresh persisted Agent»), памяти между задачами нет.
    "cli": Caps(False, False, True, frozenset({"ws", "model", "effort", "tools", "perm"})),
    # sdk держит рантайм и адресует сессию по id — контекст живёт; настройки
    # композиции бот не крутит (их задаёт cordis-файл), зато видны события.
    "sdk": Caps(True, True, False, frozenset({"ws", "model"})),
}

CAP = CAPS.get(RUNNER, CAPS["cli"])

# ─────────────────────────── состояние ──────────────────────────


@dataclass
class Session:
    """Одна сессия бота: своя настройка, свой прогон, свой хвост сообщений."""

    sid: str
    title: str = "новая сессия"
    workspace: str | None = None
    model: str = field(default_factory=lambda: MODELS[0])
    effort: str = INHERIT
    tools: str = INHERIT
    perm: str = INHERIT

    busy: bool = False
    started: float = 0.0
    steps: int = 0
    activity: str = ""
    runs: int = 0
    proc: subprocess.Popen | None = None  # cli-раннер: чтобы было чем остановить
    bound: tuple[str, str] | None = None  # sdk: (воркспейс, модель) рантайма сессии

    def get(self, kind: str) -> str:
        return {
            "ws": self.workspace or "не выбран",
            "model": self.model,
            "effort": self.effort,
            "tools": self.tools,
            "perm": self.perm,
        }[kind]

    def set(self, kind: str, value: str) -> None:
        if kind == "ws":
            self.workspace = value
        elif kind == "model":
            self.model = value
        else:
            setattr(self, kind, value)


@dataclass
class ChatState:
    sessions: dict[str, Session] = field(default_factory=dict)
    attached: str | None = None

    @property
    def current(self) -> Session | None:
        return self.sessions.get(self.attached or "")

    def new_session(self) -> Session:
        prev = self.current or next(iter(self.sessions.values()), None)
        s = Session(sid=uuid.uuid4().hex[:6])
        if prev:  # новая сессия наследует настройки прошлой — так меньше тапов
            s.workspace, s.model = prev.workspace, prev.model
            s.effort, s.tools, s.perm = prev.effort, prev.tools, prev.perm
        self.sessions[s.sid] = s
        self.attached = s.sid
        return s


STATES: dict[int, ChatState] = {}


def state_for(chat_id: int) -> ChatState:
    return STATES.setdefault(chat_id, ChatState())


def busy_sessions() -> int:
    return sum(1 for st in STATES.values() for s in st.sessions.values() if s.busy)


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


def patch_overlay(s: Session) -> str:
    """Настройки сессии → patch-оверлей профиля (см. dsh --patch)."""
    provider, name = s.model.split("/", 1)
    rows = [f"- id: agent-default-model\n  config:\n    provider: {provider}\n    model: {name}\n"]
    if s.effort != INHERIT:
        rows.append(f"- id: llm-deepseek\n  config:\n    reasoningEffort: {s.effort}\n")
    fd, path = tempfile.mkstemp(prefix="dsh-tg-patch", suffix=".yml")
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(rows))
    return path


def run_via_cli(prompt: str, s: Session) -> str:
    """Один headless-прогон. Точный контракт CLI, ничего не угадываем.

    Флагов у headless-приложения ровно два — сама задача и --help, поэтому
    модель и усилие едут patch-оверлеем, а права и подача инструментов —
    переменными окружения, которые читает композиция.
    """
    patch = patch_overlay(s)
    env = dict(os.environ)
    if s.perm != INHERIT:
        env["DSH_PERMISSION_MODE"] = s.perm
    if s.tools != INHERIT:
        env["DSH_TOOLS_MODE"] = s.tools
    try:
        proc = subprocess.Popen(
            [*DSH_BIN, "--profile", "headless", "--patch", patch, prompt],
            cwd=WORKSPACES[s.workspace],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        s.proc = proc
        try:
            out, err = proc.communicate(timeout=TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    finally:
        s.proc = None
        os.unlink(patch)
    if proc.returncode and proc.returncode < 0:
        return "⏹ прогон остановлен"
    if proc.returncode != 0:
        return f"⚠️ dsh вышел с кодом {proc.returncode}\n\n{err.strip()[:1500]}"
    return out.strip() or "(пустой ответ)"


_harnesses: dict[tuple[str, str], object] = {}

# события корневой сессии, которые считаем шагом; остальное показываем как
# активность, но не считаем — врать номером шага хуже, чем недосчитать
STEP_EVENTS = {"assistant/message", "turn/end"}


def _note_step(s: Session, notification: object) -> None:
    """Уведомление рантайма → счётчик шагов и подпись активности."""
    method = getattr(notification, "method", "")
    payload = getattr(notification, "payload", {}) or {}
    if method == "subagent.started":
        s.activity = "субагент"
        return
    if method != "session.event":
        return
    event = payload.get("event")
    kind = event.get("type") if isinstance(event, dict) else None
    if not isinstance(kind, str):
        return
    s.activity = kind
    if kind in STEP_EVENTS or kind.startswith("tools/"):
        s.steps += 1


def run_via_sdk(prompt: str, s: Session) -> str:
    """Рантайм на (воркспейс, модель); сессия адресуется своим id.

    Контракт сверен с deepseek-harness-sdk 0.1.1rc1: провайдер, модель и cwd
    фиксируются на `initialize`, поэтому сессия закрепляется за рантаймом при
    первом прогоне; on_notification отдаёт события корневой сессии и потомков.
    """
    from deepseek_harness import DeepSeekHarness

    key = s.bound or (s.workspace, s.model)
    s.bound = key
    harness = _harnesses.get(key)
    if harness is None:
        provider, name = key[1].split("/", 1)
        harness = DeepSeekHarness(
            provider=provider,
            model=name,
            cwd=WORKSPACES[key[0]],
            session_root=SESSION_ROOT,
            cordis=SDK_CORDIS,
            request_timeout_seconds=TIMEOUT_SEC,
        )
        _harnesses[key] = harness

    result = harness.start_session(s.sid).run(
        prompt, on_notification=lambda note: _note_step(s, note)
    )
    answer = (result.final_response or "").strip() or "(пустой ответ)"
    if result.finish_reason and result.finish_reason != "completed":
        answer += f"\n\n⚠️ прогон закончился как «{result.finish_reason}»"
    return answer


def run_task(prompt: str, s: Session) -> str:
    return run_via_sdk(prompt, s) if RUNNER == "sdk" else run_via_cli(prompt, s)


# ────────────────────────── конструктор ─────────────────────────


def human(seconds: float) -> str:
    sec = int(seconds)
    return f"{sec} с" if sec < 60 else f"{sec // 60} мин {sec % 60:02d} с"


def badge(s: Session) -> str:
    return "⏳" if s.busy else "·"


def sess_line(s: Session, attached: bool) -> str:
    """Строка сессии для списка: что это, где крутится, чем занята."""
    tail = f"шаг {s.steps} · {human(time.monotonic() - s.started)}" if s.busy else (
        f"прогонов: {s.runs}" if s.runs else "пустая"
    )
    if s.busy and not CAP.steps:
        tail = human(time.monotonic() - s.started)
    pin = " 📌" if attached else ""
    return f"{badge(s)} #{s.sid} {s.title}{pin} — {s.workspace or 'без воркспейса'} · {tail}"


def kb_root(st: ChatState) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{badge(s)} {s.title}"[:40], callback_data=f"s:{s.sid}")]
        for s in st.sessions.values()
    ]
    rows.append([InlineKeyboardButton(text="🆕 новая сессия", callback_data="d:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_session(s: Session) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📁 {s.get('ws')}", callback_data="p:ws")],
            [InlineKeyboardButton(text=f"🧠 {s.model}", callback_data="p:model")],
            [InlineKeyboardButton(text="⚙️ доп. настройки", callback_data="m:set")],
            [InlineKeyboardButton(text="📌 открепить", callback_data="d:detach")],
            [
                InlineKeyboardButton(text="🆕 новая", callback_data="d:new"),
                InlineKeyboardButton(text="≡ сессии", callback_data="m:root"),
            ],
        ]
    )


def kb_settings(s: Session) -> InlineKeyboardMarkup:
    def button(kind: str, icon: str) -> list[InlineKeyboardButton]:
        mark = "" if kind in CAP.applies else " ⚠️"
        text = f"{icon} {LABELS[kind]}: {s.get(kind)}{mark}"
        return [InlineKeyboardButton(text=text, callback_data=f"p:{kind}")]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            button("effort", "🎚"),
            button("tools", "🧰"),
            button("perm", "🔐"),
            [InlineKeyboardButton(text="← назад", callback_data="m:sess")],
        ]
    )


def kb_values(kind: str, values: list[str]) -> InlineKeyboardMarkup:
    back = "m:sess" if kind in {"ws", "model"} else "m:set"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=v, callback_data=f"v:{kind}:{v}")] for v in values
        ]
        + [[InlineKeyboardButton(text="← назад", callback_data=back)]]
    )


def kb_running(s: Session) -> InlineKeyboardMarkup | None:
    if not CAP.stop:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏹ остановить", callback_data=f"x:{s.sid}")]]
    )


def text_root(st: ChatState) -> str:
    if not st.sessions:
        return "Сессий пока нет. Заведи первую — дальше просто пиши задачи текстом."
    lines = [sess_line(s, s.sid == st.attached) for s in st.sessions.values()]
    tail = (
        "Прикреплённая сессия принимает текст; остальные досылают ответ в ответ на свою задачу."
        if st.attached
        else "Сессия не прикреплена — выбери, куда отправлять текст."
    )
    return "Сессии:\n" + "\n".join(lines) + "\n\n" + tail


def text_session(s: Session) -> str:
    memory = "помнит контекст между задачами" if CAP.memory else (
        "без памяти между задачами: headless на каждую задачу поднимает свежего агента"
    )
    return (
        f"Сессия #{s.sid} · {s.title}\n"
        f"{s.workspace or 'воркспейс не выбран'} · {s.model}\n"
        f"раннер {RUNNER}: {memory}"
    )


def text_settings(s: Session) -> str:
    if CAP.applies >= {"effort", "tools", "perm"}:
        return "Доп. настройки прогона. Применяются со следующей задачи."
    return (
        "Доп. настройки прогона. ⚠️ — раннер sdk их не применяет: усилие, права и "
        "подача инструментов заданы его cordis-композицией (DSH_TG_CORDIS)."
    )


def progress_text(s: Session) -> str:
    elapsed = human(time.monotonic() - s.started)
    head = f"⏳ #{s.sid} {s.title} · {elapsed}"
    if CAP.steps:
        return f"{head}\nшаг {s.steps}" + (f" · {s.activity}" if s.activity else "")
    return f"{head}\nраннер cli шагов не отдаёт — виден только ход времени"


dp = Dispatcher()


def allowed_user(user_id: int | None) -> bool:
    if user_id is not None and user_id in ALLOWED_USERS:
        return True
    print(f"отклонён user_id={user_id if user_id is not None else '?'}")
    return False


def allowed(msg: Message) -> bool:
    return allowed_user(msg.from_user.id if msg.from_user else None)


@dp.message(Command("start", "menu"))
async def cmd_start(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_for(msg.chat.id)
    if not st.sessions:
        st.new_session()
    s = st.current
    if s:
        await msg.answer(text_session(s), reply_markup=kb_session(s))
    else:
        await msg.answer(text_root(st), reply_markup=kb_root(st))


@dp.message(Command("new"))
async def cmd_new(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_for(msg.chat.id)
    s = st.new_session()
    await msg.answer(text_session(s), reply_markup=kb_session(s))


@dp.message(Command("sessions"))
async def cmd_sessions(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_for(msg.chat.id)
    await msg.answer(text_root(st), reply_markup=kb_root(st))


@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(cq: CallbackQuery) -> None:
    if not allowed_user(cq.from_user.id if cq.from_user else None):
        return
    st = state_for(cq.message.chat.id)
    where = cq.data.split(":", 1)[1]
    s = st.current
    if where == "root" or s is None:
        await cq.message.edit_text(text_root(st), reply_markup=kb_root(st))
    elif where == "set":
        await cq.message.edit_text(text_settings(s), reply_markup=kb_settings(s))
    else:
        await cq.message.edit_text(text_session(s), reply_markup=kb_session(s))
    await cq.answer()


@dp.callback_query(F.data.startswith("s:"))
async def cb_attach(cq: CallbackQuery) -> None:
    """Переключение между сессиями — то же, что вкладки на десктопе."""
    if not allowed_user(cq.from_user.id if cq.from_user else None):
        return
    st = state_for(cq.message.chat.id)
    sid = cq.data.split(":", 1)[1]
    if sid not in st.sessions:
        await cq.answer("такой сессии больше нет")
        return
    st.attached = sid
    s = st.sessions[sid]
    await cq.message.edit_text(text_session(s), reply_markup=kb_session(s))
    await cq.answer(f"→ {s.title}")


@dp.callback_query(F.data.startswith("p:"))
async def cb_pick(cq: CallbackQuery) -> None:
    if not allowed_user(cq.from_user.id if cq.from_user else None):
        return
    st = state_for(cq.message.chat.id)
    if st.current is None:
        await cq.answer("сначала выбери сессию")
        return
    kind = cq.data.split(":", 1)[1]
    values = {"ws": list(WORKSPACES), "model": MODELS}.get(kind) or OPTIONS[kind]
    await cq.message.edit_reply_markup(reply_markup=kb_values(kind, values))
    await cq.answer()


@dp.callback_query(F.data.startswith("v:"))
async def cb_set(cq: CallbackQuery) -> None:
    if not allowed_user(cq.from_user.id if cq.from_user else None):
        return
    st = state_for(cq.message.chat.id)
    s = st.current
    if s is None:
        await cq.answer("сначала выбери сессию")
        return
    _, kind, value = cq.data.split(":", 2)
    s.set(kind, value)
    if kind in {"ws", "model"}:
        await cq.message.edit_text(text_session(s), reply_markup=kb_session(s))
    else:
        await cq.message.edit_text(text_settings(s), reply_markup=kb_settings(s))
    # sdk фиксирует воркспейс и модель на рантайме сессии: правка доедет
    # только до следующей сессии, и об этом честнее сказать сразу
    stale = RUNNER == "sdk" and s.bound is not None and kind in {"ws", "model"}
    await cq.answer(
        f"→ {value}" + (" (эта сессия останется на прежнем рантайме)" if stale else "")
    )


@dp.callback_query(F.data.startswith("d:"))
async def cb_do(cq: CallbackQuery) -> None:
    if not allowed_user(cq.from_user.id if cq.from_user else None):
        return
    st = state_for(cq.message.chat.id)
    action = cq.data.split(":", 1)[1]
    if action == "new":
        s = st.new_session()
        await cq.message.edit_text(text_session(s), reply_markup=kb_session(s))
        await cq.answer("новая сессия")
    else:  # detach
        st.attached = None
        await cq.message.edit_text(text_root(st), reply_markup=kb_root(st))
        await cq.answer("откреплено")


@dp.callback_query(F.data.startswith("x:"))
async def cb_stop(cq: CallbackQuery) -> None:
    if not allowed_user(cq.from_user.id if cq.from_user else None):
        return
    st = state_for(cq.message.chat.id)
    s = st.sessions.get(cq.data.split(":", 1)[1])
    if s is None or s.proc is None:
        await cq.answer("нечего останавливать")
        return
    s.proc.kill()
    await cq.answer("останавливаю")


# ─────────────────────────── исполнение ─────────────────────────


async def ticker(bot: Bot, chat_id: int, message_id: int, s: Session) -> None:
    """Живая плашка: правит одно сообщение, новых не шлёт — значит, не звенит."""
    last = ""
    while s.busy:
        await asyncio.sleep(PROGRESS_EVERY)
        if not s.busy:
            return
        text = progress_text(s)
        if text == last:
            continue
        last = text
        with contextlib.suppress(Exception):  # правка плашки не должна ронять прогон
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb_running(s)
            )


@dp.message(F.text & ~F.text.startswith("/"))
async def on_task(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_for(msg.chat.id)
    s = st.current

    if s is None:
        await msg.answer(
            "Сессия не прикреплена — выбери, куда отправлять.", reply_markup=kb_root(st)
        )
        return
    if not s.workspace:
        await msg.answer("Сначала выбери воркспейс.", reply_markup=kb_session(s))
        return
    if s.busy:
        await msg.answer(
            f"Сессия #{s.sid} ещё крутится. Заведи новую или подожди ответ.",
            reply_markup=kb_root(st),
        )
        return

    if s.runs == 0:
        s.title = (msg.text[:28] + "…") if len(msg.text) > 28 else msg.text
    s.busy, s.started, s.steps, s.activity = True, time.monotonic(), 0, ""
    s.runs += 1
    herdr_report(state="working", message=f"{s.title[:80]} · сессий в работе: {busy_sessions()}")
    note = await msg.answer(progress_text(s), reply_markup=kb_running(s))
    tick = asyncio.create_task(ticker(msg.bot, msg.chat.id, note.message_id, s))

    verdict = "✅"
    try:
        answer = await asyncio.to_thread(run_task, msg.text, s)
    except subprocess.TimeoutExpired:
        answer, verdict = f"⚠️ таймаут {TIMEOUT_SEC} с", "⚠️"
    except Exception as exc:  # noqa: BLE001
        answer, verdict = f"⚠️ {type(exc).__name__}: {exc}", "⚠️"
    finally:
        s.busy = False
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick

    elapsed = human(time.monotonic() - s.started)
    steps = f" · шагов {s.steps}" if CAP.steps else ""
    # пока в работе есть другие сессии, стадо должно видеть working, а не idle
    others = busy_sessions()
    herdr_report(
        state="blocked" if verdict == "⚠️" else ("working" if others else "idle"),
        message=f"{s.title[:80]} · {elapsed}" + (f" · ещё в работе: {others}" if others else ""),
    )
    with contextlib.suppress(Exception):
        await note.edit_text(f"{verdict} #{s.sid} {s.title} · {elapsed}{steps} · {s.model}")

    # Хвост сессии виден в ответе на её же задачу: пока крутится несколько
    # сессий, ветка ответа — единственное, что не даёт перепутать, чей это текст.
    head = "" if st.attached == s.sid and len(st.sessions) == 1 else f"#{s.sid} {s.title}\n\n"
    answer = answer or "(пустой ответ)"
    first, chunk = True, 3900 - len(head)
    for i in range(0, len(answer), chunk):
        body = (head if first else "") + answer[i : i + chunk]
        await (msg.reply(body) if first else msg.answer(body))
        first = False


async def main() -> None:
    if not ALLOWED_USERS:
        print("TG_ALLOWED_USERS пуст — бот отклонит всех. Впиши свой id.")
    os.makedirs(SESSION_ROOT, exist_ok=True)
    herdr_report(state="idle", message=f"бот запущен ({RUNNER}), жду задач")
    await dp.start_polling(Bot(BOT_TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
