#!/usr/bin/env python3
"""Telegram bot that runs docker compose projects on this server: status, update, logs, restart.

One install per server, each with its own bot token (Telegram lets only one process poll a token)
and its own INI config (projects.example.ini); a server may hold several independent projects.
Standard library only. It runs on the host as a systemd service (deploybot.service), so it
calls git and docker compose in each project's folder directly. Only admins may use it, and only
in a private chat: whoever controls docker here controls the whole server."""

from __future__ import annotations

import configparser
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CONFIG = Path(os.environ.get("DEPLOYBOT_CONFIG", "/etc/deploybot/projects.ini"))
STATE = Path(os.environ.get("DEPLOYBOT_STATE", "/var/lib/deploybot/state.json"))  # per-chat view choice
HERE = Path(__file__).resolve().parent
STATUS, UPDATE, LOGS, RESTART = "📊 Состояние", "⬆️ Обновить", "📄 Логи", "🔄 Перезапуск"
DOCKER, HOST, VIEW = "🐳 Docker", "🖥 Сервер", "⚙️ Вид"
KEYBOARD = {"keyboard": [[{"text": t} for t in row] for row in ((STATUS, UPDATE), (LOGS, RESTART), (DOCKER, HOST), (VIEW,))],
            "resize_keyboard": True, "is_persistent": True}
HELP = ("<b>{server}</b>\n\n"
        f"{STATUS} — версии проектов и их контейнеры\n"
        f"{UPDATE} — покажу новые коммиты и обновлю после подтверждения\n"
        f"{LOGS} — логи контейнеров файлом\n{RESTART} — перезапустить приложение\n"
        f"{DOCKER} — все контейнеры сервера и место, которое занимает Docker\n"
        f"{HOST} — процессор, память, диск, кто больше всех ест память\n"
        f"{VIEW} — красиво или как в терминале")
VIEW_NAMES = {"pretty": "Красиво", "raw": "Как в терминале"}
# Bot API 9.4 button colours; older clients just ignore the field.
STYLES = {"do:update": "success", "do:restart": "danger"}


@dataclass
class Project:
    name: str
    dir: Path
    check: list[str] = field(default_factory=list)  # services that must run after an update; empty = all
    restart: list[str] = field(default_factory=list)  # what «Перезапуск» restarts; empty = the whole project
    on_change: list[tuple[str, list[str]]] = field(default_factory=list)  # file changed → extra compose command

    @property
    def is_self(self) -> bool:
        """The bot's own checkout: updated with git only, then the bot restarts."""
        return self.dir.resolve() == HERE


TOKEN, ADMINS, PROJECTS, SERVER = "", set(), {}, "Сервер"
busy: dict[str, threading.Lock] = {}  # per project: one update or restart at a time, projects stay independent


def load(path: Path = CONFIG) -> tuple[str, set[int], dict[str, Project], str]:
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))  # «check = app  # comment» works
    if not cfg.read(path, encoding="utf-8"):
        sys.exit(f"Нет конфига {path}, пример: projects.example.ini")
    bot = cfg["bot"]
    projects = {}
    for name in cfg.sections():
        if name == "bot":
            continue
        if not re.fullmatch(r"[a-z0-9_-]{1,30}", name):
            sys.exit(f"Имя проекта [{name}]: только a-z, 0-9, _ и -, до 30 символов")
        s = cfg[name]
        on_change = []
        for line in s.get("on_change", "").splitlines():
            if line.strip():
                file, _, cmd = line.partition(":")
                on_change.append((file.strip(), cmd.split()))
        projects[name] = Project(name, Path(s["dir"]), s.get("check", "").split(), s.get("restart", "").split(), on_change)
    admins = {int(x) for x in bot.get("admins", "").replace(",", " ").split()}
    if not bot.get("token") or bot["token"].startswith("123456:") or not admins or not projects:
        sys.exit(f"В {path} нужны настоящий [bot] token, admins и хотя бы один проект")
    return bot["token"], admins, projects, bot.get("name", "Сервер")


# ---------- Telegram ----------

def api(method: str, data: dict | None = None, file: tuple[str, bytes] | None = None):
    fields = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
              for k, v in (data or {}).items() if v is not None}
    if file:
        boundary = uuid.uuid4().hex
        body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in fields.items())
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{file[0]}"\r\n'
                 f"Content-Type: text/plain; charset=utf-8\r\n\r\n").encode() + file[1] + f"\r\n--{boundary}--\r\n".encode()
        ctype = f"multipart/form-data; boundary={boundary}"
    else:
        body, ctype = urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": ctype}), timeout=70) as r:
            return json.load(r)["result"]
    except urllib.error.HTTPError as e:  # the message never shows the URL, which holds the token
        raise RuntimeError(f"{method}: {e.read().decode(errors='replace')}") from None


