"""
Telegram-бот «Задачник по программированию» (aiogram 3.x + SQLite).

Что умеет:
  • Администратор (ADMIN_ID) через /admin добавляет, просматривает и удаляет задачи.
  • Ученица через /tasks выбирает задачу, получает условие и присылает код на Python.
  • Бот запускает код в отдельном процессе (с таймаутом и ограничениями ресурсов),
    подаёт на вход test_input и сравнивает вывод с test_output.

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC..."     # токен от @BotFather
    export ADMIN_ID="123456789"          # ваш Telegram ID (узнать: @userinfobot)
    python bot.py
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ======================================================================
# 1. НАСТРОЙКИ
# ======================================================================

# Токен бота (берётся из переменной окружения, либо впишите прямо сюда).
BOT_TOKEN = os.getenv("BOT_TOKEN", "8859902505:AAFyWVwFWlNbZI-dCY3gRwfUF4mnE1Re354")

# >>> ВАШ TELEGRAM ID (число). Только этот пользователь увидит /admin. <<<
# Узнать ID можно у бота @userinfobot. Можно задать переменной окружения ADMIN_ID.
ADMIN_ID = int(os.getenv("ADMIN_ID", "1120524913"))

# Файл базы данных SQLite. Лежит на диске, поэтому задачи переживают перезапуск.
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).with_name("tasks.db")))

# Ограничения «песочницы»
RUN_TIMEOUT = 5                 # секунд на выполнение кода
MAX_OUTPUT_BYTES = 20_000       # максимум байт вывода (stdout/stderr)
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024  # лимит памяти (только Linux/macOS)
MAX_PARALLEL_RUNS = 3           # сколько проверок одновременно

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("task-bot")

try:
    from theory import THEORY  # краткая теория: файл theory.py лежит рядом с bot.py
except ImportError:
    THEORY = []
    log.warning("Файл theory.py не найден — раздел «Теория» будет пустым.")


# ======================================================================
# 2. БАЗА ДАННЫХ (SQLite)
# ======================================================================
# Запросы очень лёгкие, поэтому используем встроенный sqlite3 без доп. библиотек.
#   tasks    — задачи (как раньше; уже существующие задачи не затрагиваются)
#   users    — ученики, которые пользовались ботом
#   progress — успехи: сколько было попыток и решена ли задача
# Новые таблицы создаются автоматически при запуске.

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # доступ к полям по имени: row["title"]
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                description TEXT NOT NULL,
                test_input  TEXT NOT NULL DEFAULT '',
                test_output TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL DEFAULT '',
                username  TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS progress (
                user_id   INTEGER NOT NULL,
                task_id   INTEGER NOT NULL,
                attempts  INTEGER NOT NULL DEFAULT 0,
                solved    INTEGER NOT NULL DEFAULT 0,
                solved_at TEXT,
                PRIMARY KEY (user_id, task_id)
            )
            """
        )


# ---------- задачи ----------

def add_task(title: str, description: str, test_input: str, test_output: str) -> int:
    with closing(db_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, description, test_input, test_output) VALUES (?, ?, ?, ?)",
            (title, description, test_input, test_output),
        )
        return cur.lastrowid


def get_tasks() -> list[sqlite3.Row]:
    with closing(db_connect()) as conn:
        return conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()


def get_task(task_id: int) -> sqlite3.Row | None:
    with closing(db_connect()) as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def delete_task(task_id: int) -> bool:
    with closing(db_connect()) as conn, conn:
        conn.execute("DELETE FROM progress WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0


# ---------- ученики и прогресс ----------

def save_user(user) -> None:
    """Запоминает (или обновляет) имя ученика — нужно для экрана «Успехи»."""
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO users (user_id, full_name, username) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name, username = excluded.username
            """,
            (user.id, user.full_name or "", user.username or ""),
        )


def record_attempt(user_id: int, task_id: int, solved: bool) -> int:
    """Записывает попытку сдачи. Решённая задача остаётся решённой навсегда.
    Возвращает общее число попыток по этой задаче."""
    now = datetime.now().isoformat(timespec="seconds")
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO progress (user_id, task_id, attempts, solved, solved_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(user_id, task_id) DO UPDATE SET
                attempts  = attempts + 1,
                solved    = MAX(solved, excluded.solved),
                solved_at = COALESCE(solved_at, excluded.solved_at)
            """,
            (user_id, task_id, int(solved), now if solved else None),
        )
        row = conn.execute(
            "SELECT attempts FROM progress WHERE user_id = ? AND task_id = ?", (user_id, task_id)
        ).fetchone()
        return row["attempts"]


