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
LOCK=${XDG_RUNTIME_DIR:-/tmp}/scan-download.lock
RSLT=/tmp/$$_clamav.tmp

function tidy_up {
	[ -f $LOCK ] && rm -f $LOCK
	[ -f $RSLT ] && rm -f $RSLT
}

[ "$_FLOCKER" != "$LOCK" ] && exec env _FLOCKER="$LOCK" flock -en "$LOCK" "$0"
trap "tidy_up 2>/dev/null" EXIT

while message="$(inotifywait -qr -e close_write -e moved_to $HOME/Downloads)"; do
	DWNLD="$(echo $message | cut -d' ' --output-delimiter= -f1,3-)"
	sleep 1
	if [ -f "$DWNLD" -a -s "$DWNLD" ]; then
		#clamdscan --no-summary $DWNLD > $RSLT
		clamscan --no-summary --detect-pua=yes --official-db-only=yes $DWNLD > $RSLT
		if [ $? -eq 1 ]; then
			THREAT=$(awk '$NF ~ /FOUND/ {print $2}' $RSLT)
			notify-send -u critical -c transfer.complete -i /usr/share/icons/Adwaita/scalable/status/dialog-warning-symbolic.svg "ClamAV: threat detected!" "$THREAT\nFile: $DWNLD"
		fi
		rm $RSLT
	fi
done
