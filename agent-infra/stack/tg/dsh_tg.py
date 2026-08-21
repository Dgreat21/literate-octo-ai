#!/usr/bin/env python3
"""
dsh-tg — телеграм-бот-конструктор для DeepSeek Harness.

Крутится на твоей машине, ходит в Telegram long polling'ом — входящие порты
пробрасывать не нужно. Конструктор на инлайн-кнопках: выбрал воркспейс и
модель, дальше просто пишешь задачи текстом.

Ключ провайдера у каждого свой (`/key`, раздел «ключи» ниже): прогон
оплачивается ключом того, кто пишет, а не ключом хозяина машины.

  python3 -m venv ~/.dsh-tg/venv && ~/.dsh-tg/venv/bin/pip install aiogram pyyaml
  export TG_BOT_TOKEN=...                       # от @BotFather
  export TG_ALLOWED_USERS=123456789             # свои id через запятую
  export TG_WORKSPACES="octo:/path/to/repo,home:$HOME"
  ~/.dsh-tg/venv/bin/python dsh_tg.py

Полная инструкция и слои удалённого доступа — ../herdr-dsh-instruction.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field

import yaml
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
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

DSH_HOME = os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh"))
SETTINGS_PATH = os.path.join(DSH_HOME, "settings.yaml")

# Маршрут, который llm-deepseek объявляет сам: в settings.yaml его моделей нет.
BUILTIN_ROUTES: dict[str, list[tuple[str, str]]] = {
    "deepseek-official": [
        ("deepseek-v4-flash", "DeepSeek-V4-Flash"),
        ("deepseek-v4-pro", "DeepSeek-V4-Pro"),
    ],
}


def load_settings() -> dict:
    """Тот же документ, что читает сам dsh (см. dsh-settings-file)."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}


def discover_models() -> dict[str, str]:
    """provider/model → подпись кнопки.

    Источник — маршруты llm-pi-ai из settings.yaml плюс встроенные: добавил
    провайдера в settings.yaml — он сам появился в боте, второго списка
    моделей держать не нужно.
    """
    found: dict[str, str] = {}
    settings = load_settings()
    for route, profile in ((settings.get("llm-pi-ai") or {}).get("providers") or {}).items():
        for entry in (profile or {}).get("models") or []:
            if entry.get("id"):
                found[f"{route}/{entry['id']}"] = entry.get("name") or entry["id"]
    if "llm-deepseek" in settings:
        for mid, label in BUILTIN_ROUTES["deepseek-official"]:
            found.setdefault(f"deepseek-official/{mid}", label)
    return found


# TG_MODELS перекрывает автообнаружение; пустой результат — последний фолбэк
MODEL_LABELS: dict[str, str] = {
    m: m for m in os.environ.get("TG_MODELS", "").split(",") if m
} or discover_models() or {"deepseek-official/deepseek-v4-flash": "DeepSeek-V4-Flash"}
MODELS: list[str] = list(MODEL_LABELS)

# "cli"  — вызывает `dsh --profile headless`, использует твою текущую установку.
# "sdk"  — deepseek-harness-sdk со своим встроенным Node-рантаймом.
#          Только Linux x64/arm64 и macOS 14+ на Apple Silicon.
RUNNER = os.environ.get("DSH_TG_RUNNER", "cli")

# dsh может не быть в PATH пейна (npx-установка): up.sh передаёт полный путь
DSH_BIN: list[str] = shlex.split(os.environ.get("DSH_BIN", "dsh"))

SESSION_ROOT = os.path.expanduser("~/.dsh-tg/sessions")
TIMEOUT_SEC = int(os.environ.get("DSH_TG_TIMEOUT", "900"))

# По каталогу на пользователя: личный credentials-документ и свои логи сессий
USERS_ROOT = os.path.expanduser("~/.dsh-tg/users")

# Необязательно: репортить состояние в Herdr, если бот запущен внутри панели.
HERDR_SOURCE = "custom:dsh-tg"

# ─────────────────────────── состояние ──────────────────────────


@dataclass
class ChatState:
    workspace: str | None = None
    model: str = MODELS[0]
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    busy: bool = False
    # автор последнего действия: ключ и логи сессий — личные, а состояние
    # панели живёт на чат, поэтому чей это чат, помним отдельно
    uid: int = 0


STATES: dict[int, ChatState] = {}