def get_progress(user_id: int) -> dict[int, sqlite3.Row]:
    """Прогресс ученика: {id задачи: строка с полями attempts, solved}."""
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT * FROM progress WHERE user_id = ?", (user_id,)).fetchall()
    return {r["task_id"]: r for r in rows}


def get_students() -> list[sqlite3.Row]:
    """Все ученики (кроме администратора) с числом попыток."""
    with closing(db_connect()) as conn:
        return conn.execute(
            """
            SELECT u.user_id, u.full_name, u.username,
                   COALESCE(SUM(p.attempts), 0) AS attempts
            FROM users u LEFT JOIN progress p ON p.user_id = u.user_id
            WHERE u.user_id != ?
            GROUP BY u.user_id
            ORDER BY u.full_name
            """,
            (ADMIN_ID,),
        ).fetchall()


# ======================================================================
# 3. ПЕСОЧНИЦА: безопасный запуск кода ученицы
# ======================================================================
# ВАЖНО: это «лёгкая» изоляция (отдельный процесс, таймаут, лимиты, пустое окружение).
# Это НЕ полноценная защита от злонамеренного кода. Для публичного бота запускайте
# весь бот в Docker / под отдельным пользователем без доступа к важным файлам.

@dataclass
class RunResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    output_too_long: bool = False


def _limit_resources() -> None:
    """Выполняется в дочернем процессе перед запуском кода (только POSIX)."""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (RUN_TIMEOUT + 1, RUN_TIMEOUT + 1))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))  # файлы ≤ 1 МБ


async def _read_limited(stream: asyncio.StreamReader, limit: int, proc) -> tuple[bytes, bool]:
    """Читает поток, но не больше limit байт. При превышении сразу убивает процесс
    (защита от бесконечного print, который иначе забил бы память и пайпы)."""
    data = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(data), False
        data += chunk
        if len(data) > limit:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return bytes(data[:limit]), True


_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_PARALLEL_RUNS)
    return _semaphore


async def run_python_code(code: str, stdin_data: str) -> RunResult:
    """Сохраняет код во временный файл и запускает его в отдельном процессе."""
    async with _get_semaphore():
        with tempfile.TemporaryDirectory(prefix="sandbox_") as tmp:
            script = Path(tmp) / "solution.py"
            script.write_text(code, encoding="utf-8")

            # Пустое окружение: код не увидит BOT_TOKEN и другие секреты из env.
            env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PATH": os.defpath}
            if os.name == "nt":
                env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")

            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(script),  # -I: изолированный режим Python
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmp,
                env=env,
                preexec_fn=_limit_resources if os.name == "posix" else None,
            )

            result = RunResult()
            stdin_bytes = stdin_data.encode("utf-8")
            if stdin_bytes and not stdin_bytes.endswith(b"\n"):
                stdin_bytes += b"\n"

            async def communicate() -> tuple[bytes, bytes]:
                # 1) подаём test_input на стандартный ввод
                try:
                    proc.stdin.write(stdin_bytes)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # программа завершилась, не дочитав ввод — это нормально
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                # 2) читаем вывод с ограничением размера
                (out, out_over), (err, err_over) = await asyncio.gather(
                    _read_limited(proc.stdout, MAX_OUTPUT_BYTES, proc),
                    _read_limited(proc.stderr, MAX_OUTPUT_BYTES, proc),
                )
                result.output_too_long = out_over or err_over
                await proc.wait()
                return out, err

            try:
                out, err = await asyncio.wait_for(communicate(), timeout=RUN_TIMEOUT)
                result.stdout = out.decode("utf-8", errors="replace")
                # прячем временный путь из трейсбека
                result.stderr = err.decode("utf-8", errors="replace").replace(str(script), "solution.py")
            except asyncio.TimeoutError:
                result.timed_out = True
            finally:
                # если процесс ещё жив (таймаут / слишком большой вывод) — убиваем
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                result.returncode = proc.returncode
            return result


