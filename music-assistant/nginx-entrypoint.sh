#!/bin/sh
set -eu

port=${MUSIC_ASSISTANT_HTTP_PORT:-80}
bind=${MUSIC_ASSISTANT_HTTP_BIND:-0.0.0.0}
tls_port=${MUSIC_ASSISTANT_HTTPS_PORT:-443}
case "$tls_port" in
    ''|*[!0-9]*) echo 'MUSIC_ASSISTANT_HTTPS_PORT must be numeric' >&2; exit 2 ;;
esac
case "$port" in
    ''|*[!0-9]*) echo 'MUSIC_ASSISTANT_HTTP_PORT must be numeric' >&2; exit 2 ;;
esac
case "$port" in
    1|[1-9][0-9]|[1-9][0-9][0-9]|[1-9][0-9][0-9][0-9]|[1-5][0-9][0-9][0-9][0-9]) ;;
    *) echo 'MUSIC_ASSISTANT_HTTP_PORT must be between 1 and 59999' >&2; exit 2 ;;
esac
case "$bind" in
    ''|*[!0-9.]*) echo 'MUSIC_ASSISTANT_HTTP_BIND must be an IPv4 address' >&2; exit 2 ;;
esac

sed -e "s/__HTTP_BIND__/$bind/g" -e "s/__HTTP_PORT__/$port/g" -e "s/__HTTPS_PORT__/$tls_port/g" /etc/nginx/conf.d/default.conf.template > /tmp/hifimate-nginx.conf
cat > /tmp/nginx.conf <<'EOF'
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 1024; }
http {
    include /etc/nginx/mime.types;
    access_log /dev/stdout;
    include /tmp/hifimate-nginx.conf;
}
EOF
exec nginx -c /tmp/nginx.conf -g 'daemon off;'