def state_of(event: Message | CallbackQuery) -> ChatState:
    message = event if isinstance(event, Message) else event.message
    st = STATES.setdefault(message.chat.id, ChatState())
    if event.from_user:
        st.uid = event.from_user.id
    return st


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


# ─────────────────── ключи: у каждого свой ──────────────────────
#
# Задача пользователя оплачивается ЕГО ключом, а не ключом хозяина машины.
# Порядок слоёв в dsh (credentials-local, docstring его index.ts):
#
#   унаследованное окружение  >  credentials-документ  >  .env-фолбэки
#
# Поэтому решение из двух половин, и обе обязательны:
#  1) каждому прогону подсовываем ЛИЧНЫЙ credentials-документ (--patch по
#     строке `credentials`) вместо общего ~/.dsh/.credentials.yaml хозяина;
#  2) из окружения дочернего процесса вычищаем ссылки на ключи — иначе
#     ANTHROPIC_API_KEY, экспортированный в пейне хозяина, побьёт документ
#     по слоям и молча оплатит чужую задачу.
#
# Ключ едет файлом, а не переменной окружения прогона: env и аргументы
# дочернего процесса видны в `ps`, документ 0600 — нет.

# llm-deepseek объявляет свою ссылку сам (DEFAULT_API_KEY_ENV в его index.ts)
DEEPSEEK_REF = "DEEPSEEK_API_KEY"


def provider_ref(model: str) -> str | None:
    """Какой ключ нужен маршруту модели — имя credential-ref.

    None = маршрут аутентифицируется мимо ключей (OAuth, ambient-discovery
    pi-ai). Такой по людям не разделить, и бот его не пускает.
    """
    route = model.split("/", 1)[0]
    profile = ((load_settings().get("llm-pi-ai") or {}).get("providers") or {}).get(route)
    if isinstance(profile, dict):
        return profile.get("apiKeyEnv") or None
    return DEEPSEEK_REF if route in BUILTIN_ROUTES else None


def user_home(uid: int) -> str:
    return os.path.join(USERS_ROOT, str(uid))


def creds_path(uid: int) -> str:
    return os.path.join(user_home(uid), ".credentials.yaml")


def sessions_root(uid: int) -> str:
    return os.path.join(user_home(uid), "sessions")


def ensure_user_home(uid: int) -> None:
    os.makedirs(sessions_root(uid), mode=0o700, exist_ok=True)


def load_user_creds(uid: int) -> dict[str, str]:
    """Документ ровно того формата, что читает credentials-local: плоское
    отображение ref → строка."""
    if not uid:
        return {}
    try:
        with open(creds_path(uid), encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str)}


def store_user_cred(uid: int, ref: str, value: str | None) -> None:
    """Положить/снять ключ. Режим 0600 обязателен: credentials-local
    отказывается читать документ, доступный кому-то кроме владельца."""
    ensure_user_home(uid)
    doc = load_user_creds(uid)
    if value is None:
        doc.pop(ref, None)
    else:
        doc[ref] = value
    path, tmp = creds_path(uid), creds_path(uid) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True)
    os.replace(tmp, path)


# Правило легального ключа — то же, что у самого dsh (агент-нота
# api-key-format-validation): после trim непусто и все символы печатные ASCII.
# Вторая проверка — эвристика про вставленную строку окружения; в dsh она
# живёт только на поверхности, где человек вставляет ключ. Здесь такая
# поверхность — чат.
KEY_CHARS = re.compile(r"^[\x21-\x7E]+$")
ENV_LINE = re.compile(r"^[A-Z][A-Z0-9_]*=[^=]")


def key_problem(value: str) -> str | None:
    if not value:
        return "ключ пустой"
    if not KEY_CHARS.match(value):
        return "в ключе символы, которых не бывает в HTTP-заголовке (пробел, кириллица, эмодзи)"
    if ENV_LINE.match(value) or (len(value) > 1 and value[0] == value[-1] and value[0] in "\"'"):
        return "похоже, вставлена строка вида NAME=значение или ключ в кавычках — нужно только значение"
    return None


def mask(value: str) -> str:
    return f"{value[:4]}…{value[-4:]}" if len(value) > 12 else "…"


