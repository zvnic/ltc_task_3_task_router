#!/usr/bin/env bash
# Настройка системного nginx + Let's Encrypt для «Task Router Диспетчерская».
#
# Публикует https://task-router.ii2b.ru -> http://127.0.0.1:${WEB_PORT}
# (веб-контейнер из compose.yaml; порт берётся из .env).
#
# Требует root-прав (nginx и certbot работают от root):
#   sudo CERTBOT_EMAIL=zvnman@gmail.com bash scripts/setup-nginx-ssl.sh
#
# Скрипт идемпотентен: повторный запуск безопасен (certbot продлит/сохранит
# существующий сертификат, конфиг перезапишется из шаблона).
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
SITE="task-router.ii2b.ru"
CONF_TEMPLATE="$PROJECT_ROOT/infra/nginx/${SITE}.conf"
AVAILABLE="/etc/nginx/sites-available"
ENABLED="/etc/nginx/sites-enabled"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-zvnman@gmail.com}"

if [[ $EUID -ne 0 ]]; then
  echo "Ошибка: нужны root-права (nginx и certbot)." >&2
  echo "  sudo CERTBOT_EMAIL=$CERTBOT_EMAIL bash $0" >&2
  exit 1
fi

for bin in nginx certbot; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Ошибка: $bin не установлен." >&2; exit 1; }
done

[[ -f "$ENV_FILE" ]] || { echo "Ошибка: нет $ENV_FILE" >&2; exit 1; }
[[ -f "$CONF_TEMPLATE" ]] || { echo "Ошибка: нет $CONF_TEMPLATE" >&2; exit 1; }

# Порт веб-контейнера на 127.0.0.1 (compose.yaml: WEB_PORT).
web_port="$(grep -E '^WEB_PORT=' "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '\r' | sed -E 's/^["'\'']|["'\'']$//g')"
[[ "$web_port" =~ ^[0-9]+$ ]] || { echo "Ошибка: WEB_PORT в .env некорректен." >&2; exit 1; }

mkdir -p "$AVAILABLE" "$ENABLED" /var/www/html

# Генерируем итоговый конфиг из шаблона с подстановкой порта.
sed "s|{{WEB_PORT}}|$web_port|g" "$CONF_TEMPLATE" > "$AVAILABLE/${SITE}.conf"
chmod 0644 "$AVAILABLE/${SITE}.conf"
ln -sfn "$AVAILABLE/${SITE}.conf" "$ENABLED/${SITE}.conf"
echo "конфиг: ${SITE} -> http://127.0.0.1:${web_port}"

nginx -t

# Сертификат Let's Encrypt (HTTP-01 challenge через nginx) + авто-редирект HTTP -> HTTPS.
certbot --nginx \
  --non-interactive \
  --agree-tos \
  --email "$CERTBOT_EMAIL" \
  --redirect \
  -d "$SITE"

nginx -t
systemctl reload nginx

echo
echo "Готово: https://${SITE}"
echo
certbot certificates
