---
name: plan_tags_explain_json
description: Аудит неоптимальности планов запросов Greenplum/PostgreSQL через машинные «плохие теги» по EXPLAIN (FORMAT JSON). Применяй ВСЕГДА, когда речь идёт о планах запросов: анализ и ревью тяжёлого SQL, поиск причин тормозов и долгих DAG-тасков, разбор Motion/Nested Loop/партиций, приоритизация оптимизации, написание или починка детекторов анти-паттернов плана, а также когда возникает соблазн распарсить текстовый EXPLAIN регулярками — не парсь текст, читай JSON.
---

# Теги неоптимальности плана: читать JSON, а не текст

## 1. Почему JSON, а не текстовый EXPLAIN

Снимай план **только** как `EXPLAIN (FORMAT JSON) <запрос>`.

- Текстовый вывод — форматирование для человека: вложенность передана отступами,
  числа вплавлены в строку (`(cost=0.00..1.23 rows=4 width=627)`). Регулярка по нему
  ломается от смены версии GP/PG, длины имён и переносов, а вложенность приходится
  восстанавливать по числу пробелов — это гадание.
- JSON даёт стабильный контракт: `Node Type`, `Plans[]` (дети), `Plan Rows`,
  `Plan Width`, `Total Cost`, `Join Type`, `Senders`/`Receivers`,
  `Partitions Selected`/`Partitions Total`, `Optimizer`. Дерево уже разобрано.
- Разбор JSON тестируется офлайн на сохранённых планах — без доступа к кластеру.

Правило: если пишешь `re.search` по выводу EXPLAIN — ты делаешь неправильно.
Текстом ловим только то, чего в плане нет (см. §4, текстовые теги по `query_text`).

## 2. Обход дерева: цепочка родителей обязательна

Тег вешается не на узел, а на узел **в контексте**. Обходи дерево, передавая вниз
список типов родителей (эталон — `_walk` в `plan_tagger.py`):

```python
stack = [(root, 0, [])]
while stack:
    n, depth, parents = stack.pop()
    yield n, depth, parents
    chain = parents + [n.get("Node Type", "")]
    for child in n.get("Plans", []) or []:
        stack.append((child, depth + 1, chain))
```

### Почему без родителей детектор врёт (реальные случаи, журнал 2026-08-20 16:35)

**Gather Motion N:1.** Сам по себе он ничего не значит. Двухфазная агрегация —
это ПРАВИЛЬНЫЙ план, и выглядит она так:

```
Aggregate                 <- финальная свёртка на координаторе
  Gather Motion 32:1      <- собираются частичные агрегаты (мало строк)
    Aggregate             <- частичный агрегат на каждом сегменте
      Seq Scan
```

Анти-паттерн — когда через тот же Gather протаскивают **сырые строки**
(кейс MR!435: вся ODS стягивалась на один сегмент под `Materialize` для
`Nested Loop`). Отличает их только цепочка родителей: если сверху лежат лишь
`Aggregate`, `Finalize Aggregate`, `GroupAggregate`, `WindowAgg`, `Sort`,
`Limit`, `Unique`, `Result` — Gather легитимен, тег не вешаем.

**Глубина — не критерий.** Ранняя версия ловила Gather по `depth > 0` и дала
29 замечаний, из которых большинство ложные. После добавления родителей осталось
6, все три критических — настоящие.

**NO_WHERE_CLAUSE.** Вешался на `SELECT 1`, `version()`, `current_schema()`
(пинги драйвера) и на `SELECT MAX(...) FROM t` (агрегат по всей таблице — не
«забыли WHERE»). Лечится предусловиями: есть ` FROM `, нет агрегатной функции,
не `pg_*`/`information_schema`.

Вывод общий: **каждый новый детектор проверяй на легитимном плане тоже.**
Ловить анти-паттерн мало — надо не ловить норму.

## 3. Каталог тегов и весов

Источник истины — `TAGS` в `optimisation/tools/plan_tagger.py`. Балл = Σ весов.

| Тег | Вес | Как ловится в JSON |
|---|---|---|
| `NESTED_LOOP_ANTI_JOIN` | 100 | `Node Type`=`Nested Loop` + `Join Type`=`Anti` |
| `DISABLE_COST` | 100 | `Total Cost` ≥ 1e10 — взят запрещённый узел |
| `GATHER_MOTION_TO_ONE` | 90 | `Senders`>1, `Receivers`=1, родители НЕ свёртка |
| `IS_NOT_DISTINCT_FROM` | 80 | текст запроса (в плане не виден) |
| `PLANNER_FALLBACK` | 70 | `Optimizer` != GPORCA |
| `NESTED_LOOP_LARGE` | 60 | `Nested Loop` при `Plan Rows` ≥ порога |
| `CARTESIAN` | 55 | `Nested Loop` без Join/Hash/Merge/Index/Recheck Cond |
| `FULL_PARTITION_SCAN` | 50 | `Partitions Selected` = `Partitions Total` > 1 |
| `BROADCAST_LARGE` | 45 | `Broadcast Motion` большой ветки |
| `REDISTRIBUTE_LARGE` | 40 | `Redistribute Motion` большой ветки |
| `SPILL_RISK` | 35 | `Hash`: `Plan Rows` × `Plan Width` > `work_mem` |
| `SEQ_SCAN_LARGE` | 25 | `Seq Scan` при `Plan Rows` ≥ порога |
| `NO_WHERE_CLAUSE` | 20 | текст: есть FROM, нет WHERE/JOIN/агрегата |
| `MANY_JOINS` | 15 | джойнов > 10 |
| `SELECT_STAR` | 10 | текст запроса |
| `WIDE_ROW` | 10 | `Plan Width` ≥ 2000 |

