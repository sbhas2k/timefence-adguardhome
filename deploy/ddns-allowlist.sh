#!/usr/bin/env bash
# Allow port 53 (tcp and udp) only from the address a DDNS name resolves to.
#
# Run as root from timefence-ddns.timer. It never opens DNS to the world: it
# only ever adds rules for the current address of one hostname and removes the
# rules it recorded for the previous address. Every rule it creates carries the
# comment below, and that comment is the only thing it deletes by.
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
<hostname> currently resolves to. The previous address is read from
DIR/ip (default /var/lib/timefence-ddns) and its rules are removed after the
new ones are in place.
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

old_ip=''
if [ -f "$STATE_DIR/ip" ]; then
    old_ip=$(cat "$STATE_DIR/ip")
fi

if [ "$ip" = "$old_ip" ]; then
    log "$HOSTNAME_ARG is still $ip; no firewall change"
    exit 0
fi

if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$STATE_DIR/lock"
        flock 9
        old_ip=''
        if [ -f "$STATE_DIR/ip" ]; then
            old_ip=$(cat "$STATE_DIR/ip")
        fi
        if [ "$ip" = "$old_ip" ]; then
            log "$HOSTNAME_ARG is still $ip; no firewall change"
            exit 0
        fi
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

# Add first: if this fails the old rule still works.
for proto in udp tcp; do
    run ufw allow from "$ip" to any port "$DNS_PORT" proto "$proto" comment "$COMMENT"
done

failed=0
if [ -n "$old_ip" ]; then
    for proto in udp tcp; do
        if ! run ufw delete allow from "$old_ip" to any port "$DNS_PORT" proto "$proto" comment "$COMMENT"; then
            log "could not remove the rule for the previous address $old_ip"
            failed=1
        fi
    done
fi

if [ "$failed" -ne 0 ]; then
    log "keeping $old_ip in the state file so the next run retries the removal"
    exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    log "would record $ip for $HOSTNAME_ARG (was ${old_ip:-none})"
    exit 0
fi

printf '%s\n' "$ip" >"$STATE_DIR/ip.new"
chmod 600 "$STATE_DIR/ip.new"
mv "$STATE_DIR/ip.new" "$STATE_DIR/ip"
log "$HOSTNAME_ARG is now $ip (was ${old_ip:-none}); DNS allowed from it only"
