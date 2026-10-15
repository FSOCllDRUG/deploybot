"""Runs the real update path: a real git clone tracking a remote, and a fake `docker` on PATH."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploybot  # noqa: E402
from deploybot import Project  # noqa: E402

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$DOCKER_CALLS"
case "$*" in
  "compose ps"*) printf '%s\\n' "$PS_JSON" ;;
  "compose logs"*) echo "app-1  | started" ;;
  "stats"*) printf 'zapasik-app-1\\t0.5%%\\t180MiB / 7.6GiB\\n' ;;
  "ps --all"*) printf '%s\\n' "$ALL_JSON" ;;
  "system df"*) printf '%s\\n' '{"Type":"Images","Size":"2.137GB","Reclaimable":"1.2GB (56%)"}' ;;
esac
exit "${DOCKER_EXIT:-0}"
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Server:
    def __init__(self, tmp_path, monkeypatch):
        origin, self.dev = tmp_path / "origin.git", tmp_path / "dev"
        git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
        git("clone", "-q", str(origin), str(self.dev), cwd=tmp_path)
        git("config", "user.email", "t@t", cwd=self.dev)
        git("config", "user.name", "t", cwd=self.dev)
        self.push("Caddyfile", "a\n", "first")
        self.clone = tmp_path / "zapasik"
        git("clone", "-q", "-b", "main", str(origin), str(self.clone), cwd=tmp_path)
        self.project = Project("zapasik", self.clone, check=["app"], restart=["app"], on_change=[("Caddyfile", ["restart", "caddy"])])
        monkeypatch.setattr(deploybot, "PROJECTS", {"zapasik": self.project})
        monkeypatch.setattr(deploybot, "ADMINS", {1})

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(FAKE_DOCKER)
        (bin_dir / "docker").chmod(0o755)
        self.calls_file = tmp_path / "calls"
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("DOCKER_CALLS", str(self.calls_file))
        self.app_state(monkeypatch, "running", "Up 2 hours")
        monkeypatch.setattr(deploybot, "wait_healthy", lambda p, seconds=0: deploybot.healthy(p))

        self.sent = []
        monkeypatch.setattr(deploybot, "send", lambda chat, text, buttons=None: self.sent.append(("send", text, buttons)) or 1)
        monkeypatch.setattr(deploybot, "edit", lambda chat, mid, text: self.sent.append(("edit", text)))
        monkeypatch.setattr(deploybot, "send_file", lambda chat, name, text, caption="": self.sent.append(("file", name, text)))

    def push(self, path, content, msg):
        (self.dev / path).write_text(content)
        git("add", ".", cwd=self.dev)
        git("commit", "-qm", msg, cwd=self.dev)
        git("push", "-q", "origin", "HEAD:main", cwd=self.dev)

    @staticmethod
    def app_state(monkeypatch, state, status):
        monkeypatch.setenv("PS_JSON", json.dumps({"Service": "app", "Name": "zapasik-app-1", "State": state, "Status": status}))

    def calls(self):
        return self.calls_file.read_text().splitlines() if self.calls_file.exists() else []


@pytest.fixture
def server(tmp_path, monkeypatch):
    return Server(tmp_path, monkeypatch)


def test_update_pulls_builds_and_runs_on_change_only_when_needed(server):
    server.push("app.py", "x = 1\n", "Feature one")
    deploybot.ask_update(1, server.project)
    assert "Feature one" in server.sent[-1][1] and server.sent[-1][2][0] == ("Обновить", "do:update:zapasik")

    deploybot.update(1, 5, server.project)
    assert server.calls() == ["compose up -d --build", "compose ps --all --format json"]
    assert server.sent[-1][1].startswith("✅ zapasik обновлён") and "Feature one" in server.sent[-1][1]

    server.push("Caddyfile", "b\n", "New CSP")
    deploybot.update(1, 5, server.project)
    assert "compose restart caddy" in server.calls()


def test_failed_build_sends_a_log_file(server, monkeypatch):
    server.push("app.py", "x = 2\n", "Broken")
    monkeypatch.setenv("DOCKER_EXIT", "1")
    deploybot.update(1, 5, server.project)
    assert any(m[0] == "edit" and m[1].startswith("❌") for m in server.sent)
    kind, name, text = server.sent[-1]
    assert kind == "file" and name.startswith("zapasik-update-") and "compose up -d --build" in text and "собираю" in text


def test_app_down_after_start_is_a_failure(server, monkeypatch):
    server.app_state(monkeypatch, "restarting", "Restarting (1)")
    deploybot.update(1, 5, server.project)
    assert any(m[0] == "edit" and m[1].startswith("❌") for m in server.sent) and server.sent[-1][0] == "file"


def test_restart(server):
    deploybot.restart(1, 5, server.project)
    assert server.calls()[0] == "compose restart app" and server.sent[-1][1].startswith("✅")


def test_status(server):
    server.push("app.py", "x = 3\n", "Pending")
    text = deploybot.status_text()
    assert "Новых коммитов: 1" in text and "🟢 <b>app</b> · работает 2 ч · CPU 0,5% · 180 МБ" in text and "💾 Память" in text


def test_self_update_restarts_the_bot(server, monkeypatch):
    monkeypatch.setattr(deploybot, "HERE", server.clone)
    exits = []
    monkeypatch.setattr(deploybot.os, "_exit", exits.append)
    server.push("deploybot.py", "# new\n", "Bot fix")
    deploybot.update(1, 5, server.project)
    assert server.calls() == [] and exits == [0]  # git only, no docker


def test_several_projects_ask_which_one(server, monkeypatch, tmp_path):
    other = Project("blog", tmp_path)
    monkeypatch.setattr(deploybot, "PROJECTS", {"zapasik": server.project, "blog": other})
    deploybot.choose(1, deploybot.LOGS)
    assert [b for _, b in server.sent[-1][2]] == ["ask:logs:zapasik", "ask:logs:blog", "cancel"]


def test_config(tmp_path):
    ini = tmp_path / "p.ini"
    ini.write_text((Path(deploybot.__file__).parent / "projects.example.ini").read_text(encoding="utf-8"), encoding="utf-8")
    ini.write_text(ini.read_text(encoding="utf-8").replace("123456:ABC...", "42:real"), encoding="utf-8")
    token, admins, projects, server = deploybot.load(ini)
    assert server == "germany vps" and admins == {111111111} and list(projects) == ["zapasik", "deploybot"]
    z = projects["zapasik"]
    assert (z.check, z.restart, z.on_change) == (["app"], ["app"], [("Caddyfile", ["restart", "caddy"])])
    ini.write_text("[bot]\ntoken = t\nadmins = 1\n[Bad Name]\ndir = /x\n")
    with pytest.raises(SystemExit):
        deploybot.load(ini)


def test_strangers_and_groups_are_ignored(monkeypatch):
    started = []
    monkeypatch.setattr(deploybot, "ADMINS", {1})
    monkeypatch.setattr(deploybot, "in_background", lambda chat, fn, *a: started.append(chat))
    monkeypatch.setattr(deploybot, "api", lambda *a, **kw: started.append("api"))
    msg = lambda uid, kind: {"message": {"from": {"id": uid}, "chat": {"id": uid, "type": kind}, "text": deploybot.STATUS}}
    deploybot.handle(msg(2, "private"))
    deploybot.handle(msg(1, "group"))
    deploybot.handle({"callback_query": {"id": "q", "from": {"id": 2}, "data": "do:update:zapasik", "message": {"chat": {"id": 2}, "message_id": 3}}})
    assert started == []
    deploybot.handle(msg(1, "private"))
    assert started == [1]


def test_send_document_is_valid_multipart(monkeypatch):
    from email.parser import BytesParser
    seen = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"ok": true, "result": {}}'

    monkeypatch.setattr(deploybot.urllib.request, "urlopen", lambda req, timeout: seen.append(req) or Resp())
    deploybot.send_file(1, "логи.txt", "строка 1\nстрока 2", "подпись")
    req = seen[0]
    msg = BytesParser().parsebytes(f"Content-Type: {req.get_header('Content-type')}\r\n\r\n".encode() + req.data)
    parts = {p.get_param("name", header="content-disposition"): p for p in msg.get_payload()}
    assert parts["chat_id"].get_payload() == "1" and parts["caption"].get_payload(decode=True).decode() == "подпись"
    assert parts["document"].get_payload(decode=True).decode() == "строка 1\nстрока 2"


def test_projects_update_independently(server, tmp_path):
    """A long build of one project must not block another; the same project can't run twice."""
    other = Project("blog", tmp_path)
    deploybot.busy["zapasik"] = lock = deploybot.threading.Lock()
    lock.acquire()
    deploybot.exclusive(lambda chat, mid, p: server.sent.append(("ran", p.name)), 1, 5, server.project)
    deploybot.exclusive(lambda chat, mid, p: server.sent.append(("ran", p.name)), 1, 5, other)
    lock.release()
    assert server.sent == [("edit", "zapasik: уже идёт обновление или перезапуск, дождитесь его."), ("ran", "blog")]


