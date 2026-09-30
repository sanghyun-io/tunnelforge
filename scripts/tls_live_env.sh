#!/usr/bin/env bash
# Disposable TLS/SSH trust test environment for TF-STATUS-110 (all resources: tf-test-a-*).
#
#   scripts/tls_live_env.sh certs                 # generate CAs + scenario certs into $TF_TLS_CERT_DIR
#   scripts/tls_live_env.sh up-pg | up-mysql      # start servers on 25432 / 23306 (tmpfs, no volumes)
#   scripts/tls_live_env.sh cert pg|mysql <scenario>   # hot-swap the server certificate
#                                                 # scenarios: good | wrongname | expired | untrusted | none
#   scripts/tls_live_env.sh up-ssh [pubkey-file] | authorize <pubkey-file> | rotate-ssh
#                                                 # OpenSSH server on 22222 (same docker network as PG) / regenerate host key
#   scripts/tls_live_env.sh down                  # remove every tf-test-a-* container
#
# Live tests (migration_core/tests/live_tls.rs, tests/test_tls_live.py) read TF_TLS_TEST_* env vars.
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

CERT_DIR="${TF_TLS_CERT_DIR:-${TEMP:-/tmp}/tf-test-a-certs}"
# forward slashes: openssl config files treat backslashes as escapes
CERT_DIR="${CERT_DIR//\\//}"
PG_NAME=tf-test-a-pg
MY_NAME=tf-test-a-mysql
SSH_NAME=tf-test-a-ssh
NET_NAME=tf-test-a-net
SAN="subjectAltName=DNS:tf-db.test,IP:127.0.0.1"

gen_ca() { # name
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$CERT_DIR/$1.key" -out "$CERT_DIR/$1.pem" \
    -days 3650 -subj "/CN=tf-test-a $1" >/dev/null 2>&1
}

