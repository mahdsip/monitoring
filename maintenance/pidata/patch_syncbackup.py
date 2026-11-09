#!/usr/bin/env python3
"""One-off patch for pidata:/home/pi/syncbackup.sh (run as root).

- Moves NC_PASSWORD out of the script into a root-only env file.
- curl reads credentials from a 0600 temp config (-K), not from -u (ps-visible).
- Fixes the DAV base (NextCloudPi serves Nextcloud at /, not /nextcloud) and the
  address-book path (addressbooks/users/<user>/).
- Discovers and exports every address book and calendar instead of hard-coded
  collection names (the "personal" calendar's real id is a UUID).

Usage: patch_syncbackup.py SCRIPT SECRET_FILE   (prints the patched script to stdout)
"""
import re
import sys

script_path, secret_file = sys.argv[1], sys.argv[2]
s = open(script_path, encoding="utf-8").read()


def replace_once(old, new, text, regex=False):
    if regex:
        out, n = re.subn(old, new, text, count=1, flags=re.M)
    else:
        n = text.count(old)
        out = text.replace(old, new, 1)
    if n != 1:
        sys.exit(f"patch anchor not found exactly once: {old[:60]!r} (found {n})")
    return out


# 1. Header: no more "export NC_PASSWORD" instructions.
s = replace_once(
    r"^# Before running:\n(#.*\n)*?#      trusted LAN-only NextCloudPi server\.\n",
    "# The Nextcloud app password is NOT stored here: it is read from\n"
    f"# {secret_file} (root-only, mode 600), which contains one line:\n"
    "#   NC_PASSWORD='<app password>'\n"
    "# TLS verification is skipped for this trusted LAN-only NextCloudPi server.\n",
    s, regex=True)

# 2. Password out of the script.
s = replace_once(
    r'^NC_PASSWORD="[^"\n]*"\n',
    f'NC_SECRET_FILE="{secret_file}"\n'
    'NC_PASSWORD=""\n'
    '# shellcheck disable=SC1090\n'
    '[ -r "$NC_SECRET_FILE" ] && . "$NC_SECRET_FILE"\n'
    'NC_CURL_CFG=""\n',
    s, regex=True)

# 3. Base URL.
s = replace_once('NC_BASE_URL="https://$NCP_HOST/nextcloud"', 'NC_BASE_URL="https://$NCP_HOST"', s)

# 4. Cleanup also removes the curl credentials file.
s = replace_once("cleanup() {\n    sync\n", 'cleanup() {\n    [ -n "$NC_CURL_CFG" ] && rm -f "$NC_CURL_CFG"\n    sync\n', s)

# 5. curl credentials from the 0600 config instead of the command line.
s = replace_once('        -u "$NC_USER:$NC_PASSWORD" \\\n', '        -K "$NC_CURL_CFG" \\\n', s)

# 6. Discovery helper after backup_dav().
s = replace_once(
    "log \"============================================================\"\nlog \"BACKUP START: $(date)\"",
    'dav_collections() {\n'
    '    # Child collection names of a DAV home (one per line), safe charset only.\n'
    '    curl --fail --silent --show-error "${curl_tls_args[@]}" -K "$NC_CURL_CFG" \\\n'
    "        -X PROPFIND -H 'Depth: 1' \"$1\" 2>> \"$LOG\" \\\n"
    "        | grep -oE '<d:href>[^<]+</d:href>' \\\n"
    "        | sed -E 's#</?d:href>##g; s#/$##; s#.*/##' \\\n"
    '        | grep -vx "$NC_USER" | grep -E \'^[A-Za-z0-9._-]+$\'\n'
    '}\n\n'
    "log \"============================================================\"\nlog \"BACKUP START: $(date)\"",
    s)

# 7. Replace the hard-coded exports with discovery.
s = replace_once(
    r'^backup_dav "Contacts" \\\n(.*\n)*?    "\$NC_BACKUP_DIR/tasks/tasks_\$BACKUP_STAMP\.ics"\n',
    'if [ -z "$NC_PASSWORD" ]; then\n'
    '    fail "Nextcloud app password missing: create $NC_SECRET_FILE (NC_PASSWORD=...)."\n'
    'else\n'
    '    NC_CURL_CFG=$(mktemp)\n'
    '    chmod 600 "$NC_CURL_CFG"\n'
    '    printf \'user = "%s:%s"\\n\' "$NC_USER" "$NC_PASSWORD" > "$NC_CURL_CFG"\n'
    '    DAV="$NC_BASE_URL/remote.php/dav"\n'
    '    found=0\n'
    '    for c in $(dav_collections "$DAV/addressbooks/users/$NC_USER/"); do\n'
    '        case "$c" in z-*) continue ;; esac   # system / app-generated books\n'
    '        found=1\n'
    '        backup_dav "Contacts ($c)" "$DAV/addressbooks/users/$NC_USER/$c/" \\\n'
    '            "$NC_BACKUP_DIR/contacts/contacts_${c}_$BACKUP_STAMP.vcf"\n'
    '    done\n'
    '    [ "$found" -eq 1 ] || fail "No address books found."\n'
    '    found=0\n'
    '    for c in $(dav_collections "$DAV/calendars/$NC_USER/"); do\n'
    '        case "$c" in inbox|outbox|trashbin|contact_birthdays) continue ;; esac\n'
    '        found=1\n'
    '        if [ "$c" = "$TASKS_COLLECTION" ]; then\n'
    '            backup_dav "Tasks ($c)" "$DAV/calendars/$NC_USER/$c/" \\\n'
    '                "$NC_BACKUP_DIR/tasks/tasks_$BACKUP_STAMP.ics"\n'
    '        else\n'
    '            backup_dav "Calendar ($c)" "$DAV/calendars/$NC_USER/$c/" \\\n'
    '                "$NC_BACKUP_DIR/calendar/calendar_${c}_$BACKUP_STAMP.ics"\n'
    '        fi\n'
    '    done\n'
    '    [ "$found" -eq 1 ] || fail "No calendars found."\n'
    '    rm -f "$NC_CURL_CFG"; NC_CURL_CFG=""\n'
    'fi\n',
    s, regex=True)

# The old per-collection settings are no longer used except TASKS_COLLECTION.
s = replace_once(r'^CONTACTS_COLLECTION="[^"\n]*"\n', "", s, regex=True)
s = replace_once(r'^CALENDAR_COLLECTION="[^"\n]*"\n', "", s, regex=True)
s = replace_once(
    "# Set these to the collection names returned by the PROPFIND commands below.\n"
    "# Do not include a leading or trailing slash.\n",
    "# Address books and calendars are discovered automatically; this one goes\n"
    "# to tasks/ instead of calendar/.\n",
    s)

assert "NC_PASSWORD=\"" not in s.replace('NC_PASSWORD=""', ""), "password literal still present"
sys.stdout.write(s)
