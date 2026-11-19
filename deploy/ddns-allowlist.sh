#!/usr/bin/env bash
# Allow port 53 (tcp and udp) only from the address a DDNS name resolves to.
#
# Run as root from timefence-ddns.timer. It never opens DNS to the world: it
# only ever adds rules for the current address of one hostname and removes the
# rules it recorded for every address it added before. Every rule it creates
# carries the comment below, and that comment is the only thing it deletes by.
#
# State lives in the state dir: `ip` is the address the firewall is open to and
# `added` lists every address whose rules this helper added and has not removed
# yet (one per line), except the one `ip` names. A new address is written there
# as soon as its rules exist, before any old rule is removed, so a run that dies
# between the two steps leaves a note of the rules it created.
#
# Exit codes: 0 ok, 1 resolution/ufw failure, 2 refused address, 64 usage.
set -eu

COMMENT='timefence-ddns'
DNS_PORT=53
STATE_DIR='/var/lib/timefence-ddns'
DRY_RUN=0
HOSTNAME_ARG=''

usage() {
    cat <<'EOF'
usage: ddns-allowlist.sh <hostname> [--state-dir DIR] [--dry-run]

Keeps the firewall's DNS port open to one address only, the address that
<hostname> currently resolves to. The rules for every address recorded in
DIR/added (default /var/lib/timefence-ddns) are removed once the new ones are
in place, so an address that was only briefly current cannot keep an open port.
EOF
}

log() {
    printf 'ddns-allowlist: %s\n' "$*" >&2
}

die_usage() {
    log "$*"
    usage >&2
    exit 64
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --state-dir)
            [ "$#" -ge 2 ] || die_usage "--state-dir needs a value"
            STATE_DIR=$2
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*)
            die_usage "unknown option: $1"
            ;;
        *)
            [ -z "$HOSTNAME_ARG" ] || die_usage "only one hostname is accepted"
            HOSTNAME_ARG=$1
            shift
            ;;
    esac
done

[ -n "$HOSTNAME_ARG" ] || die_usage "a DDNS hostname is required"

name_re='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'
if ! [[ "$HOSTNAME_ARG" =~ $name_re ]]; then
    log "refusing hostname with unexpected characters: $HOSTNAME_ARG"
    exit 64
fi

# One IPv4 address, newest first. An empty result is a failure, never "allow all".
line=$(getent ahostsv4 "$HOSTNAME_ARG" 2>/dev/null | head -n 1)
ip=$(printf '%s\n' "$line" | cut -d' ' -f1)

if [ -z "$ip" ]; then
    log "could not resolve $HOSTNAME_ARG; leaving the firewall untouched"
    exit 1
fi

if ! [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    log "resolution for $HOSTNAME_ARG is not an IPv4 address: $ip; leaving the firewall untouched"
    exit 1
fi

refused() {
    case "$1" in
        0.*|127.*|10.*|192.168.*|169.254.*) return 0 ;;
        172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
        100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0 ;;
        22[4-9].*|2[3-9][0-9].*) return 0 ;;
    esac
    return 1
}

if refused "$ip"; then
    log "refusing non-public address $ip for $HOSTNAME_ARG; leaving the firewall untouched"
    exit 2
fi

# Addresses whose rules must go: the recorded current address, plus anything an
# earlier run added and has not removed. Only well-formed IPv4 lines are used,
# so a damaged state file cannot put arguments into a ufw command.
old_ip=''
targets=()
seen=' '

