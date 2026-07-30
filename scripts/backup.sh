#!/bin/sh
# MEM-12's other half: the nightly pg_dump, beside the markdown fact export.
#
# "A nightly job exports the full active fact set to human readable markdown
# alongside a pg_dump. Both artefacts exist and are dated after the most recent
# consolidation."
#
# The markdown half is `coach-scheduler`'s. This is the dump, and it lives in the
# deployment rather than in the application for one reason: it needs the database
# superuser and a destination on the host, and `coach-scheduler` has no business
# holding either. It runs in the postgres image so `pg_dump` is the same version
# as the server — a newer server dumped by an older client refuses outright.
#
# **It runs after consolidation, not before.** MEM-12 wants both artefacts dated
# after the most recent consolidation, and consolidation is at 03:00 local. A dump
# at 02:00 would satisfy "a nightly dump" and fail the requirement, because the
# night's ratified facts would not be in it.
#
# Deliberately a sleep loop rather than cron. The same reasoning as
# `coach.runtime.scheduler`: a container that was down at 03:30 should dump when
# it comes back rather than skip the night, and a crude clock plus an idempotent
# job beats a precise clock that silently misses.
set -eu

: "${BACKUP_AT_HOUR:=3}"
: "${BACKUP_AT_MINUTE:=30}"
: "${BACKUP_KEEP_DAYS:=14}"
: "${BACKUP_DIR:=/backups}"
: "${PGDATABASE:=coach}"
: "${PGUSER:=coach}"
: "${PGHOST:=postgres}"

export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

log() { printf '%s backup: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

dump_once() {
    stamp=$(date '+%Y-%m-%d')
    target="${BACKUP_DIR}/coach-${stamp}.dump"

    # Idempotent on the date, like every other nightly job here. A restart at
    # 04:00 must not produce a second dump of the same day.
    if [ -f "${target}" ]; then
        log "already dumped ${stamp}, nothing to do"
        return 0
    fi

    # Written to a temporary name and moved into place, so a dump interrupted
    # half way through is never mistaken for a complete one. OBS-06 wants these
    # verified restorable, and a truncated file that looks finished is the one
    # thing that would defeat a restore test.
    log "dumping to ${target}"
    if pg_dump --format=custom --compress=6 --file="${target}.partial" \
        --host="${PGHOST}" --username="${PGUSER}" "${PGDATABASE}"; then
        mv "${target}.partial" "${target}"
        log "wrote ${target} ($(du -h "${target}" | cut -f1))"
    else
        rm -f "${target}.partial"
        log "FAILED; left no partial file behind"
        return 1
    fi

    # Retention. The markdown export is small and kept by the application; these
    # are not, and an unbounded dump directory fills the disk that the FIT archive
    # also lives on.
    deleted=$(find "${BACKUP_DIR}" -maxdepth 1 -name 'coach-*.dump' \
        -mtime "+${BACKUP_KEEP_DAYS}" -print -delete | wc -l)
    [ "${deleted}" -gt 0 ] && log "pruned ${deleted} dump(s) older than ${BACKUP_KEEP_DAYS} days"
    return 0
}

seconds_until_target() {
    now=$(date '+%s')
    today=$(date "+%Y-%m-%d ${BACKUP_AT_HOUR}:${BACKUP_AT_MINUTE}:00")
    at=$(date -d "${today}" '+%s' 2>/dev/null || echo 0)
    if [ "${at}" -le "${now}" ]; then
        at=$((at + 86400))
    fi
    echo $((at - now))
}

log "starting; nightly dump at ${BACKUP_AT_HOUR}:${BACKUP_AT_MINUTE} container-local, \
keeping ${BACKUP_KEEP_DAYS} days in ${BACKUP_DIR}"

# One immediately, so a fresh deployment has a dump before it has been up a day
# and the restore test of OBS-06 has something to restore. Idempotent, so a
# restart does not repeat it.
dump_once || log "the first dump failed; will retry tonight"

while true; do
    wait_s=$(seconds_until_target)
    log "sleeping ${wait_s}s"
    sleep "${wait_s}"
    dump_once || log "dump failed; will retry tomorrow"
done
