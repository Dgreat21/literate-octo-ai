#!/usr/bin/env python3
"""
Смоук dsh_tg без Telegram и без модели: состояние сессий, клавиатуры,
плашка прогресса, patch-оверлей и счётчик шагов.

  ~/.dsh-tg/venv/bin/python agent-infra/stack/tg/smoke_dsh_tg.py

Гоняет оба раннера (cli и sdk) — раннер выбирается на импорте, поэтому
скрипт перезапускает сам себя. Вместо dsh подставляется заглушка: она
печатает аргументы, окружение и содержимое оверлея, так что проверяется
именно то, что бот отправляет наружу.

В конце — негативный контроль (MST: verifier_negative_control): те же
проверки на сессии без настроек обязаны провалиться. Зелёный смоук на
сломанном входе означал бы, что проверка ничего не проверяет.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUB = """#!/usr/bin/env bash
echo "ARGS: $*"
echo "PERM=${DSH_PERMISSION_MODE:-unset} TOOLS=${DSH_TOOLS_MODE:-unset} CWD=$PWD"
for a in "$@"; do case "$a" in *.yml) cat "$a";; esac; done
"""


def run_both() -> int:
    """Прогнать себя же в обоих раннерах, вернуть код выхода."""
    failed = 0
    for runner in ("cli", "sdk"):
        env = dict(os.environ, DSH_TG_RUNNER=runner, DSH_TG_SMOKE_CHILD="1")
        failed |= subprocess.run([sys.executable, __file__], env=env).returncode
    print("\n✅ смоук пройден в обоих раннерах" if not failed else "\n❌ смоук провален")
    return failed


def main() -> None:
    stub_dir = Path(tempfile.mkdtemp(prefix="dsh-tg-smoke"))
    stub = stub_dir / "fake_dsh"
    stub.write_text(STUB)
    stub.chmod(0o755)

    os.environ.setdefault("TG_BOT_TOKEN", "123:FAKE")
    os.environ.setdefault("TG_ALLOWED_USERS", "1")
    os.environ["TG_WORKSPACES"] = f"octo:{HERE},home:{Path.home()}"
    os.environ["DSH_BIN"] = str(stub)
    sys.path.insert(0, str(HERE))
    import dsh_tg as bot

    print(f"── раннер {bot.RUNNER}: memory={bot.CAP.memory} "
          f"steps={bot.CAP.steps} stop={bot.CAP.stop}")

    # 1. сессии: заводятся, наследуют настройки, переключаются, открепляются
    st = bot.state_for(42)
    first = st.new_session()
    first.workspace, first.title, first.effort = "octo", "первая", "max"
    second = st.new_session()
    assert second.workspace == "octo" and second.effort == "max", "новая сессия не унаследовала настройки"
    assert st.attached == second.sid and len(st.sessions) == 2
    st.attached = first.sid
    assert st.current is first
    st.attached = None
    assert st.current is None, "открепление не сработало"
    print("сессии: наследование, переключение, открепление — ок")

    # 2. клавиатуры: рисуются, влезают в лимит callback_data, честно помечают
    #    настройки, которых этот раннер не применяет
    st.attached = first.sid
    for markup in (bot.kb_root(st), bot.kb_session(first), bot.kb_settings(first),
                   bot.kb_values("effort", bot.OPTIONS["effort"])):
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64, button.callback_data
    labels = [b.text for row in bot.kb_settings(first).inline_keyboard for b in row]
    warned = [t for t in labels if "⚠️" in t]
    if bot.RUNNER == "sdk":
        assert len(warned) == 3, f"sdk обязан пометить неприменимые настройки: {labels}"
    else:
        assert not warned, f"cli применяет всё, лишние предупреждения: {labels}"
    assert "открепить" in "".join(
        b.text for row in bot.kb_session(first).inline_keyboard for b in row
    )
    print("клавиатуры:", labels)

    # 3. плашка прогресса: шаги показываются там, где раннер их видит
    first.busy, first.started = True, bot.time.monotonic() - 75
    first.steps, first.activity = 7, "tools/result"
    text = bot.progress_text(first)
    assert "1 мин 15 с" in text, text
    assert ("шаг 7" in text) == bot.CAP.steps, text
    print("плашка:", text.replace("\n", " | "))

    # 4. счётчик шагов из уведомлений рантайма
    note = lambda method, payload: types.SimpleNamespace(method=method, payload=payload)
    probe = bot.Session(sid="t1")
    for item in (
        note("session.event", {"event": {"type": "tools/result"}}),
        note("session.event", {"event": {"type": "assistant/message"}}),
        note("session.status", {"status": "idle"}),
        note("subagent.started", {}),
        note("session.event", {"event": {"type": "turn/end"}}),
    ):
        bot._note_step(probe, item)
    assert probe.steps == 3, f"шагов насчитано {probe.steps}, ждали 3"
    assert probe.activity == "turn/end", probe.activity
    print("счётчик шагов: 3 шага, последняя активность turn/end — ок")

    # 5. cli-раннер сквозь заглушку: что именно уезжает в dsh
    if bot.RUNNER == "cli":
        first.busy = False
        first.model = "deepseek-official/deepseek-v4-pro"
        first.effort, first.perm, first.tools = "max", "danger-full-access", "code"
        out = bot.run_via_cli("проверка", first)
        assert "reasoningEffort: max" in out, out
        assert "provider: deepseek-official" in out and "model: deepseek-v4-pro" in out, out
        assert "PERM=danger-full-access" in out and "TOOLS=code" in out, out
        assert f"CWD={bot.WORKSPACES['octo']}" in out, out
        first.effort = first.perm = first.tools = bot.INHERIT
        bare = bot.run_via_cli("проверка", first)
        assert "reasoningEffort" not in bare, "«по профилю» не должно попадать в оверлей"
        assert "PERM=unset" in bare and "TOOLS=unset" in bare, bare
        print("cli-раннер: оверлей и env доезжают, «по профилю» ничего не навязывает")

        # негативный контроль: на сессии без настроек те же проверки обязаны упасть
        blank = bot.Session(sid="neg", workspace="octo", model="deepseek-official/deepseek-v4-flash")
        got = bot.run_via_cli("проверка", blank)
        try:
            assert "reasoningEffort: max" in got and "PERM=danger-full-access" in got
        except AssertionError:
            print("негативный контроль: на пустых настройках проверка падает — значит, проверяет")
        else:
            raise SystemExit("НЕГАТИВНЫЙ КОНТРОЛЬ ПРОВАЛЕН: зелено на сломанном входе")

    print(f"✅ смоук {bot.RUNNER} пройден")


if __name__ == "__main__":
    sys.exit(main() if os.environ.get("DSH_TG_SMOKE_CHILD") else run_both())
