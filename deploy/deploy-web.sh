#!/bin/sh
# Spielt die Web-Oberfläche des Job Agent in Container 106 (web, 192.168.178.217) ein.
# LAN: https://192.168.178.217/jobagent/ (nginx, selbstsigniert). Öffentlich: https://jobagent.paeffgen-it.de über
# Cloudflare (Pages, Worker, Tunnel; nur ausgehend). Den Tunnel betreibt der Betreiber selbst.
# Geheimnisse (AES-Schlüssel, PostgreSQL-Passwort der Rolle jobagent_web, Token der KI-Brücke) landen nur in
# /etc/jobagent-web.env (root, 600). Sie werden per stdin übertragen und nie ausgegeben.
set -eu
cd "$(dirname "$0")/.."
ZIEL=${ZIEL:-web}
PG_HOST=${PG_HOST:-192.168.178.214}

venv/bin/python -m py_compile jobsuche/web/app.py jobsuche/web/auftraege.py jobsuche/mandanten.py jobsuche/geheim.py
tar czf - --exclude=__pycache__ --exclude='jobsuche/ui' --exclude='jobsuche/analyse.py' \
    jobsuche requirements-web.txt deploy | ssh -o BatchMode=yes "$ZIEL" "cat > /tmp/jobagent-web.tgz"

ssh -o BatchMode=yes "$ZIEL" sh -s <<'REMOTE'
set -eu
export DEBIAN_FRONTEND=noninteractive
if ! python3 -c 'import venv, ensurepip' 2>/dev/null || ! command -v nft >/dev/null || ! command -v curl >/dev/null; then
  apt-get -qq update && apt-get -qq install -y python3-venv nftables curl >/dev/null
fi
id jobagent >/dev/null 2>&1 || useradd --system --home /var/lib/jobagent-web --shell /usr/sbin/nologin jobagent
install -d -o jobagent -g jobagent -m 700 /var/lib/jobagent-web
install -d -m 755 /opt/jobagent-web /etc/jobagent-web
rm -rf /opt/jobagent-web/app.neu && mkdir -p /opt/jobagent-web/app.neu
tar xzf /tmp/jobagent-web.tgz -C /opt/jobagent-web/app.neu && rm /tmp/jobagent-web.tgz
[ -d /opt/jobagent-web/venv ] || python3 -m venv /opt/jobagent-web/venv
/opt/jobagent-web/venv/bin/pip install -q --upgrade pip >/dev/null
/opt/jobagent-web/venv/bin/pip install -q -r /opt/jobagent-web/app.neu/requirements-web.txt
rm -rf /opt/jobagent-web/app.alt; [ -d /opt/jobagent-web/app ] && mv /opt/jobagent-web/app /opt/jobagent-web/app.alt
mv /opt/jobagent-web/app.neu /opt/jobagent-web/app
chmod -R a+rX /opt/jobagent-web/app

# Ausgangsfilter (SSRF-Schutz) und Dienste
install -m 644 /opt/jobagent-web/app/deploy/jobagent-egress.nft /etc/jobagent-web/egress.nft
install -m 644 /opt/jobagent-web/app/deploy/jobagent-egress.service /etc/systemd/system/jobagent-egress.service
install -m 644 /opt/jobagent-web/app/deploy/jobagent-web.service /etc/systemd/system/jobagent-web.service

# nginx: LAN unter /jobagent/ im bestehenden default-Server, Tunnel-Eingang nur auf 127.0.0.1:8091
install -m 644 /opt/jobagent-web/app/deploy/nginx-jobagent-lan.conf /etc/nginx/snippets/jobagent-lan.conf
install -m 644 /opt/jobagent-web/app/deploy/nginx-jobagent-tunnel.conf /etc/nginx/conf.d/jobagent-tunnel.conf
if ! grep -q 'snippets/jobagent-lan.conf' /etc/nginx/sites-available/default; then
  cp /etc/nginx/sites-available/default /etc/nginx/sites-available/default.vor-jobagent
  # nach „server_name _;“ im 443-Block einfügen (der 80-Block hat keinen server_name)
  sed -i '/listen 443 ssl default_server;/,/^}/ s|^\(\s*\)server_name _;|&\n\1include /etc/nginx/snippets/jobagent-lan.conf;|' /etc/nginx/sites-available/default
fi
nginx -t 2>&1 | tail -1 && systemctl reload nginx

# Öffentlicher Weg: Cloudflare Pages + Worker (Workers VPC) -> Tunnel (cloudflared-Dienst des Betreibers, Token in
# /etc/cloudflared.env) -> nginx 127.0.0.1:8091 -> App. Das richtet dieses Skript nicht ein.
# Geheimnis-Datei: AES-Schlüssel einmalig erzeugen (nie ausgeben)
touch /etc/jobagent-web.env && chmod 600 /etc/jobagent-web.env
if ! grep -q '^JOBAGENT_SCHLUESSEL=..' /etc/jobagent-web.env; then
  sed -i '/^JOBAGENT_SCHLUESSEL=/d' /etc/jobagent-web.env
  echo "JOBAGENT_SCHLUESSEL=$(cd /opt/jobagent-web/app && /opt/jobagent-web/venv/bin/python -m jobsuche.web schluessel)" >> /etc/jobagent-web.env
fi
systemctl daemon-reload
systemctl enable -q jobagent-egress jobagent-web
systemctl restart jobagent-egress
REMOTE

# PostgreSQL-Passwort der Web-Rolle und Token der KI-Brücke aus dem Schlüsselbund des PCs in die 600-Datei (per stdin)
venv/bin/python - "$ZIEL" "$PG_HOST" <<'PY'
import subprocess, sys, keyring
ziel, pg_host = sys.argv[1], sys.argv[2]
werte = {"JOBAGENT_PG_PASSWORT": keyring.get_password("jobsuche", f"postgres:jobagent_web@{pg_host}") or "",
         "JOBAGENT_CLAUDE_BRUECKE_TOKEN": keyring.get_password("db-studio", "claude-bruecke") or ""}
fehlt = [k for k, v in werte.items() if not v]
if fehlt:
    print("Fehlt im Schlüsselbund:", ", ".join(fehlt))
eingabe = "".join(f"{k}={v}\n" for k, v in werte.items() if v)
skript = ("set -eu; umask 077; f=/etc/jobagent-web.env; t=$(mktemp); "
          "grep -v -e '^JOBAGENT_PG_PASSWORT=' -e '^JOBAGENT_CLAUDE_BRUECKE_TOKEN=' $f > $t || true; cat >> $t; "
          "install -m 600 $t $f; rm -f $t")
r = subprocess.run(["ssh", "-o", "BatchMode=yes", ziel, skript], input=eingabe, text=True, capture_output=True)
print("Geheimnis-Datei aktualisiert" if r.returncode == 0 else "FEHLER beim Schreiben der Geheimnis-Datei")
PY

ssh -o BatchMode=yes "$ZIEL" "systemctl restart jobagent-web; sleep 6; systemctl is-active jobagent-web"
curl -sk -o /dev/null -w "https://192.168.178.217/jobagent/api/gesundheit -> %{http_code}\n" https://192.168.178.217/jobagent/api/gesundheit
