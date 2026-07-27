#!/usr/bin/env bash
# Start a throwaway Postgres for the test suite, without Docker.
#
# The tests run against a real database because the P00 invariants are database
# invariants: a partial unique index, a foreign key, a check constraint,
# transaction rollback. A fake would test nothing that matters.
#
#   ./scripts/dev-db.sh start   then   pytest
#   ./scripts/dev-db.sh stop

set -euo pipefail

PGBIN=${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | tail -1)}
PGDATA=${PGDATA:-/tmp/pgdata}
PGRUN=${PGRUN:-/tmp/pgrun}
PGPORT=${PGPORT:-55432}

export PATH="$PGBIN:$PATH"

# initdb and postgres refuse to run as root, so drop to an unprivileged user.
as_pg() {
  if [ "$(id -u)" -eq 0 ]; then
    id -u postgres >/dev/null 2>&1 || useradd -m postgres
    su postgres -c "PATH=$PGBIN:\$PATH $*"
  else
    bash -c "$*"
  fi
}

case "${1:-start}" in
  start)
    mkdir -p "$PGDATA" "$PGRUN"
    [ "$(id -u)" -eq 0 ] && chown -R postgres "$PGDATA" "$PGRUN"
    if [ ! -s "$PGDATA/PG_VERSION" ]; then
      as_pg "initdb -D $PGDATA -U coach --auth=trust" >/dev/null
    fi
    # listen_addresses empty means unix socket only: nothing on the network.
    as_pg "pg_ctl -D $PGDATA -o '-p $PGPORT -k $PGRUN -c listen_addresses=' -l /tmp/pg.log start"
    echo "TEST_DATABASE_URL=postgresql://coach@/postgres?host=$PGRUN&port=$PGPORT"
    ;;
  stop)
    as_pg "pg_ctl -D $PGDATA stop" || true
    ;;
  *)
    echo "usage: $0 {start|stop}" >&2
    exit 2
    ;;
esac