def normalize(text: str) -> str:
    """Игнорируем различия в \\r\\n, пробелах в конце строк и пустых строках в конце."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.strip().split("\n"))


def extract_code(text: str) -> str:
    """Если код прислан внутри ```...```, достаём только его."""
    match = re.search(r"```(?:python|py)?[ \t]*\n(.*?)```", text, re.S | re.I)
    return (match.group(1) if match else text).strip("\n")


def short(text: str, limit: int = 800) -> str:
    """Обрезает длинный текст, чтобы влезть в лимит Telegram (4096 символов)."""
    return text if len(text) <= limit else text[:limit] + "\n… (обрезано)"


def pre(text: str, limit: int = 800) -> str:
    return f"<pre>{html.escape(short(text, limit) or '(пусто)')}</pre>"


async def check_solution(code: str, task: sqlite3.Row) -> tuple[bool, str]:
    """Запускает код на тесте задачи. Возвращает (решено ли, HTML-сообщение)."""
    res = await run_python_code(code, task["test_input"])

    if res.timed_out:
        return False, f"⏱ Превышено время выполнения ({RUN_TIMEOUT} сек.). Возможно, бесконечный цикл."
    if res.output_too_long:
        return False, "📛 Слишком большой вывод. Проверь, нет ли бесконечного print в цикле."
    if res.returncode != 0:
        return False, f"❌ Ошибка выполнения программы:\n{pre(res.stderr)}"

    if normalize(res.stdout) == normalize(task["test_output"]):
        return True, "✅ Задача решена верно! Тесты пройдены."

    return False, (
        "❌ Результат не совпал с ожидаемым.\n\n"
        f"<b>Входные данные:</b>\n{pre(task['test_input'])}"
        f"<b>Ожидалось:</b>\n{pre(task['test_output'])}"
        f"<b>Получено:</b>\n{pre(res.stdout)}"
    )


# ======================================================================
# 4. СОСТОЯНИЯ (FSM) И КЛАВИАТУРЫ
# ======================================================================

class AddTask(StatesGroup):
    """Пошаговый диалог добавления задачи администратором."""
    title = State()
    description = State()
    test_input = State()
    test_output = State()


class Solving(StatesGroup):
    """Что делает ученица после выбора задачи."""
    waiting_code = State()   # режим «сдача»: следующий присланный код будет проверен
    trial_code = State()     # режим «пробный запуск»: ждём код программы
    trial_input = State()    # пробный запуск: ждём данные для input()


def admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить задачу", callback_data="adm:add")
    kb.button(text="📋 Список задач", callback_data="adm:list")
    kb.button(text="🗑 Удалить задачу", callback_data="adm:delete")
    kb.button(text="📊 Успехи учеников", callback_data="adm:stats")
    kb.adjust(1)
    return kb.as_markup()


def tasks_kb(tasks: list[sqlite3.Row], prefix: str, back: str | None = None) -> InlineKeyboardMarkup:
    """(Для админа) кнопка на каждую задачу; callback_data = '<prefix>:<id>'."""
    kb = InlineKeyboardBuilder()
    for t in tasks:
        kb.button(text=f"#{t['id']} {t['title'][:40]}", callback_data=f"{prefix}:{t['id']}")
    if back:
        kb.button(text="◀ Назад", callback_data=back)
    kb.adjust(1)
    return kb.as_markup()


def format_task(task: sqlite3.Row, admin: bool = False) -> str:
    text = f"<b>#{task['id']} {html.escape(task['title'])}</b>\n\n{html.escape(short(task['description'], 2000))}"
    if admin:
        text += (
            f"\n\n<b>Тестовый ввод:</b>\n{pre(task['test_input'])}"
            f"<b>Ожидаемый вывод:</b>\n{pre(task['test_output'])}"
        )
    return text


class IsAdmin(BaseFilter):
    """Пропускает только пользователя с ID = ADMIN_ID."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id == ADMIN_ID


# ---------- список задач для ученицы с отметками ----------

def status_icon(p: sqlite3.Row | None) -> str:
    """✅ решена · 🔸 были попытки · ⬜ ещё не пробовала."""
    if p is not None and p["solved"]:
        return "✅"
    if p is not None and p["attempts"] > 0:
        return "🔸"
    return "⬜"