def send(chat: int, text: str, buttons: list[tuple[str, str]] | None = None) -> int:
    if len(text) > 4000:  # cutting HTML would break its tags: the whole text goes as a file instead
        send_file(chat, f"message-{stamp()}.txt", html.unescape(re.sub(r"<[^>]+>", "", text)), "Сообщение длинное, отправил файлом")
        return 0
    markup = {"inline_keyboard": [[dict({"text": t, "callback_data": d}, **({"style": STYLES[d[:d.rfind(":")]]} if d[:d.rfind(":")] in STYLES else {}))]
                                  for t, d in buttons]} if buttons else KEYBOARD
    return api("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "reply_markup": markup})["message_id"]


def edit(chat: int, message_id: int, text: str) -> None:
    api("editMessageText", {"chat_id": chat, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML"})


def send_file(chat: int, name: str, text: str, caption: str = "") -> None:
    api("sendDocument", {"chat_id": chat, "caption": caption[:1000]}, (name, text.encode()))


# ---------- server ----------

def sh(p: Project | None, *cmd: str, timeout: int = 120) -> tuple[bool, str]:
    """Runs a command in the project's folder (None: anywhere); returns (ok, transcript with the command line)."""
    head = f"$ {' '.join(cmd)}\n"
    try:
        r = subprocess.run(cmd, cwd=p.dir if p else None, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{head}прервано: дольше {timeout} с\n"
    except OSError as e:
        return False, f"{head}{e}\n"
    return r.returncode == 0, head + r.stdout + r.stderr


def out(p: Project | None, *cmd: str) -> str:
    return sh(p, *cmd)[1].split("\n", 1)[1].strip()


def containers(p: Project) -> list[dict]:
    """`docker compose ps` as dicts; newer compose prints one JSON per line, older a JSON array."""
    ok, text = sh(p, "docker", "compose", "ps", "--all", "--format", "json")
    text = text.split("\n", 1)[1].strip()
    if not ok or not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def healthy(p: Project) -> bool:
    running = {c.get("Service") for c in containers(p) if c.get("State") == "running"}
    return bool(running) and all(s in running for s in p.check)


def wait_healthy(p: Project, seconds: int = 20) -> bool:
    """A container may start and crash a few seconds later (bad .env, failed migration)."""
    time.sleep(seconds)
    return healthy(p)


def num(x: float) -> str:
    text = f"{x:.0f}" if x >= 100 else f"{x:.1f}".replace(".0", "") if f"{x:.1f}".endswith(".0") else f"{x:.1f}"
    return text.replace(".", ",")


def gb(n: float) -> str:
    return num(n / 2**30)


def duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m} мин {s} с" if m else f"{s} с"


def uptime(seconds: float) -> str:
    d, rest = divmod(int(seconds), 86400)
    h, m = rest // 3600, rest % 3600 // 60
    return f"{d} дн {h} ч" if d else f"{h} ч {m} мин" if h else f"{m} мин"


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M")


_SIZE_UNITS = {"B": "Б", "kB": "КБ", "KiB": "КБ", "MB": "МБ", "MiB": "МБ", "GB": "ГБ", "GiB": "ГБ", "TB": "ТБ", "TiB": "ТБ"}


def ru_size(text: str) -> str:
    """Docker sizes: «212.8MiB» → «213 МБ», «1.2GB (55%)» → «1,2 ГБ (55%)»."""
    return re.sub(r"([\d.]+)\s*(kB|[KMGT]i?B|B)\b", lambda m: f"{num(float(m[1]))} {_SIZE_UNITS.get(m[2], m[2])}", text)


_RU_TIME = [(r"About an? hour", "~1 ч"), (r"About a minute", "~1 мин"), (r"Less than a second", "<1 с"),
            (r"(\d+) seconds?", r"\1 с"), (r"(\d+) minutes?", r"\1 мин"), (r"(\d+) hours?", r"\1 ч"),
            (r"(\d+) days?", r"\1 дн"), (r"(\d+) weeks?", r"\1 нед"), (r"(\d+) months?", r"\1 мес"),
            (r"(\d+) years?", r"\1 г"), (r"\bago\b", "назад")]
_RU_STATUS = [(r"^Up ", "работает "), (r"^Exited \((\d+)\) (.*)", r"остановлен, код \1, \2"),
              (r"^Restarting \((\d+)\).*", r"перезапускается, код \1"), (r"^Created$", "создан, не запущен"),
              (r"\(healthy\)", "· здоров"), (r"\(unhealthy\)", "· нездоров"), (r"\(health: starting\)", "· проверка здоровья"),
              (r"\(Paused\)", "· на паузе")]


def ru_status(text: str) -> str:
    """Docker's English «Up 6 minutes (healthy)» → «работает 6 мин · здоров»; unknown parts pass through."""
    for pattern, repl in _RU_TIME + _RU_STATUS:
        text = re.sub(pattern, repl, text)
    return text


def docker_stats() -> dict[str, str]:
    """Container name → «CPU 0,6% · 213 МБ» (memory limit dropped: without one it is just the server's RAM)."""
    ok, text = sh(None, "docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}")
    stats = {}
    for row in text.splitlines()[1:] if ok else []:
        name, cpu, mem_usage = (row.split("\t") + ["", ""])[:3]
        stats[name] = f"CPU {cpu.replace('.', ',')} · {ru_size(mem_usage.split(' / ')[0])}"
    return stats


def container_line(name: str, state: str, status: str, stats: str = "") -> str:
    icon = ("🟢" if state == "running" and "unhealthy" not in status
            else "⚪" if status.startswith("Exited (0)")  # finished cleanly, e.g. a one-off migration
            else "🟡" if state in ("restarting", "created", "paused") else "🔴")
    return f"{icon} <b>{html.escape(name)}</b> · {html.escape(ru_status(status or state))}" + (f" · {stats}" if stats else "")


def commit_line(p: Project | None, *rev: str) -> str:
    h, _, rest = out(p, "git", "log", "-1", "--format=%h%x09%s%x09%cr", *rev).partition("\t")
    subject, _, when = rest.partition("\t")
    return f"<code>{h}</code> {html.escape(subject)}" + (f" · {ru_status(when)}" if when else "")


def project_status(p: Project, stats: dict[str, str]) -> str:
    sh(p, "git", "fetch", "-q")
    behind = out(p, "git", "rev-list", "--count", "HEAD..@{u}")
    lines = [f"📦 <b>{p.name}</b>", commit_line(p),
             f"⬆️ Новых коммитов: {behind}, нажмите «{UPDATE}»" if behind not in ("", "0") else "✅ Последняя версия"]
    if not p.is_self:
        for c in containers(p) or [{"Service": "?", "State": "?", "Status": "docker compose не ответил"}]:
            lines.append(container_line(c.get("Service", "?"), c.get("State", ""), c.get("Status", ""), stats.get(c.get("Name", ""), "")))
    return "\n".join(lines)


def meminfo() -> dict[str, int]:
    """/proc/meminfo in bytes."""
    rows = (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {k: int(v.split()[0]) * 1024 for k, v in rows}


def bar(fraction: float) -> str:
    n = min(10, round(fraction * 10))
    return "▰" * n + "▱" * (10 - n)


def resources_line() -> str:
    mem, disk = meminfo(), shutil.disk_usage("/")
    used = mem["MemTotal"] - mem["MemAvailable"]
    return f"💾 Память {gb(used)} из {gb(mem['MemTotal'])} ГБ · диск {gb(disk.used)} из {gb(disk.total)} ГБ"


def status_text() -> str:
    stats = docker_stats()
    blocks = [f"🖥 <b>{html.escape(SERVER)}</b>"] + [project_status(p, stats) for p in PROJECTS.values()] + [resources_line()]
    return "\n\n".join(blocks)


def docker_text() -> str:
    ok, text = sh(None, "docker", "ps", "--all", "--format", "json")
    if not ok:
        return f"🐳 Docker не ответил:\n<pre>{html.escape(text[-1500:])}</pre>"
    rows = [json.loads(line) for line in text.splitlines()[1:] if line.startswith("{")]
    stats, groups = docker_stats(), {}
    for c in rows:
        project = re.search(r"com\.docker\.compose\.project=([^,]+)", c.get("Labels", ""))
        groups.setdefault(project[1] if project else "без compose", []).append(c)
    running = sum(c.get("State") == "running" for c in rows)
    lines = [f"🐳 <b>Docker</b> · {html.escape(SERVER)}", f"Работают {running} из {len(rows)}"]
    for name, cs in sorted(groups.items()):
        lines.append(f"\n📦 <b>{html.escape(name)}</b>")
        for c in cs:
            lines.append(container_line(c.get("Names", "?"), c.get("State", ""), c.get("Status", ""), stats.get(c.get("Names", ""), "")))
            lines.append(f"      <i>{html.escape(c.get('Image', ''))}</i>")
    ok, text = sh(None, "docker", "system", "df", "--format", "json")
    names = {"Images": "Образы", "Containers": "Контейнеры", "Local Volumes": "Тома", "Build Cache": "Кэш сборки"}
    df = [json.loads(line) for line in text.splitlines()[1:] if line.startswith("{")] if ok else []
    if df:
        lines.append("\n💽 <b>Docker на диске</b>")
        lines += [f"{names.get(r.get('Type'), r.get('Type'))}: {ru_size(r.get('Size', '?'))}, можно освободить {ru_size(r.get('Reclaimable', '?'))}"
                  for r in df]
    return "\n".join(lines)


def server_text() -> str:
    mem, disk = meminfo(), shutil.disk_usage("/")
    cores, load = os.cpu_count() or 1, os.getloadavg()
    used = mem["MemTotal"] - mem["MemAvailable"]
    lines = [f"🖥 <b>{html.escape(SERVER)}</b> · без перезагрузки {uptime(float(Path('/proc/uptime').read_text().split()[0]))}", "",
             f"⚙️ <b>Процессор</b> {cores} ядер, нагрузка {' · '.join(num(x) for x in load)}"
             + (" — <b>выше числа ядер</b>" if load[0] > cores else ""),
             f"🧠 <b>Память</b> {bar(used / mem['MemTotal'])} {gb(used)} из {gb(mem['MemTotal'])} ГБ"]
    if mem.get("SwapTotal"):
        swap = mem["SwapTotal"] - mem["SwapFree"]
        lines.append(f"🔁 <b>Swap</b> {bar(swap / mem['SwapTotal'])} {gb(swap)} из {gb(mem['SwapTotal'])} ГБ")
    lines.append(f"💽 <b>Диск /</b> {bar(disk.used / disk.total)} {gb(disk.used)} из {gb(disk.total)} ГБ")
    ok, text = sh(None, "ps", "-eo", "rss=,comm=", "--sort=-rss")
    top = [row.split(None, 1) for row in text.splitlines()[1:6]] if ok else []
    if top:
        rows = "\n".join(f"{ru_size(f'{int(rss) / 1024:.1f}MiB')} {html.escape(name)}" for rss, name in top if rss.isdigit())
        lines.append(f"\n<b>Больше всего памяти</b>\n<blockquote expandable>{rows}</blockquote>")
    return "\n".join(lines)


# ---------- «как в терминале»: the commands' own output, nothing added ----------

def pre(*transcripts: str) -> str:
    return "<pre>" + html.escape("\n".join(t.rstrip() for t in transcripts)) + "</pre>"


def head(transcript: str, lines: int) -> str:
    return "\n".join(transcript.splitlines()[:lines + 1])  # + the «$ command» line


def status_raw() -> str:
    parts = []
    for p in PROJECTS.values():
        sh(p, "git", "fetch", "-q")
        parts += [f"# {p.name}: {p.dir}", sh(p, "git", "log", "-1", "--oneline")[1], sh(p, "git", "status", "-sb")[1]]
        if not p.is_self:
            parts.append(sh(p, "docker", "compose", "ps")[1])
        parts.append("")
    return pre(*parts, sh(None, "free", "-h")[1], sh(None, "df", "-h", "/")[1])


def docker_raw() -> str:
    return pre(sh(None, "docker", "ps", "-a")[1], sh(None, "docker", "stats", "--no-stream")[1], sh(None, "docker", "system", "df")[1])


def server_raw() -> str:
    return pre(sh(None, "uptime")[1], sh(None, "free", "-h")[1], sh(None, "df", "-h", "/")[1],
               head(sh(None, "ps", "-eo", "pid,rss,comm", "--sort=-rss")[1], 6))


def view(chat: int) -> str:
    try:
        return json.loads(STATE.read_text()).get(str(chat), "pretty")
    except (OSError, ValueError):
        return "pretty"


def set_view(chat: int, mode: str) -> None:
    try:
        data = json.loads(STATE.read_text())
    except (OSError, ValueError):
        data = {}
    data[str(chat)] = mode
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(STATE)  # atomic: a crash never leaves half a file


def ask_view(chat: int) -> None:
    now = view(chat)
    send(chat, f"Как показывать ответы? Сейчас: {VIEW_NAMES[now]}.\n\n«Как в терминале» — вывод самих команд "
               "(<code>docker ps -a</code>, <code>free -h</code>…) без украшений.",
         [(("✓ " if k == now else "") + name, f"view:{k}") for k, name in VIEW_NAMES.items()])


def logs_text(p: Project, tail: int = 2000) -> str:
    return sh(p, "docker", "compose", "logs", "--no-color", "--timestamps", f"--tail={tail}", timeout=60)[1]


# ---------- actions ----------

def show_logs(chat: int, p: Project) -> None:
    text = logs_text(p)
    errors = sum(1 for line in text.splitlines() if "ERROR" in line or "Traceback" in line)
    send_file(chat, f"{p.name}-logs-{stamp()}.txt", text, f"{p.name}: последние 2000 строк каждого контейнера. Строк с ошибками: {errors}")


def ask_update(chat: int, p: Project) -> None:
    sh(p, "git", "fetch", "-q")
    ok, log = sh(p, "git", "log", "--oneline", "HEAD..@{u}")
    commits = log.splitlines()[1:] if ok else []
    if commits and view(chat) == "raw":
        send(chat, pre(log), [("Обновить", f"do:update:{p.name}"), ("Отмена", "cancel")])
    elif commits:
        rows = "\n".join(f"<code>{h}</code> {html.escape(subject)}" for h, _, subject in (c.partition(" ") for c in commits))
        send(chat, f"📦 <b>{p.name}</b>: новых коммитов {len(commits)}\n<blockquote expandable>{rows}</blockquote>",
             [("Обновить", f"do:update:{p.name}"), ("Отмена", "cancel")])
    elif p.is_self:
        send(chat, "Бот деплоя уже последней версии.")
    else:
        send(chat, f"{p.name}: новых коммитов нет. Пересобрать и перезапустить всё равно (например, после правки .env)?",
             [("Пересобрать", f"do:update:{p.name}"), ("Отмена", "cancel")])


def ask_restart(chat: int, p: Project) -> None:
    what = ", ".join(p.restart) or "все контейнеры"
    send(chat, f"Перезапустить {p.name} ({what})?", [("Перезапустить", f"do:restart:{p.name}"), ("Отмена", "cancel")])


def run_steps(chat: int, message_id: int, title: str, p: Project, steps: list[tuple[str, list[str], int]]) -> tuple[bool, str]:
    """Runs steps one by one, showing progress in the message; stops at the first failure."""
    log = ""
    for i, (label, cmd, timeout) in enumerate(steps, 1):
        edit(chat, message_id, f"⏳ {p.name}, {title}: {i}/{len(steps)} {label}…")
        ok, text = sh(p, *cmd, timeout=timeout)
        log += text + "\n"
        if not ok:
            return False, log + f"\nОшибка на шаге «{label}»\n"
    return True, log


def update(chat: int, message_id: int, p: Project) -> None:
    started, before = time.time(), out(p, "git", "rev-parse", "HEAD")
    ok, log = run_steps(chat, message_id, "обновление", p, [("скачиваю код", ["git", "pull", "--ff-only"], 120)])
    changed = out(p, "git", "diff", "--name-only", f"{before}..HEAD").split() if ok else []
    if ok and not p.is_self:
        steps = [("собираю и запускаю", ["docker", "compose", "up", "-d", "--build"], 1800)]
        steps += [(f"{file} изменился: {' '.join(cmd)}", ["docker", "compose", *cmd], 300) for file, cmd in p.on_change if file in changed]
        ok, more = run_steps(chat, message_id, "обновление", p, steps)
        log += more
        if ok and not wait_healthy(p):
            ok, log = False, log + f"\nПосле запуска не работает: {', '.join(p.check) or 'ни один контейнер'}\n"
    version = commit_line(p)
    if ok:
        edit(chat, message_id, f"✅ {p.name} обновлён за {duration(time.time() - started)}\n{version}")
    else:
        edit(chat, message_id, f"❌ {p.name}: обновление не удалось, подробности в файле.\nСейчас: {version}")
        send_file(chat, f"{p.name}-update-{stamp()}.txt", log + ("" if p.is_self else "\n\n===== логи после обновления =====\n" + logs_text(p, 200)))
    if ok and p.is_self and changed:
        send(chat, "Бот деплоя перезапускается с новым кодом, через несколько секунд он снова на связи.")
        os._exit(0)  # systemd (Restart=always) starts the new code


def restart(chat: int, message_id: int, p: Project) -> None:
    ok, log = run_steps(chat, message_id, "перезапуск", p, [("перезапускаю", ["docker", "compose", "restart", *p.restart], 300)])
    if ok and wait_healthy(p, 10):
        edit(chat, message_id, f"✅ {p.name} перезапущен и работает")
        return
    edit(chat, message_id, f"❌ {p.name} не поднялся, подробности в файле")
    send_file(chat, f"{p.name}-restart-{stamp()}.txt", log + "\n\n===== логи =====\n" + logs_text(p, 200))


def exclusive(job, chat: int, message_id: int, p: Project) -> None:
    lock = busy.setdefault(p.name, threading.Lock())
    if not lock.acquire(blocking=False):
        edit(chat, message_id, f"{p.name}: уже идёт обновление или перезапуск, дождитесь его.")
        return
    try:
        job(chat, message_id, p)
    finally:
        lock.release()


def in_background(chat: int, fn, *args) -> None:
    """Long commands must not block polling; a failure is reported instead of lost."""
    def run():
        try:
            fn(*args)
        except Exception as e:
            traceback.print_exc()
            send(chat, f"Ошибка бота: {html.escape(str(e))}")
    threading.Thread(target=run, daemon=True).start()


VIEWS = {STATUS: (status_text, status_raw), DOCKER: (docker_text, docker_raw), HOST: (server_text, server_raw)}
ASK = {UPDATE: ask_update, LOGS: show_logs, RESTART: ask_restart}  # need a project
JOBS = {"update": update, "restart": restart}


def choose(chat: int, text: str) -> None:
    """One project: act on it right away; several: ask which one first."""
    options = [p for p in PROJECTS.values() if text == UPDATE or not p.is_self]  # the bot itself only updates
    if len(options) == 1:
        ASK[text](chat, options[0])
        return
    key = {UPDATE: "update", LOGS: "logs", RESTART: "restart"}[text]
    send(chat, f"{text}: какой проект?", [(p.name, f"ask:{key}:{p.name}") for p in options] + [("Отмена", "cancel")])


def handle(u: dict) -> None:
    """Dispatches one update; anything from a stranger or outside a private chat is ignored."""
    if m := u.get("message"):
        if m["from"]["id"] not in ADMINS or m["chat"]["type"] != "private":
            return
        chat, text = m["chat"]["id"], m.get("text", "")
        if text in VIEWS:
            render = VIEWS[text][view(chat) == "raw"]
            in_background(chat, lambda: send(chat, render()))
        elif text == VIEW:
            in_background(chat, ask_view, chat)
        elif text in ASK:
            in_background(chat, choose, chat, text)
        else:
            in_background(chat, send, chat, HELP.format(server=html.escape(SERVER)))
    elif q := u.get("callback_query"):
        if q["from"]["id"] not in ADMINS or "message" not in q:
            return
        api("answerCallbackQuery", {"callback_query_id": q["id"]})
        chat, message_id = q["message"]["chat"]["id"], q["message"]["message_id"]
        kind, _, rest = q.get("data", "").partition(":")
        action, _, name = rest.partition(":")
        p = PROJECTS.get(name)
        if kind == "view" and rest in VIEW_NAMES:
            set_view(chat, rest)
            edit(chat, message_id, f"Вид: {VIEW_NAMES[rest]}")
        elif kind == "ask" and p:
            edit(chat, message_id, p.name)  # drops the buttons
            asks = {"update": ask_update, "logs": show_logs, "restart": ask_restart}
            in_background(chat, asks[action], chat, p)
        elif kind == "do" and p and action in JOBS:  # the edit inside drops the buttons: no double start
            in_background(chat, exclusive, JOBS[action], chat, message_id, p)
        else:
            edit(chat, message_id, "Отменено")


def main() -> None:
    global TOKEN, ADMINS, PROJECTS, SERVER
    TOKEN, ADMINS, PROJECTS, SERVER = load()
    api("setMyCommands", {"commands": [{"command": "start", "description": "Кнопки управления"}]})
    print(f"deploybot on {SERVER}: projects {list(PROJECTS)}, admins {sorted(ADMINS)}", flush=True)
    offset = 0
    while True:
        try:
            updates = api("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]})
        except Exception as e:
            print(f"getUpdates failed: {e}", file=sys.stderr, flush=True)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    main()