def child_env() -> dict[str, str]:
    """Окружение прогона без чужих ключей: унаследованное окружение бьёт
    credentials-документ, так что ключ хозяина отсюда надо убрать."""
    drop = {ref for ref in (provider_ref(m) for m in MODELS) if ref} | {DEEPSEEK_REF}
    return {
        key: value
        for key, value in os.environ.items()
        if key not in drop and not key.endswith("_API_KEY")
    }


# ──────────────────────────── раннеры ───────────────────────────


def run_patch(model: str, uid: int) -> tuple[str, list[str]]:
    """Оверлей одного прогона: модель, ключ пользователя, его логи сессий.

    Одного оверлея на agent-default-model НЕ хватает: композиционное значение —
    только база секции, а смонтированный settings-провайдер кладёт поверх неё
    выбор из $DSH_HOME/settings.yaml (README dsh-agent-default-model). Пока
    выбор писался только в базу, каждый прогон уходил в модель из settings.yaml
    независимо от нажатой кнопки. Поэтому переводим settings-провайдер на копию
    того же документа с подменённой секцией: остальные секции (маршруты
    llm-pi-ai с их apiKeyEnv, permission, ui) едут как есть, оригинал не трогаем.

    Строки `credentials` и `session-persistence-jsonl` уводят прогон в каталог
    пользователя: чужим ключом он не платит и чужих логов не видит.
    """
    provider, name = model.split("/", 1)
    ensure_user_home(uid)

    doc = load_settings()
    selection = dict(doc.get("agent-default-model") or {})
    selection.update(provider=provider, model=name)
    # усилие рассуждения принадлежит прошлому выбору: у новой модели его может
    # не быть вовсе, а унаследованное значение — чужая настройка
    selection.pop("reasoningEffort", None)
    doc["agent-default-model"] = selection

    fd, settings_path = tempfile.mkstemp(prefix="dsh-tg-settings", suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False)

    rows = [
        {"id": "settings", "config": {"path": settings_path}},
        {"id": "agent-default-model", "config": {"provider": provider, "model": name}},
        # watch выключен: документ переписывает только бот, и только между прогонами
        {"id": "credentials", "config": {"path": creds_path(uid), "watch": False}},
        {"id": "session-persistence-jsonl", "config": {"root": sessions_root(uid)}},
    ]
    fd, patch_path = tempfile.mkstemp(prefix="dsh-tg-patch", suffix=".yml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(rows, fh, allow_unicode=True, sort_keys=False)
    return patch_path, [settings_path]


def run_via_cli(prompt: str, st: ChatState) -> str:
    """Один headless-прогон. Точный контракт CLI, ничего не угадываем."""
    patch, temps = run_patch(st.model, st.uid)
    try:
        proc = subprocess.run(
            [*DSH_BIN, "--profile", "headless", "--patch", patch, prompt],
            cwd=WORKSPACES[st.workspace],
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
        )
    finally:
        for path in (patch, *temps):
            try:
                os.unlink(path)
            except OSError:
                pass
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


# Постоянная нижняя клавиатура: конструктор не тонет в простыне сообщений —
# эти кнопки живут над полем ввода и не уезжают вверх с историей.
BTN_PANEL = "⚙️ панель"
BTN_NEW = "🆕 сессия"
BTN_SESSIONS = "🗂 сессии"


def kb_persistent() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text=BTN_PANEL),
            KeyboardButton(text=BTN_SESSIONS),
            KeyboardButton(text=BTN_NEW),
        ]],
        resize_keyboard=True,
        is_persistent=True,
    )


def key_line(st: ChatState) -> str:
    ref = provider_ref(st.model)
    if ref is None:
        return "🔑 у маршрута нет apiKeyEnv"
    return f"🔑 {ref}: {'твой' if ref in load_user_creds(st.uid) else 'НУЖЕН'}"


def kb_main(st: ChatState) -> InlineKeyboardMarkup:
    ws = st.workspace or "не выбран"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📁 {ws}", callback_data="pick:ws")],
            [InlineKeyboardButton(
                text=f"🧠 {MODEL_LABELS.get(st.model, st.model)}", callback_data="pick:model"
            )],
            [InlineKeyboardButton(text=key_line(st), callback_data="do:key")],
            [
                InlineKeyboardButton(text="🗂 сессии", callback_data="do:sessions"),
                InlineKeyboardButton(text="🆕 новая сессия", callback_data="do:new"),
            ],
        ]
    )