def tasks_overview(tasks: list[sqlite3.Row], progress: dict[int, sqlite3.Row]) -> tuple[str, InlineKeyboardMarkup]:
    """Текст со списком задач и сетка кнопок-номеров."""
    solved = sum(1 for t in tasks if progress.get(t["id"]) is not None and progress[t["id"]]["solved"])
    lines = [f"{status_icon(progress.get(t['id']))} {t['id']}. {html.escape(t['title'][:40])}" for t in tasks]
    header = f"📚 <b>Задачи</b> — решено {solved} из {len(tasks)}\n✅ решена · 🔸 есть попытки · ⬜ новая\n\n"
    footer = "\n\nНажми на номер задачи 👇"
    text = header + "\n".join(lines) + footer
    if len(text) > 4000:  # очень длинный список: оставляем только номера
        text = header + "\n".join(f"{status_icon(progress.get(t['id']))} {t['id']}" for t in tasks) + footer

    kb = InlineKeyboardBuilder()
    shown = tasks[:99]  # Telegram разрешает до 100 кнопок (одна занята под «Теорию»)
    for t in shown:
        kb.button(text=f"{status_icon(progress.get(t['id']))} {t['id']}", callback_data=f"task:{t['id']}")
    kb.button(text="📖 Теория", callback_data="th:new")
    n = len(shown)
    kb.adjust(*([4] * (n // 4) + ([n % 4] if n % 4 else [])), 1)  # по 4 номера в ряд, «Теория» — отдельной строкой
    return text, kb.as_markup()


async def send_tasks_list(message: Message, user: User) -> None:
    """Отправляет ученице список задач с её отметками."""
    save_user(user)
    tasks = get_tasks()
    if not tasks:
        await message.answer("Пока нет ни одной задачи. Загляни позже 🙂")
        return
    text, kb = tasks_overview(tasks, get_progress(user.id))
    await message.answer(text, reply_markup=kb)


# ---------- теория ----------

THEORY_LIST_TEXT = "📖 <b>Теория</b> — выбери тему:"


def render_theory(text: str) -> str:
    """Превращает простую разметку из theory.py в HTML для Telegram:
    ```блок кода```, `код`, **жирный**. Остальной текст экранируется."""
    out = []
    for i, block in enumerate(text.split("```")):
        if i % 2:  # внутри тройных кавычек — блок кода
            out.append("<pre>" + html.escape(block.strip("\n"), quote=False) + "</pre>")
            continue
        for j, piece in enumerate(re.split(r"`([^`\n]+)`", block)):
            escaped = html.escape(piece, quote=False)
            if j % 2:  # `код` внутри строки
                out.append(f"<code>{escaped}</code>")
            else:
                out.append(re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", escaped))
    # убираем лишние пустые строки вокруг блоков кода
    return "".join(out).replace("</pre>\n", "</pre>").replace("\n<pre>", "<pre>")


def theory_topic_text(index: int) -> str:
    topic = THEORY[index]
    return f"<b>{html.escape(topic['title'])}</b>\n\n{render_theory(topic['text'])}"


def theory_list_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, topic in enumerate(THEORY):
        kb.button(text=topic["title"], callback_data=f"th:{i}")
    kb.adjust(1)
    return kb.as_markup()


def theory_topic_kb(index: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    nav = 0
    if index > 0:
        kb.button(text="◀ Назад", callback_data=f"th:{index - 1}")
        nav += 1
    if index < len(THEORY) - 1:
        kb.button(text="Дальше ▶", callback_data=f"th:{index + 1}")
        nav += 1
    kb.button(text="📚 Все темы", callback_data="th:list")
    kb.adjust(*([nav] if nav else []), 1)
    return kb.as_markup()


# ======================================================================
# 5. ОБЩИЕ КОМАНДЫ (/start, /tasks, /cancel)
# ======================================================================
# Вынесены в отдельный роутер, который подключается первым: так эти команды
# работают даже посреди диалога (например, /cancel во время добавления задачи).

common_router = Router()


@common_router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = "👋 Привет! Я задачник по программированию.\n\n/tasks — список задач\n/theory — краткая теория\n/cancel — отмена"
    if message.from_user.id == ADMIN_ID:
        text += "\n/admin — панель преподавателя"
    await message.answer(text)


@common_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.")


@common_router.message(Command("tasks"))
async def cmd_tasks(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_tasks_list(message, message.from_user)


@common_router.message(Command("theory"))
async def cmd_theory(message: Message) -> None:
    # Состояние намеренно НЕ сбрасываем: можно заглянуть в теорию посреди решения задачи
    if not THEORY:
        await message.answer("Теория пока не добавлена.")
        return
    await message.answer(THEORY_LIST_TEXT, reply_markup=theory_list_kb())


@common_router.callback_query(F.data.startswith("th:"))
async def theory_nav(cb: CallbackQuery) -> None:
    """th:new — новое сообщение со списком тем; th:list — вернуться к списку; th:<номер> — открыть тему."""
    arg = cb.data.split(":")[1]
    if not THEORY:
        await cb.answer("Теория пока не добавлена", show_alert=True)
    elif arg == "new":
        await cb.message.answer(THEORY_LIST_TEXT, reply_markup=theory_list_kb())
        await cb.answer()
    elif arg == "list":
        await cb.message.edit_text(THEORY_LIST_TEXT, reply_markup=theory_list_kb())
        await cb.answer()
    elif arg.isdigit() and int(arg) < len(THEORY):
        index = int(arg)
        await cb.message.edit_text(theory_topic_text(index), reply_markup=theory_topic_kb(index))
        await cb.answer()
    else:
        await cb.answer("Такой темы нет", show_alert=True)


# ======================================================================
# 6. АДМИН-ПАНЕЛЬ
# ======================================================================

admin_router = Router()
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 Панель преподавателя:", reply_markup=admin_menu_kb())


@admin_router.callback_query(F.data == "adm:menu")
async def adm_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("🛠 Панель преподавателя:", reply_markup=admin_menu_kb())
    await cb.answer()


# ---------- Добавление задачи (FSM) ----------

@admin_router.callback_query(F.data == "adm:add")
async def adm_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddTask.title)
    await cb.message.answer("Шаг 1/4. Введите <b>название</b> задачи.\n(/cancel — отмена)")
    await cb.answer()


@admin_router.message(AddTask.title, F.text)
async def add_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await state.set_state(AddTask.description)
    await message.answer("Шаг 2/4. Введите <b>условие</b> задачи.")


@admin_router.message(AddTask.description, F.text)
async def add_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description=message.text.strip())
    await state.set_state(AddTask.test_input)
    await message.answer(
        "Шаг 3/4. Введите <b>тестовый ввод</b> (то, что получит input()).\n"
        "Можно в несколько строк. Если ввод не нужен, отправьте «-»."
    )


@admin_router.message(AddTask.test_input, F.text)
async def add_test_input(message: Message, state: FSMContext) -> None:
    value = "" if message.text.strip() == "-" else message.text
    await state.update_data(test_input=value)
    await state.set_state(AddTask.test_output)
    await message.answer("Шаг 4/4. Введите <b>правильный вывод</b> (то, что должна напечатать программа).")


@admin_router.message(AddTask.test_output, F.text)
async def add_test_output(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = add_task(data["title"], data["description"], data["test_input"], message.text)
    await state.clear()
    await message.answer(f"✅ Задача #{task_id} «{html.escape(data['title'])}» сохранена.", reply_markup=admin_menu_kb())


# ---------- Список, просмотр, удаление ----------

@admin_router.callback_query(F.data == "adm:list")
async def adm_list(cb: CallbackQuery) -> None:
    tasks = get_tasks()
    if not tasks:
        await cb.message.edit_text("Задач пока нет.", reply_markup=admin_menu_kb())
    else:
        await cb.message.edit_text("📋 Все задачи (нажмите, чтобы открыть):",
                                   reply_markup=tasks_kb(tasks, "adm:view", back="adm:menu"))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:view:"))
async def adm_view(cb: CallbackQuery) -> None:
    task = get_task(int(cb.data.split(":")[2]))
    if not task:
        await cb.answer("Задача не найдена", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить", callback_data=f"adm:delask:{task['id']}")
    kb.button(text="◀ К списку", callback_data="adm:list")
    kb.adjust(2)
    await cb.message.edit_text(format_task(task, admin=True), reply_markup=kb.as_markup())
    await cb.answer()


@admin_router.callback_query(F.data == "adm:delete")
async def adm_delete(cb: CallbackQuery) -> None:
    tasks = get_tasks()
    if not tasks:
        await cb.message.edit_text("Удалять нечего — задач нет.", reply_markup=admin_menu_kb())
    else:
        await cb.message.edit_text("🗑 Какую задачу удалить?",
                                   reply_markup=tasks_kb(tasks, "adm:delask", back="adm:menu"))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:delask:"))
async def adm_delete_ask(cb: CallbackQuery) -> None:
    task = get_task(int(cb.data.split(":")[2]))
    if not task:
        await cb.answer("Задача не найдена", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"adm:delyes:{task['id']}")
    kb.button(text="❌ Отмена", callback_data="adm:menu")
    kb.adjust(2)
    await cb.message.edit_text(f"Удалить задачу #{task['id']} «{html.escape(task['title'])}»?",
                               reply_markup=kb.as_markup())
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:delyes:"))
async def adm_delete_confirm(cb: CallbackQuery) -> None:
    ok = delete_task(int(cb.data.split(":")[2]))
    await cb.message.edit_text("🗑 Задача удалена." if ok else "Задача уже была удалена.",
                               reply_markup=admin_menu_kb())
    await cb.answer()

# ---------- Успехи учеников ----------

@admin_router.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery) -> None:
    students = get_students()
    total = len(get_tasks())
    if not students:
        text = "📊 Пока никто из учеников не пользовался ботом."
    else:
        text = "📊 <b>Успехи учеников</b>"
        for s in students:
            progress = get_progress(s["user_id"])
            done = sorted(t for t, p in progress.items() if p["solved"])
            trying = sorted(t for t, p in progress.items() if not p["solved"] and p["attempts"])
            name = html.escape(s["full_name"] or "Без имени")
            if s["username"]:
                name += f" (@{html.escape(s['username'])})"
            block = f"\n\n<b>{name}</b>\nРешено: {len(done)} из {total}, всего попыток: {s['attempts']}"
            if done:
                block += "\n✅ " + ", ".join(map(str, done))
            if trying:
                block += "\n🔸 " + ", ".join(map(str, trying))
            if len(text) + len(block) > 3800:  # не превышаем лимит Telegram
                text += "\n\n… (остальные не поместились)"
                break
            text += block
    kb = InlineKeyboardBuilder()
    kb.button(text="◀ Назад", callback_data="adm:menu")
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()


# ======================================================================
# 7. ИНТЕРФЕЙС УЧЕНИЦЫ
# ======================================================================
# Схема работы:
#   /tasks → номер задачи → условие и две кнопки:
#       🧪 Пробный запуск — можно запускать программу сколько угодно, без проверки;
#                           потом кнопка «Сдать это решение» (копировать код не нужно);
#       📤 Сдать решение  — присланный код проверяется, попытка записывается.

user_router = Router()

INPUT_PROMPT = (
    "⌨️ В программе есть <code>input()</code>. Пришли данные, которые нужно ввести: "
    "каждое значение с новой строки (как будто после каждого значения нажимаешь Enter).\n"
    "Если ввод не нужен — отправь «-»."
)


def task_actions_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧪 Пробный запуск", callback_data=f"try:{task_id}")
    kb.button(text="📤 Сдать решение", callback_data=f"submit:{task_id}")
    kb.button(text="📖 Теория", callback_data="th:new")
    kb.button(text="📋 К списку задач", callback_data="tasks:list")
    kb.adjust(2, 2)
    return kb.as_markup()


def trial_kb(has_input: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_input:
        kb.button(text="🔁 Другой ввод", callback_data="trial:again")
    kb.button(text="📤 Сдать это решение", callback_data="trial:submit")
    kb.button(text="📋 К списку задач", callback_data="tasks:list")
    kb.adjust(1)
    return kb.as_markup()


def after_fail_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧪 Потренироваться", callback_data=f"try:{task_id}")
    kb.button(text="📖 Теория", callback_data="th:new")
    kb.button(text="📋 К списку задач", callback_data="tasks:list")
    kb.adjust(2, 1)
    return kb.as_markup()


def after_success_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 К списку задач", callback_data="tasks:list")
    return kb.as_markup()


def format_trial(res: RunResult) -> str:
    """Результат пробного запуска: вывод программы и/или ошибка (без проверки)."""
    if res.timed_out:
        return (f"⏱ Программа работала дольше {RUN_TIMEOUT} секунд — возможно, где-то бесконечный цикл. "
                "Проверь условие цикла.")
    if res.output_too_long:
        return "📛 Программа вывела слишком много текста. Нет ли бесконечного print в цикле?"
    parts = []
    if res.stdout.strip():
        parts.append(f"<b>Вывод программы:</b>\n{pre(res.stdout, 1500)}")
    if res.returncode != 0 or res.stderr.strip():
        hint = ""
        if "EOFError" in res.stderr:
            hint = ("\n💡 Программа ждёт больше данных, чем ты ввёл. Нажми «Другой ввод» "
                    "и проверь, что строк хватает для всех input().")
        parts.append(f"<b>Ошибка:</b>\n{pre(res.stderr, 1200)}{hint}")
    if not parts:
        parts.append("Программа завершилась, но ничего не вывела. Чтобы увидеть результат, используй print().")
    return "🧪 <b>Результат пробного запуска</b>\n\n" + "\n".join(parts)


async def run_trial(message: Message, code: str, stdin_data: str) -> None:
    """Пробный запуск: никаких проверок и записи попыток."""
    wait = await message.answer("⏳ Запускаю…")
    try:
        res = await run_python_code(code, stdin_data)
        text = format_trial(res)
    except Exception:
        log.exception("Ошибка при пробном запуске")
        await wait.edit_text("⚠️ Не получилось запустить программу. Попробуй ещё раз.")
        return
    text += "\n\n✏️ Хочешь изменить программу — пришли новый код. Готово — жми «Сдать это решение»."
    await wait.edit_text(text, reply_markup=trial_kb("input(" in code))


async def submit_solution(message: Message, user: User, task: sqlite3.Row, code: str, state: FSMContext) -> None:
    """Проверка решения на тестах задачи + запись попытки в прогресс."""
    save_user(user)
    wait = await message.answer("⏳ Запускаю тесты…")
    try:
        solved, report = await check_solution(code, task)
    except Exception:
        log.exception("Ошибка при проверке решения")
        await wait.edit_text("⚠️ Внутренняя ошибка проверки. Скажи преподавателю.")
        return

    attempts = record_attempt(user.id, task["id"], solved)
    if solved:
        await state.clear()  # решено — выходим из режима ожидания кода
        await wait.edit_text(f"{report}\n\n🎉 Попыток на эту задачу: {attempts}", reply_markup=after_success_kb())
    else:
        # остаёмся в режиме сдачи: можно просто прислать исправленный код
        await state.set_state(Solving.waiting_code)
        await state.update_data(task_id=task["id"])
        await wait.edit_text(
            f"{report}\n\n📝 Попытка №{attempts} записана. Ошибаться — это нормально, так и учатся! "
            "Исправь код и пришли ещё раз 💪",
            reply_markup=after_fail_kb(task["id"]),
        )


@user_router.message(Command("admin"))
async def admin_denied(message: Message) -> None:
    # Сюда попадают только не-админы (для админа сработал admin_router).
    await message.answer("⛔ Эта команда доступна только преподавателю.")


@user_router.callback_query(F.data == "tasks:list")
async def back_to_list(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await send_tasks_list(cb.message, cb.from_user)
    await cb.answer()


@user_router.callback_query(F.data.startswith("task:"))
async def choose_task(cb: CallbackQuery, state: FSMContext) -> None:
    task = get_task(int(cb.data.split(":")[1]))
    if not task:
        await cb.answer("Эта задача уже удалена", show_alert=True)
        return
    await state.clear()
    await state.update_data(task_id=task["id"])

    p = get_progress(cb.from_user.id).get(task["id"])
    note = ""
    if p is not None and p["solved"]:
        note = f"\n\n✅ Эта задача уже решена (попыток: {p['attempts']}). Можно решить ещё раз для практики."
    elif p is not None and p["attempts"]:
        note = f"\n\n🔸 Попыток уже: {p['attempts']}. Ты на правильном пути!"

    await cb.message.answer(
        format_task(task) + note
        + "\n\nПиши программу на Python: данные читай через <code>input()</code>, результат выводи через <code>print()</code>."
          "\nСначала можно потренироваться, а потом сдать 👇",
        reply_markup=task_actions_kb(task["id"]),
    )
    await cb.answer()


# ---------- Пробный запуск ----------

@user_router.callback_query(F.data.startswith("try:"))
async def start_trial(cb: CallbackQuery, state: FSMContext) -> None:
    task = get_task(int(cb.data.split(":")[1]))
    if not task:
        await cb.answer("Эта задача уже удалена", show_alert=True)
        return
    await state.set_state(Solving.trial_code)
    await state.update_data(task_id=task["id"])
    await cb.message.answer(
        "🧪 <b>Пробный запуск</b>\n"
        "Пришли код программы одним сообщением — я запущу её и покажу, что получилось. "
        "Здесь ничего не проверяется и не считается ошибкой, пробуй сколько хочешь!\n"
        "Когда программа будет готова, нажми «Сдать это решение»."
    )
    await cb.answer()


@user_router.message(Solving.trial_code, F.text, ~F.text.startswith("/"))
async def trial_receive_code(message: Message, state: FSMContext) -> None:
    code = extract_code(message.text)
    await state.update_data(last_code=code)
    if "input(" in code:
        await state.set_state(Solving.trial_input)
        await message.answer(INPUT_PROMPT)
    else:
        await state.set_state(Solving.trial_code)
        await run_trial(message, code, "")


@user_router.message(Solving.trial_input, F.text, ~F.text.startswith("/"))
async def trial_receive_input(message: Message, state: FSMContext) -> None:
    # Если вместо данных прислали новый код — запускаем его как новый код
    if "print(" in message.text or "input(" in message.text:
        await trial_receive_code(message, state)
        return
    code = (await state.get_data()).get("last_code")
    if not code:
        await state.set_state(Solving.trial_code)
        await message.answer("Сначала пришли код программы 🙂")
        return
    stdin_data = "" if message.text.strip() == "-" else message.text
    await state.set_state(Solving.trial_code)
    await run_trial(message, code, stdin_data)


@user_router.callback_query(F.data == "trial:again")
async def trial_again(cb: CallbackQuery, state: FSMContext) -> None:
    if not (await state.get_data()).get("last_code"):
        await cb.answer("Сначала пришли код. Если бот перезапускался — выбери задачу заново: /tasks", show_alert=True)
        return
    await state.set_state(Solving.trial_input)
    await cb.message.answer(INPUT_PROMPT)
    await cb.answer()


@user_router.callback_query(F.data == "trial:submit")
async def trial_submit(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    task = get_task(data["task_id"]) if data.get("task_id") else None
    code = data.get("last_code")
    if not task or not code:
        await cb.answer("Сессия закончилась. Выбери задачу заново: /tasks", show_alert=True)
        return
    await cb.answer()
    await submit_solution(cb.message, cb.from_user, task, code, state)


# ---------- Сдача решения ----------

@user_router.callback_query(F.data.startswith("submit:"))
async def start_submit(cb: CallbackQuery, state: FSMContext) -> None:
    task = get_task(int(cb.data.split(":")[1]))
    if not task:
        await cb.answer("Эта задача уже удалена", show_alert=True)
        return
    await state.set_state(Solving.waiting_code)
    await state.update_data(task_id=task["id"])
    await cb.message.answer(
        "📤 <b>Сдача решения</b>\n"
        "Пришли код одним сообщением — я проверю его на тестах. "
        "Каждая попытка записывается, а ошибаться можно сколько угодно 🙂"
    )
    await cb.answer()


@user_router.message(Solving.waiting_code, F.text, ~F.text.startswith("/"))
async def receive_code(message: Message, state: FSMContext) -> None:
    task_id = (await state.get_data()).get("task_id")
    task = get_task(task_id) if task_id else None
    if not task:
        await state.clear()
        await message.answer("Эта задача была удалена. Выбери другую: /tasks")
        return
    await submit_solution(message, message.from_user, task, extract_code(message.text), state)


@user_router.message(F.text, ~F.text.startswith("/"))
async def fallback(message: Message) -> None:
    await message.answer("Сначала выбери задачу: /tasks")


# ======================================================================
# 8. ЗАПУСК
# ======================================================================

async def main() -> None:
    if "ВСТАВЬТЕ" in BOT_TOKEN:
        raise SystemExit("Укажите токен бота в переменной окружения BOT_TOKEN (см. README.md).")
    if ADMIN_ID == 0:
        log.warning("ADMIN_ID не задан — админ-панель никому не будет доступна!")

    init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()  # по умолчанию состояния FSM хранятся в памяти
    # Порядок важен: сначала общие команды, потом админ, потом ученица
    dp.include_routers(common_router, admin_router, user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен. БД: %s", DB_PATH)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
