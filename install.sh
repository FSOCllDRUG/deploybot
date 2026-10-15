#!/bin/sh
# Sets up the deploy bot on this server. Run as root from the cloned folder: ./install.sh
# First run creates the config; fill it in and run again to start the service.
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Нужен python3 (3.8+)"; exit 1; }
command -v docker >/dev/null || echo "Внимание: docker не найден"
if [ ! -f /etc/deploybot/projects.ini ]; then
  mkdir -p /etc/deploybot
  install -m 600 projects.example.ini /etc/deploybot/projects.ini
  echo "Создан /etc/deploybot/projects.ini. Впишите name, token, admins и проекты, затем снова: ./install.sh"
  exit 0
fi
python3 -c 'import deploybot; deploybot.load()'  # stops here with a clear message if the config is wrong
sed "s#/opt/deploybot#$(pwd)#" deploybot.service > /etc/systemd/system/deploybot.service
systemctl daemon-reload
systemctl enable deploybot >/dev/null 2>&1
systemctl restart deploybot
sleep 3
systemctl --no-pager --lines=5 status deploybot
echo "Готово. Напишите боту /start."