gen_leaf() { # scenario ca san [startdate enddate]
  local name=$1 ca=$2 san=$3
  local dir="$CERT_DIR/$name"; mkdir -p "$dir"
  openssl req -newkey rsa:2048 -nodes -keyout "$dir/server.key" -out "$dir/server.csr" \
    -subj "/CN=tf-db.test" >/dev/null 2>&1
  printf '%s\n' "$san" > "$dir/ext.cnf"
  if [ $# -ge 5 ]; then
    # openssl ca is the only portable way to back-date a certificate
    mkdir -p "$dir/ca"; : > "$dir/ca/index.txt"; echo 01 > "$dir/ca/serial"
    cat > "$dir/ca.cnf" <<EOF
[ca]
default_ca = c
[c]
database = $dir/ca/index.txt
serial = $dir/ca/serial
new_certs_dir = $dir/ca
default_md = sha256
policy = p
copy_extensions = none
[p]
commonName = supplied
EOF
    openssl ca -batch -config "$dir/ca.cnf" -cert "$CERT_DIR/$ca.pem" -keyfile "$CERT_DIR/$ca.key" \
      -in "$dir/server.csr" -out "$dir/server.crt" -startdate "$4" -enddate "$5" \
      -extfile "$dir/ext.cnf" -notext >/dev/null 2>&1
  else
    openssl x509 -req -in "$dir/server.csr" -CA "$CERT_DIR/$ca.pem" -CAkey "$CERT_DIR/$ca.key" \
      -CAcreateserial -out "$dir/server.crt" -days 365 -extfile "$dir/ext.cnf" >/dev/null 2>&1
  fi
  cp "$CERT_DIR/$ca.pem" "$dir/ca.pem"
}

cmd_certs() {
  mkdir -p "$CERT_DIR"
  gen_ca ca; gen_ca other-ca
  gen_leaf good ca "$SAN"
  gen_leaf wrongname ca "subjectAltName=DNS:other.test,IP:127.0.0.1"
  gen_leaf wrongname-only ca "subjectAltName=DNS:other.test"
  gen_leaf expired ca "$SAN" 20200101000000Z 20200102000000Z
  gen_leaf untrusted other-ca "$SAN"
  echo "certs in $CERT_DIR"
}

wait_pg() { for _ in $(seq 60); do docker exec $PG_NAME pg_isready -U postgres >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
wait_my() { for _ in $(seq 90); do docker exec $MY_NAME mysqladmin -uroot -ptfpass ping >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }

ensure_net() { docker network inspect $NET_NAME >/dev/null 2>&1 || docker network create $NET_NAME >/dev/null; }

cmd_up_pg() {
  docker rm -f $PG_NAME >/dev/null 2>&1 || true
  ensure_net
  # network alias tf-db.test lets an SSH tunnel target the server by the name in the certificate
  docker run -d --name $PG_NAME --network $NET_NAME --network-alias tf-db.test     -p 127.0.0.1:25432:5432 -e POSTGRES_PASSWORD=tfpass \
    --tmpfs /var/lib/postgresql postgres:18.4 >/dev/null
  wait_pg; sleep 3; wait_pg
  docker exec $PG_NAME mkdir -p /tls
}

cmd_up_mysql() {
  docker rm -f $MY_NAME >/dev/null 2>&1 || true
  docker run -d --name $MY_NAME -p 127.0.0.1:23306:3306 -e MYSQL_ROOT_PASSWORD=tfpass \
    -e MYSQL_DATABASE=tfdb --tmpfs /var/lib/mysql mysql:8.4 >/dev/null
  wait_my; sleep 3; wait_my
}

cmd_cert() { # pg|mysql scenario
  local target=$1 scenario=$2
  if [ "$scenario" = expired-live ]; then
    # MySQL refuses to load an already-expired certificate, so issue one that expires in ~25s
    # (valid at load time), then wait until it has expired.
    gen_leaf expired-live ca "$SAN" "$(date -u -d '-1 hour' +%Y%m%d%H%M%SZ)" "$(date -u -d '+25 seconds' +%Y%m%d%H%M%SZ)"
    scenario=expired-live; local wait_after=30
  fi
  if [ "$target" = pg ]; then
    if [ "$scenario" = none ]; then
      docker exec -u postgres $PG_NAME psql -qc "ALTER SYSTEM SET ssl=off" -c "select pg_reload_conf()" >/dev/null
    else
      docker cp "$CERT_DIR/$scenario/server.crt" $PG_NAME:/tls/server.crt
      docker cp "$CERT_DIR/$scenario/server.key" $PG_NAME:/tls/server.key
      docker exec $PG_NAME sh -c "chown postgres:postgres /tls/server.* && chmod 600 /tls/server.key"
      docker exec -u postgres $PG_NAME psql -qc "ALTER SYSTEM SET ssl_cert_file='/tls/server.crt'" \
        -c "ALTER SYSTEM SET ssl_key_file='/tls/server.key'" -c "ALTER SYSTEM SET ssl=on" -c "select pg_reload_conf()" >/dev/null
    fi
    sleep 1
  else
    if [ "$scenario" = none ]; then
      docker exec $MY_NAME mysql -uroot -ptfpass -e "SET PERSIST require_secure_transport=OFF" 2>/dev/null
      echo "mysql 'none' scenario: use up-mysql-nossl" >&2; return 1
    fi
    # docker cp cannot write into tmpfs mounts, so stream the files through exec
    for pair in server.crt:server-cert.pem server.key:server-key.pem ca.pem:ca.pem; do
      docker exec -i $MY_NAME sh -c "cat > /var/lib/mysql/${pair#*:}" < "$CERT_DIR/$scenario/${pair%%:*}"
    done
    docker exec $MY_NAME sh -c "chown mysql:mysql /var/lib/mysql/*.pem && chmod 600 /var/lib/mysql/server-key.pem"
    docker exec $MY_NAME mysql -uroot -ptfpass -e "ALTER INSTANCE RELOAD TLS" 2>/dev/null
  fi
  sleep "${wait_after:-0}"
}

cmd_up_mysql_nossl() {
  docker rm -f $MY_NAME >/dev/null 2>&1 || true
  docker run -d --name $MY_NAME -p 127.0.0.1:23306:3306 -e MYSQL_ROOT_PASSWORD=tfpass \
    -e MYSQL_DATABASE=tfdb --tmpfs /var/lib/mysql mysql:8.4 --tls-version= >/dev/null
  wait_my; sleep 3; wait_my
}

cmd_up_ssh() { # [public-key-file]
  docker rm -f -v $SSH_NAME >/dev/null 2>&1 || true
  ensure_net
  docker run -d --name $SSH_NAME --network $NET_NAME -p 127.0.0.1:22222:2222 \
    -e USER_NAME=tfuser -e USER_PASSWORD=tfpass -e PASSWORD_ACCESS=false \
    lscr.io/linuxserver/openssh-server:latest >/dev/null
  for _ in $(seq 60); do docker logs $SSH_NAME 2>&1 | grep -q "done." && break; sleep 1; done
  sleep 2
  # the image ships AllowTcpForwarding=no; the SSH tunnel tests need direct-tcpip channels
  docker exec $SSH_NAME sed -i 's/^AllowTcpForwarding no/AllowTcpForwarding yes/' /config/sshd/sshd_config
  docker restart $SSH_NAME >/dev/null
  for _ in $(seq 60); do docker logs --since 5s $SSH_NAME 2>&1 | grep -q "done." && break; sleep 1; done
  sleep 2
  if [ -n "${1:-}" ]; then cmd_authorize "$1"; fi
}

cmd_authorize() { # public-key-file: append to tfuser's authorized_keys
  docker exec -i $SSH_NAME sh -c 'cat >> /config/.ssh/authorized_keys' < "$1"
}

cmd_rotate_ssh() {
  # the image only generates host keys on first start, so create the replacements ourselves
  docker exec $SSH_NAME sh -c 'cd /config/ssh_host_keys && rm -f ssh_host_*     && for t in rsa ecdsa ed25519; do ssh-keygen -q -t $t -N "" -f ssh_host_${t}_key; done     && chown tfuser:tfuser ssh_host_* && chmod 600 ssh_host_*_key'
  docker restart $SSH_NAME >/dev/null
  for _ in $(seq 60); do docker logs --since 5s $SSH_NAME 2>&1 | grep -q "done." && break; sleep 1; done
  sleep 2
}

cmd_down() {
  for n in $PG_NAME $MY_NAME $SSH_NAME; do docker rm -f -v $n >/dev/null 2>&1 || true; done
  docker network rm $NET_NAME >/dev/null 2>&1 || true
  echo "removed tf-test-a-* containers"
}

case "${1:-}" in
  certs) cmd_certs ;;
  up-pg) cmd_up_pg ;;
  up-mysql) cmd_up_mysql ;;
  up-mysql-nossl) cmd_up_mysql_nossl ;;
  cert) cmd_cert "$2" "$3" ;;
  up-ssh) cmd_up_ssh "${2:-}" ;;
  authorize) cmd_authorize "$2" ;;
  rotate-ssh) cmd_rotate_ssh ;;
  down) cmd_down ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
