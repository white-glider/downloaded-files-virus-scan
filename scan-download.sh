#!/bin/bash
# Copyright (c) 2018 Felix Almeida (white-glider)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

umask 077
WORK_DIR=
WATCH_PID=
SCAN_PID=

cleanup() {
    for pid in "$WATCH_PID" "$SCAN_PID"; do
        if [[ -n "$pid" ]]; then
            kill "$pid" 2>/dev/null || :
            wait "$pid" 2>/dev/null || :
        fi
    done
    [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
    # Keep the lock pathname: flock is attached to its inode, not its name.
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

notify() {
    notify-send -u critical -c transfer.complete -i dialog-warning -- "$1" "$2" ||
        printf '%s\n' 'scan-download: desktop notification failed.' >&2
}
error() {
    printf 'scan-download: %s\n' "$1" >&2
    notify 'ClamAV: download scan failed' "$1"
}
fail() { error "$1"; exit 2; }

for dependency in inotifywait clamscan flock mktemp mkdir stat awk cat grep; do
    command -v "$dependency" >/dev/null 2>&1 || fail "Missing command: $dependency"
done
# %0 and --no-newline require modern inotify-tools (see README).
inotifywait --help | grep -q -- '--no-newline' ||
    fail 'inotifywait needs --no-newline support; install inotify-tools 4.23.9 or newer.'
DOWNLOADS=${SCAN_DOWNLOAD_DIR:-"$HOME/Downloads"}
[[ -d "$DOWNLOADS" ]] || fail "Downloads directory does not exist: $DOWNLOADS"
# Absolute paths ensure filenames beginning with '-' stay operands.
DOWNLOADS=$(cd -- "$DOWNLOADS" && pwd -P) || fail 'Cannot resolve Downloads directory.'

if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    STATE_DIR="$XDG_RUNTIME_DIR/scan-download"
else
    STATE_DIR="${TMPDIR:-/tmp}/scan-download-$UID"
fi
if ! mkdir -m 700 -- "$STATE_DIR" 2>/dev/null; then
    [[ -d "$STATE_DIR" ]] || fail 'Cannot create the lock directory.'
fi
[[ ! -L "$STATE_DIR" && -O "$STATE_DIR" && $(stat -c %a -- "$STATE_DIR") == 700 ]] ||
    fail 'The lock directory must be owned by this user, mode 700, and not a symlink.'
LOCK="$STATE_DIR/lock"
[[ ! -L "$LOCK" && ( ! -e "$LOCK" || ( -f "$LOCK" && -O "$LOCK" ) ) ]] ||
    fail 'Unsafe lock file.'
exec 9>> "$LOCK" || fail 'Cannot open the lock file.'
flock -n -E 3 9
LOCK_STATUS=$?
case "$LOCK_STATUS" in
    0) ;;
    3) printf '%s\n' 'scan-download: another instance is already running.' >&2; exit 0 ;;
    *) fail 'Cannot acquire the process lock.' ;;
esac

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/scan-download.XXXXXXXXXX") ||
    fail 'Cannot create private temporary storage.'
RSLT="$WORK_DIR/result"

# Keep one producer and one open stream for the lifetime of the monitor.
# NUL framing preserves spaces, newlines, backslashes and wildcard characters.
exec 3< <(exec inotifywait --monitor --recursive --quiet --no-newline \
    --format '%w%f%0' -e close_write -e moved_to -- "$DOWNLOADS" 9>&- 2> "$WORK_DIR/watcher.log")
WATCH_PID=$!
while IFS= read -r -d '' DWNLD <&3; do
    # A temporary download may already have been renamed. The moved_to event
    # supplies its final path. Scan moved directories recursively as well.
    [[ -f "$DWNLD" || -d "$DWNLD" ]] || continue
    clamscan --recursive --no-summary --detect-pua=yes --official-db-only=yes \
        -- "$DWNLD" 3<&- 9>&- > "$RSLT" 2>&1 &
    SCAN_PID=$!
    wait "$SCAN_PID"
    STATUS=$?
    SCAN_PID=
    case "$STATUS" in
        0) ;;
        1)
            THREAT=$(awk '/ FOUND$/ { sub(/^.*: /, ""); sub(/ FOUND$/, ""); print }' "$RSLT")
            TEXT="$THREAT"$'\n'"File: $DWNLD"
            TEXT=${TEXT//&/\&amp;}
            TEXT=${TEXT//</\&lt;}
            TEXT=${TEXT//>/\&gt;}
            notify 'ClamAV: threat detected!' "$TEXT"
            ;;
        *)
            cat -- "$RSLT" >&2
            error "ClamAV failed (exit $STATUS). File was not verified clean: $DWNLD"
            ;;
    esac
done
exec 3<&-
wait "$WATCH_PID"
STATUS=$?
WATCH_PID=
cat -- "$WORK_DIR/watcher.log" >&2
fail "Download monitoring stopped (exit $STATUS). Restart the scanner after resolving the error."