def test_russian_docker_words():
    assert deploybot.ru_status("Up 6 minutes (healthy)") == "работает 6 мин · здоров"
    assert deploybot.ru_status("Up About an hour") == "работает ~1 ч"
    assert deploybot.ru_status("Exited (137) 2 days ago") == "остановлен, код 137, 2 дн назад"
    assert deploybot.ru_status("Restarting (1) 5 seconds ago") == "перезапускается, код 1"
    assert deploybot.ru_status("22 minutes ago") == "22 мин назад"
    assert deploybot.ru_size("212.8MiB") == "213 МБ" and deploybot.ru_size("1.2GB (56%)") == "1,2 ГБ (56%)"
    assert deploybot.ru_size("0B") == "0 Б" and deploybot.num(1.0) == "1"


def test_docker_view_groups_all_containers(server, monkeypatch):
    rows = [
        {"Names": "zapasik-app-1", "State": "running", "Status": "Up 2 hours", "Image": "zapasik-app",
         "Labels": "com.docker.compose.project=zapasik,com.docker.compose.service=app"},
        {"Names": "old-db", "State": "exited", "Status": "Exited (0) 3 weeks ago", "Image": "postgres:16", "Labels": ""},
    ]
    monkeypatch.setenv("ALL_JSON", "\n".join(json.dumps(r) for r in rows))
    text = deploybot.docker_text()
    assert "Работают 1 из 2" in text and "📦 <b>zapasik</b>" in text and "📦 <b>без compose</b>" in text
    assert "🟢 <b>zapasik-app-1</b> · работает 2 ч · CPU 0,5% · 180 МБ" in text
    assert "⚪ <b>old-db</b> · остановлен, код 0, 3 нед назад" in text
    assert "Образы: 2,1 ГБ, можно освободить 1,2 ГБ (56%)" in text


def test_server_view():
    text = deploybot.server_text()
    assert "⚙️ <b>Процессор</b>" in text and "🧠 <b>Память</b>" in text
    assert "💽 <b>Диск /</b>" in text and "<blockquote expandable>" in text


def test_long_message_goes_as_file_and_buttons_get_colours(monkeypatch):
    calls = []
    monkeypatch.setattr(deploybot, "api", lambda method, data=None, file=None: calls.append((method, data, file)) or {"message_id": 7})
    deploybot.send(1, "<b>x</b>" * 1000)
    assert calls[-1][0] == "sendDocument" and calls[-1][2][1].startswith(b"xxx")
    deploybot.send(1, "ok?", [("Обновить", "do:update:zapasik"), ("Отмена", "cancel")])
    buttons = [row[0] for row in calls[-1][1]["reply_markup"]["inline_keyboard"]]
    assert buttons[0]["style"] == "success" and "style" not in buttons[1]
