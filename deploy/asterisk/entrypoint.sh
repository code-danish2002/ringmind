#!/bin/sh
# Render /etc/asterisk/templates/*.conf with env vars, then run Asterisk in the foreground.
set -eu

die() { echo "asterisk-entrypoint: $*" >&2; exit 1; }

: "${LAN_IP:?LAN_IP must be set to the Mac's Wi-Fi IP (e.g. 192.168.1.23)}"
: "${SIP_PASSWORD:=}"
: "${SIP_1001_CALLERID_NUM:=+15551234567}"
: "${SIP_1002_CALLERID_NUM:=+15550001111}"
: "${BOT_HOST:=app}"
: "${BOT_HTTP_PORT:=8000}"
: "${BOT_AUDIOSOCKET_PORT:=9092}"
: "${LOCAL_NET:=172.16.0.0/12}"

echo "$LAN_IP" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || die "LAN_IP '$LAN_IP' is not an IPv4 address"
[ "${#SIP_PASSWORD}" -ge 8 ] || die "SIP_PASSWORD must be set and at least 8 characters (SIP port is exposed on your LAN)"
case "$SIP_PASSWORD" in
  *[\;\"\\[:space:]]*) die "SIP_PASSWORD must not contain ';', '\"', '\\' or whitespace" ;;
esac
for n in "$SIP_1001_CALLERID_NUM" "$SIP_1002_CALLERID_NUM"; do
  echo "$n" | grep -Eq '^\+?[0-9]{3,20}$' || die "caller ID number '$n' must be digits with optional leading +"
done
for p in "$BOT_HTTP_PORT" "$BOT_AUDIOSOCKET_PORT"; do
  echo "$p" | grep -Eq '^[0-9]{1,5}$' || die "port '$p' is not a number"
done
LOCAL_NET_LINES=""
for net in $(echo "$LOCAL_NET" | tr ',' ' '); do
  echo "$net" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' || die "LOCAL_NET entry '$net' is not a CIDR"
  LOCAL_NET_LINES="${LOCAL_NET_LINES}local_net = $net
"
done
echo "$BOT_HOST" | grep -Eq '^[A-Za-z0-9.-]+$' || die "BOT_HOST '$BOT_HOST' is not a hostname/IP"

export LOCAL_NET_LINES LAN_IP SIP_PASSWORD SIP_1001_CALLERID_NUM SIP_1002_CALLERID_NUM BOT_HOST BOT_HTTP_PORT BOT_AUDIOSOCKET_PORT
# Only substitute our variables; Asterisk's own ${...} dialplan expressions are left alone.
VARS='${LOCAL_NET_LINES} ${LAN_IP} ${SIP_PASSWORD} ${SIP_1001_CALLERID_NUM} ${SIP_1002_CALLERID_NUM} ${BOT_HOST} ${BOT_HTTP_PORT} ${BOT_AUDIOSOCKET_PORT}'
for f in /etc/asterisk/templates/*.conf; do
  envsubst "$VARS" < "$f" > "/etc/asterisk/$(basename "$f")"
done
chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /run/asterisk 2>/dev/null || true
chmod 640 /etc/asterisk/pjsip.conf

echo "asterisk-entrypoint: LAN_IP=$LAN_IP local_net=$LOCAL_NET bot=$BOT_HOST http:$BOT_HTTP_PORT audiosocket:$BOT_AUDIOSOCKET_PORT"
exec asterisk -f -n -U asterisk -G asterisk -vvv "$@"