def kb_options(kind: str, values: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"set:{kind}:{value}")]
            for value, label in values
        ]
        + [[InlineKeyboardButton(text="← назад", callback_data="do:back")]]
    )


# ─────────────────────── навигатор по сессиям ───────────────────
#
# Только чтение: headless-профиль принимает лишь текст задачи, флага сессии у
# него нет (dsh --profile headless --help), поэтому продолжить старую сессию с
# телефона нельзя — можно посмотреть, что в ней было.

@dataclass
class SessionCard:
    sid: str
    title: str
    created: float
    model: str
    turns: int
    last_answer: str


def _ws_log_dir(uid: int, ws_path: str) -> str:
    """Каталог логов воркспейса внутри логов пользователя: dsh кодирует cwd
    в имя папки. Корень личный (см. run_patch) — чужие задачи и ответы в
    навигатор не попадают."""
    return os.path.join(
        sessions_root(uid), "--" + ws_path.strip("/").replace("/", "-") + "--"
    )


def _events(log: str) -> list[dict]:
    """session.jsonl.zstd → события. zstd берём внешним бинарником, чтобы не
    тащить лишнюю зависимость в venv бота."""
    zstd = shutil.which("zstd") or "/opt/homebrew/bin/zstd"
    if not os.path.exists(zstd):
        return []
    proc = subprocess.run(
        [zstd, "-dc", log], capture_output=True, text=True, timeout=30, check=False
    )
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def read_card(log: str, sid: str) -> SessionCard | None:
    created = 0.0
    title = ""
    model = ""
    turns = 0
    blocks: list[str] = []
    for event in _events(log):
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "session":
            created = (event.get("createdAt") or 0) / 1000
        elif kind == "session/title":
            title = data.get("title") or title
        elif kind == "user/message":
            turns += 1
        elif kind == "request/header":
            config = (data.get("header") or {}).get("config") or {}
            if config.get("model"):
                model = f"{config.get('provider', '?')}/{config['model']}"
        elif kind == "assistant/chunk":
            chunk = data.get("chunk") or {}
            if chunk.get("type") == "block-end":
                text = (chunk.get("block") or {}).get("text")
                if text:
                    blocks.append(text)
    if not created:
        return None
    return SessionCard(
        sid, title or "(без темы)", created, model or "?", turns, blocks[-1] if blocks else ""
    )


def list_cards(uid: int, ws_path: str, limit: int = 8) -> list[SessionCard]:
    root = _ws_log_dir(uid, ws_path)
    if not os.path.isdir(root):
        return []
    newest = sorted(
        (entry for entry in os.scandir(root) if entry.is_dir()),
        key=lambda entry: entry.stat().st_mtime,
        reverse=True,
    )[:limit]
    cards = []
    for entry in newest:
        log = os.path.join(entry.path, "session.jsonl.zstd")
        if os.path.exists(log):
            card = read_card(log, entry.name)
            if card:
                cards.append(card)
    return cards


def kb_sessions(cards: list[SessionCard]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{time.strftime('%d.%m %H:%M', time.localtime(card.created))} · {card.title[:30]}",
                callback_data=f"sess:{card.sid}"[:64],
            )]
            for card in cards
        ]
        + [[InlineKeyboardButton(text="← назад", callback_data="do:back")]]
    )


dp = Dispatcher()


async def show_panel(target: Message, st: ChatState) -> None:
    await target.answer("Конструктор dsh:", reply_markup=kb_main(st))


async def show_sessions(target: Message, st: ChatState) -> None:
    if not st.workspace:
        await target.answer("Сначала выбери воркспейс.", reply_markup=kb_main(st))
        return
    cards = await asyncio.to_thread(list_cards, st.uid, WORKSPACES[st.workspace])
    if not cards:
        await target.answer(f"В {st.workspace} ещё нет сохранённых сессий.", reply_markup=kb_main(st))
        return
    await target.answer(
        f"Последние сессии · {st.workspace}\nТолько чтение: headless не умеет продолжать сессию.",
        reply_markup=kb_sessions(cards),
    )