Пороги (`large_rows`, `work_mem_bytes`, `many_joins`) держи в `THRESHOLDS`,
не хардкодь в SQL: на проде и тесте они разные. `work_mem_bytes` сверяй
с `SHOW work_mem`.

## 4. Три честных ограничения метода

Проговаривай их в любом отчёте — иначе выводам верят больше, чем они стоят.

1. **`query_text` обрезан.** `pg_stat_activity.query` режется по
   `track_activity_query_size` (дефолт 1024 байта). Наши `stg_to_ods` на 118 колонок
   туда не влезают — в снапшоте огрызок, он не распарсится и не пойдёт в EXPLAIN.
   Проверь `SHOW track_activity_query_size`; 1024 → просить DBA поднять до 8192+
   (нужен рестарт). До этого длинные запросы систематически выпадают из аудита.
2. **Снапшот раз в 5 минут видит не всё.** Запрос на 3 секунды попадёт в срез
   только по везению. Быстрые запросы недопредставлены, длинные переоценены.
   `count(*)` по снапшотам **не является частотой запуска** — честная частота
   только из `pg_stat_statements`.
3. **EXPLAIN офлайн ≠ план на проде.** Наша сессия отличается по `search_path`,
   GUC (`optimizer`, `enable_*`, `work_mem`), временным таблицам. План —
   **сигнал, а не приговор**: верхушку рейтинга перепроверяй руками.

Плюс четвёртое, техническое: **EXPLAIN без свежей статистики врёт** — покажет
`Plan Rows=1` там, где миллионы, и рейтинг будет построен на вымысле. Порядок:
зонд `stale_stats` по `pg_stat_all_tables` → точечный `ANALYZE` по согласованию
с DBA в окно низкой нагрузки → только потом EXPLAIN. Если ANALYZE пропущен —
объёмозависимые теги (`SEQ_SCAN_LARGE`, `SPILL_RISK`) читай с поправкой;
структурные (`NESTED_LOOP_ANTI_JOIN`, `GATHER_MOTION_TO_ONE`, `PLANNER_FALLBACK`)
от статистики почти не зависят и надёжны всегда.

## 5. Предохранители при сборе на живом кластере

- **Только `EXPLAIN` без `ANALYZE`.** `EXPLAIN` планирует, запрос НЕ выполняется —
  риск близок к нулю. `EXPLAIN ANALYZE` выполняет запрос целиком: часовой
  `Nested Loop` из MR!435 честно отработает час. Никогда не запускай его веером.
- Read-only транзакция + `statement_timeout` 5–10 с (`--timeout-ms 10000`).
- Троттлинг: `--sleep-ms 200` между планами. Нагрузка ложится на **координатор**.
- `--max-active 20`: каждые 10 планов смотрим `pg_stat_activity`, активных больше —
  сбор останавливается. Проверено: при `--max-active 1` прервалось на 10-м из 30.
- `--limit 300` — потолок классов за прогон.
- Отдельная сессия с `application_name='plan_audit'`, чтобы аудит не попал
  в собственный снапшот.
- Фильтруй: только `SELECT` / `INSERT ... SELECT` / `WITH`; обрезанные тексты
  отбрасывай сразу — это шум.

## 6. Не пиши это заново — инструменты готовы

| Инструмент | Что делает |
|---|---|
| `optimisation/tools/plan_tagger.py` | разбор JSON → теги, балл, объяснение. В БД не ходит. `--selftest` воспроизводит MR!435 |
| `optimisation/tools/plan_audit_collect.py` | сбор: разведка, нормализация, fingerprint, EXPLAIN, отчёт в `generated/plan_audit/` |
| `optimisation/tools/plan_audit_probe.sql` | этап 0, read-only разведка |
| `optimisation/tools/plan_audit_report.py` | пересчёт тегов по `plans.json` без обращения к кластеру |
| `optimisation/PLAN_plan_audit.md` | модель данных, ограничения, этапы |

```bash
python3 plan_tagger.py --selftest                 # проверить логику тегов офлайн
python3 plan_tagger.py plan.json --query "$SQL"   # разобрать один план
python3 plan_audit_collect.py --probe             # разведка, ничего не планирует
python3 plan_audit_collect.py --days 3 --limit 30 --sleep-ms 500 --max-active 10
```

Новый детектор — это функция `_tag_*` в `plan_tagger.py` плюс запись в `TAGS`
и кейс в `_selftest`, а не новый скрипт.

## 7. Чек-лист

- [ ] План снят как `EXPLAIN (FORMAT JSON)`, регулярок по тексту плана нет.
- [ ] Обход дерева идёт по `Plans[]` с передачей цепочки родителей.
- [ ] Каждый контекстно-зависимый тег (Gather, Motion) проверен на легитимном
      плане — двухфазная агрегация НЕ помечена.
- [ ] Текстовые теги имеют предусловия (FROM / не агрегат / не `pg_*`).
- [ ] Пороги в `THRESHOLDS`, `work_mem` сверен с `SHOW work_mem`.
- [ ] `EXPLAIN ANALYZE` не используется; включены sleep-ms, max-active, timeout.
- [ ] Свежесть статистики проверена, объёмозависимые теги читаются с поправкой.
- [ ] Три ограничения (обрезка текста, интервал снапшота, офлайн-план) написаны
      в отчёте явно.
- [ ] Верхушка рейтинга перепроверена руками.
- [ ] `--selftest` зелёный после любой правки логики.
- [ ] Строка добавлена в `work_journal.md`.

Ключ реестра: MST-20
Журнал применений: mastery/usage_log.csv — после каждого применения добавь строку (см. .context/AGENTS.md §4).
