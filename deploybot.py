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
HERE = Path(__file__).resolve().parent
STATUS, UPDATE, LOGS, RESTART = "📊 Состояние", "⬆️ Обновить", "📄 Логи", "🔄 Перезапуск"
KEYBOARD = {"keyboard": [[{"text": STATUS}, {"text": UPDATE}], [{"text": LOGS}, {"text": RESTART}]],
            "resize_keyboard": True, "is_persistent": True}
HELP = ("<b>{server}</b>\n\n"
        f"{STATUS} — версии, контейнеры, память и диск\n"
        f"{UPDATE} — покажу новые коммиты и обновлю после подтверждения\n"
        f"{LOGS} — логи контейнеров файлом\n{RESTART} — перезапустить приложение")


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
    cfg = configparser.ConfigParser()
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
    markup = {"inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in buttons]} if buttons else KEYBOARD
    return api("sendMessage", {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "reply_markup": markup})["message_id"]


def edit(chat: int, message_id: int, text: str) -> None:
    api("editMessageText", {"chat_id": chat, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML"})


def send_file(chat: int, name: str, text: str, caption: str = "") -> None:
    api("sendDocument", {"chat_id": chat, "caption": caption[:1000]}, (name, text.encode()))


# ---------- server ----------

def sh(p: Project, *cmd: str, timeout: int = 120) -> tuple[bool, str]:
    """Runs a command in the project's folder; returns (ok, transcript with the command line)."""
    head = f"$ {' '.join(cmd)}\n"
    try:
        r = subprocess.run(cmd, cwd=p.dir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{head}прервано: дольше {timeout} с\n"
    except OSError as e:
        return False, f"{head}{e}\n"
    return r.returncode == 0, head + r.stdout + r.stderr


def out(p: Project, *cmd: str) -> str:
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


def gb(n: float) -> str:
    return f"{n / 2**30:.1f}".replace(".", ",")


def duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m} мин {s} с" if m else f"{s} с"


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M")


def project_status(p: Project, stats: dict[str, str]) -> str:
    sh(p, "git", "fetch", "-q")
    behind = out(p, "git", "rev-list", "--count", "HEAD..@{u}")
    lines = [f"<b>{p.name}</b> {html.escape(out(p, 'git', 'log', '-1', '--format=%h %s (%cr)'))}",
             f"Новых коммитов: {behind}, нажмите «{UPDATE}»" if behind not in ("", "0") else "Последняя версия"]
    if not p.is_self:
        for c in containers(p) or [{"Service": "?", "State": "docker compose не ответил"}]:
            icon = "🟢" if c.get("State") == "running" and "unhealthy" not in c.get("Status", "") else "🔴"
            extra = stats.get(c.get("Name", ""), "")
            lines.append(f"{icon} {c.get('Service')} — {html.escape(c.get('Status') or c.get('State', ''))}"
                         + (f" · {html.escape(extra)}" if extra else ""))
    return "\n".join(lines)


def status_text() -> str:
    stats = {}
    first = next(iter(PROJECTS.values()))
    ok, text = sh(first, "docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}")
    for row in text.splitlines()[1:] if ok else []:
        name, *rest = row.split("\t")
        stats[name] = " · ".join(rest)
    blocks = [f"🖥 <b>{html.escape(SERVER)}</b>"] + [project_status(p, stats) for p in PROJECTS.values()]
    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    total, avail = (int(mem[k].split()[0]) * 1024 for k in ("MemTotal", "MemAvailable"))
    disk = shutil.disk_usage("/")
    blocks.append(f"<b>Ресурсы</b> память {gb(total - avail)} из {gb(total)} ГБ, диск {gb(disk.used)} из {gb(disk.total)} ГБ")
    return "\n\n".join(blocks)


def logs_text(p: Project, tail: int = 2000) -> str:
    return sh(p, "docker", "compose", "logs", "--no-color", "--timestamps", f"--tail={tail}", timeout=60)[1]


# ---------- actions ----------

def show_status(chat: int) -> None:
    send(chat, status_text())


def show_logs(chat: int, p: Project) -> None:
    text = logs_text(p)
    errors = sum(1 for line in text.splitlines() if "ERROR" in line or "Traceback" in line)
    send_file(chat, f"{p.name}-logs-{stamp()}.txt", text, f"{p.name}: последние 2000 строк каждого контейнера. Строк с ошибками: {errors}")


def ask_update(chat: int, p: Project) -> None:
    sh(p, "git", "fetch", "-q")
    commits = out(p, "git", "log", "--format=%h %s", "HEAD..@{u}")
    if commits:
        send(chat, f"<b>{p.name}: новые коммиты</b>\n{html.escape(commits)}", [("Обновить", f"do:update:{p.name}"), ("Отмена", "cancel")])
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
    version = html.escape(out(p, "git", "log", "-1", "--format=%h %s"))
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
        if text == STATUS:
            in_background(chat, show_status, chat)
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
        if kind == "ask" and p:
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