add_target() {
    # A state file copied from Windows may end its lines with CR; only the
    # leading dotted-quad part is ever used.
    candidate=${1%%[!0-9.]*}
    [ -n "$candidate" ] || return 0
    [[ "$candidate" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 0
    [ "$candidate" = "$ip" ] && return 0
    case "$seen" in *" $candidate "*) return 0 ;; esac
    seen="$seen$candidate "
    targets+=("$candidate")
}

collect_state() {
    old_ip=''
    targets=()
    seen=' '
    if [ -f "$STATE_DIR/ip" ]; then
        old_ip=$(cat "$STATE_DIR/ip")
        old_ip=${old_ip%%[!0-9.]*}
    fi
    add_target "$old_ip"
    if [ -f "$STATE_DIR/added" ]; then
        while IFS= read -r recorded; do
            add_target "$recorded"
        done <"$STATE_DIR/added"
    fi
}

collect_state

if [ "$ip" = "$old_ip" ] && [ "${#targets[@]}" -eq 0 ]; then
    log "$HOSTNAME_ARG is still $ip; no firewall change"
    exit 0
fi

if [ "$DRY_RUN" -eq 0 ]; then
    umask 077
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$STATE_DIR/lock"
        flock 9
        collect_state
        if [ "$ip" = "$old_ip" ] && [ "${#targets[@]}" -eq 0 ]; then
            log "$HOSTNAME_ARG is still $ip; no firewall change"
            exit 0
        fi
    else
        log "notice: flock not found (util-linux); running without the state lock"
    fi
fi

# One command per line, so a dry run shows exactly what would run.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        log "would run: $*"
        return 0
    fi
    "$@"
}

kept=()

# Record the address as soon as a rule for it exists: a run that dies before it
# can rewrite the state file still leaves a note of what it created.
note_added() {
    [ "$DRY_RUN" -eq 0 ] || return 0
    printf '%s\n' "$1" >>"$STATE_DIR/added"
}

# Write the state: every address that still holds a rule, never the address the
# `ip` file names. The first argument is the address the firewall is open to
# after this run (the previous one when the add was incomplete); the second one
# `include-recorded` keeps the addresses recorded earlier (used when no removal
# was attempted, so every recorded rule is still in place).
write_state() {
    STATE_IP=$1
    {
        if [ "${2:-}" = "include-recorded" ] && [ -f "$STATE_DIR/added" ]; then
            cat "$STATE_DIR/added"
        fi
        for address in "${kept[@]:-}"; do
            [ -n "$address" ] && printf '%s\n' "$address"
        done
    } | awk 'NF' | sort -u | grep -vx "$STATE_IP" >"$STATE_DIR/added.new" || true
    printf '%s\n' "$STATE_IP" >"$STATE_DIR/ip.new"
    chmod 600 "$STATE_DIR/added.new" "$STATE_DIR/ip.new"
    mv "$STATE_DIR/added.new" "$STATE_DIR/added"
    mv "$STATE_DIR/ip.new" "$STATE_DIR/ip"
}

failed=0

# Add first: if this fails the old rule still works. Rules for the address that
# is already current are left alone; ufw would only skip them anyway.
if [ "$ip" != "$old_ip" ]; then
    added_ok=0
    for proto in udp tcp; do
        if run ufw allow from "$ip" to any port "$DNS_PORT" proto "$proto" comment "$COMMENT"; then
            added_ok=$((added_ok + 1))
            note_added "$ip"
        else
            log "could not add the $proto rule for $ip"
        fi
    done
    if [ "$added_ok" -lt 2 ]; then
        log "the new rules are incomplete; keeping the previous ones for the next run"
        if [ "$DRY_RUN" -eq 0 ]; then
            write_state "$old_ip" include-recorded
        fi
        exit 1
    fi
fi

# Record every address added before deleting, so a run that dies after the add
# still leaves a note of the rule it created.
for target in "${targets[@]:-}"; do
    [ -n "$target" ] || continue
    for proto in udp tcp; do
        if run ufw delete allow from "$target" to any port "$DNS_PORT" proto "$proto" comment "$COMMENT"; then
            :
        else
            log "could not remove the rule for the previous address $target"
            failed=1
            kept+=("$target")
        fi
    done
done

if [ "$failed" -ne 0 ]; then
    log "keeping the addresses that still have rules so the next run retries"
    if [ "$DRY_RUN" -eq 0 ]; then
        write_state "$ip"
    fi
    exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    log "would record $ip for $HOSTNAME_ARG (was ${old_ip:-none})"
    exit 0
fi

write_state "$ip"
log "$HOSTNAME_ARG is now $ip (was ${old_ip:-none}); DNS allowed from it only"