@dp.message(Command("start", "menu"))
async def cmd_start(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_of(msg)
    context_note = (
        "Каждая задача — отдельный headless-прогон: контекст между сообщениями "
        "не сохраняется."
        if RUNNER == "cli"
        else "Сессия держится между сообщениями."
    )
    await msg.answer(
        f"Конструктор dsh. Выбери воркспейс и модель, потом просто пиши задачи.\n{context_note}\n"
        "Кнопки внизу никуда не уедут — /start больше искать не нужно.",
        reply_markup=kb_persistent(),
    )
    await show_panel(msg, st)


@dp.message(Command("sessions"))
async def cmd_sessions(msg: Message) -> None:
    if not allowed(msg):
        return
    await show_sessions(msg, state_of(msg))


@dp.message(Command("new"))
async def cmd_new(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_of(msg)
    st.session_id = uuid.uuid4().hex[:12]
    await msg.answer("Сессия сброшена.", reply_markup=kb_main(st))


@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(cq: CallbackQuery) -> None:
    kind = cq.data.split(":", 1)[1]
    values = (
        [(name, f"📁 {name}") for name in WORKSPACES]
        if kind == "ws"
        else [(model, MODEL_LABELS.get(model, model)) for model in MODELS]
    )
    await cq.message.edit_reply_markup(reply_markup=kb_options(kind, values))
    await cq.answer()


@dp.callback_query(F.data.startswith("set:"))
async def cb_set(cq: CallbackQuery) -> None:
    _, kind, value = cq.data.split(":", 2)
    st = state_of(cq)
    if kind == "ws":
        st.workspace = value
    else:
        st.model = value
    await cq.message.edit_reply_markup(reply_markup=kb_main(st))
    await cq.answer(f"→ {value}")


@dp.callback_query(F.data == "do:sessions")
async def cb_sessions(cq: CallbackQuery) -> None:
    await cq.answer()
    await show_sessions(cq.message, state_of(cq))


@dp.callback_query(F.data.startswith("sess:"))
async def cb_session(cq: CallbackQuery) -> None:
    sid = cq.data.split(":", 1)[1]
    st = state_of(cq)
    await cq.answer()
    if not st.workspace:
        return
    log = os.path.join(
        _ws_log_dir(st.uid, WORKSPACES[st.workspace]), sid, "session.jsonl.zstd"
    )
    card = await asyncio.to_thread(read_card, log, sid) if os.path.exists(log) else None
    if card is None:
        await cq.message.answer("Лог этой сессии не читается.", reply_markup=kb_main(st))
        return
    stamp = time.strftime("%d.%m %H:%M", time.localtime(card.created))
    await cq.message.answer(
        f"🗂 {card.title}\n{stamp} · {card.model} · сообщений: {card.turns}\n"
        f"`{card.sid}`\n\n{card.last_answer[:1500] or '(ответа в логе нет)'}",
        reply_markup=kb_main(st),
    )


@dp.callback_query(F.data.in_({"do:back", "do:new"}))
async def cb_do(cq: CallbackQuery) -> None:
    st = state_of(cq)
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


KEY_HELP = (
    "Ключ у каждого свой: задача уходит в провайдера с твоим ключом и на твой счёт.\n\n"
    "Прислать (только в личку боту):\n"
    "  /key <ключ>            — под ссылку текущей модели\n"
    "  /key <REF> <ключ>      — явно, например /key DEEPSEEK_API_KEY sk-...\n"
    "  /key drop [REF]        — снять\n"
    "  /key                   — что уже лежит\n\n"
    "Сообщение с ключом бот удаляет сразу; ключ ложится в "
    "~/.dsh-tg/users/<твой id>/.credentials.yaml с правами 0600."
)


@dp.callback_query(F.data == "do:key")
async def cb_key(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(KEY_HELP)


@dp.message(Command("key"))
async def cmd_key(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_of(msg)
    args = (msg.text or "").split()[1:]

    if not args:
        stored = load_user_creds(st.uid)
        listing = "\n".join(f"  {ref}: {mask(v)}" for ref, v in sorted(stored.items()))
        await msg.answer(
            (f"Твои ключи:\n{listing}" if stored else "Ключей ещё нет.")
            + f"\n\nТекущей модели нужен: {provider_ref(st.model) or '— (маршрут без apiKeyEnv)'}"
            + f"\n\n{KEY_HELP}"
        )
        return

    # ключ, отправленный в группу, уже утёк — принимать его нельзя
    if msg.chat.type != "private":
        await msg.answer("Ключ — только в личку боту. Этот уже засвечен: отзови его у провайдера.")
        return

    if args[0] == "drop":
        ref = args[1] if len(args) > 1 else provider_ref(st.model)
        if not ref:
            await msg.answer("Не понял, какую ссылку снимать: /key drop <REF>.")
            return
        store_user_cred(st.uid, ref, None)
        await msg.answer(f"{ref} снят.", reply_markup=kb_main(st))
        return

    # ссылка — только если первое слово похоже на имя переменной окружения:
    # иначе «/key sk-... лишнее слово» молча сохранило бы ключ под ссылкой sk-...
    if len(args) > 1 and re.match(r"^[A-Z][A-Z0-9_]*$", args[0]):
        ref, value = args[0], args[1]
    else:
        ref, value = provider_ref(st.model), args[0]
    try:
        await msg.delete()  # секрет в истории чата не живёт
    except Exception:  # noqa: BLE001 — не удалилось, скажем об этом ниже
        await msg.answer("⚠️ не смог удалить сообщение с ключом — удали его сам.")
    if not ref:
        await msg.answer("У маршрута текущей модели нет apiKeyEnv — назови ссылку явно: /key <REF> <ключ>.")
        return
    problem = key_problem(value.strip())
    if problem:
        await msg.answer(f"Не принял: {problem}.")
        return
    store_user_cred(st.uid, ref, value.strip())
    await msg.answer(f"{ref} сохранён ({mask(value.strip())}).", reply_markup=kb_main(st))


@dp.message(F.text.in_({BTN_PANEL, BTN_SESSIONS, BTN_NEW}))
async def on_button(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_of(msg)
    if msg.text == BTN_NEW:
        st.session_id = uuid.uuid4().hex[:12]
        await msg.answer("Сессия сброшена.", reply_markup=kb_main(st))
    elif msg.text == BTN_SESSIONS:
        await show_sessions(msg, st)
    else:
        await show_panel(msg, st)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_task(msg: Message) -> None:
    if not allowed(msg):
        return
    st = state_of(msg)

    if not st.workspace:
        await msg.answer("Сначала выбери воркспейс.", reply_markup=kb_main(st))
        return
    # fail-closed: без своего ключа прогона нет. Иначе dsh дошёл бы по слоям до
    # ключа хозяина машины (.env воркспейса, окружение) и оплатил задачу им.
    ref = provider_ref(st.model)
    if ref is None:
        await msg.answer(
            f"У маршрута {st.model.split('/', 1)[0]} в settings.yaml нет apiKeyEnv — "
            "ключ такой модели по людям не разделить. Выбери другую модель.",
            reply_markup=kb_main(st),
        )
        return
    if ref not in load_user_creds(st.uid):
        await msg.answer(f"Сначала пришли свой ключ.\n\n{KEY_HELP}", reply_markup=kb_main(st))
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
    tail = f" · {st.session_id}" if RUNNER == "sdk" else ""
    await msg.answer(
        f"⌛ {elapsed} с · {st.workspace} · {MODEL_LABELS.get(st.model, st.model)}{tail}",
        reply_markup=kb_main(st),
    )


async def main() -> None:
    if not ALLOWED_USERS:
        print("TG_ALLOWED_USERS пуст — бот отклонит всех. Впиши свой id.")
    # sdk-раннер держит харнесс в процессе бота: своего credentials-документа
    # на прогон там нет, значит все ходили бы под ключом хозяина. Одному себе
    # это можно, нескольким людям — нет.
    if RUNNER == "sdk" and len(ALLOWED_USERS) > 1:
        raise SystemExit(
            "DSH_TG_RUNNER=sdk не разделяет ключи по людям: либо один "
            "TG_ALLOWED_USERS, либо раннер cli."
        )
    os.makedirs(SESSION_ROOT, exist_ok=True)
    os.makedirs(USERS_ROOT, mode=0o700, exist_ok=True)
    herdr_report(state="idle", message="бот запущен, жду задач")
    bot = Bot(BOT_TOKEN)
    # кнопка «меню» слева от поля ввода: второй путь к панели, без прокрутки
    await bot.set_my_commands([
        BotCommand(command="start", description="панель: воркспейс и модель"),
        BotCommand(command="sessions", description="последние сессии воркспейса"),
        BotCommand(command="new", description="новая сессия"),
        BotCommand(command="key", description="свой ключ провайдера"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
