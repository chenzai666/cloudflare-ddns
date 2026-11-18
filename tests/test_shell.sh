#!/bin/bash
# 无网络回归测试，使用模拟的 curl 和 crontab。
set -eo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
source "$SCRIPT_DIR/cloudflare_ddns.sh"
TASK_DIR=$(mktemp -d)
CONFIG_FILE="$TASK_DIR/config"
PYTHON_FILE="$TASK_DIR/python.json"
MANAGER_FILE="$TASK_DIR/manager.json"
DEFAULT_LOG="$TASK_DIR/cloudflare_ddns.log"
CUSTOM_LOG="$TASK_DIR/legacy-custom.log"
CRONTAB_FILE="$TASK_DIR/crontab"
REQUEST_FILE="$TASK_DIR/requests"
TEST_LOG_SUFFIX=".cloudflare-ddns-sh.$(date '+%Y-%m-%d')"
EXPIRED_LOG="$DEFAULT_LOG.cloudflare-ddns-sh.2000-01-01"
OTHER_LOG="$DEFAULT_LOG.cloudflare-ddns-py.2000-01-01"

cleanup() {
    rm -f -- "$CONFIG_FILE" "$MANAGER_FILE" "$DEFAULT_LOG" "$CUSTOM_LOG" "$CRONTAB_FILE" "$REQUEST_FILE" \
        "$DEFAULT_LOG$TEST_LOG_SUFFIX" "$EXPIRED_LOG" "$OTHER_LOG"
    rmdir -- "$TASK_DIR"
}
trap cleanup EXIT

assert() {
    if ! "$@"; then
        printf '检查失败：%s\n' "$*" >&2
        exit 1
    fi
}

cat > "$CONFIG_FILE" <<'CONFIG'
#!/bin/bash
API_TOKEN='test-token'
ZONE_ID='11111111111111111111111111111111'
RECORD_NAME='ddns.example.com'
RECORD_TYPE='A'
TTL='60'
BASE_DOMAIN='example.com'
CONFIG
printf "LOG_FILE='%s'\n" "$CUSTOM_LOG" >> "$CONFIG_FILE"

SOURCE='{"id":"source","name":"ddns.example.com","type":"A","content":"192.0.2.1","ttl":1,"proxied":true}'
OTHER='{"id":"one","name":"one.example.com","type":"A","content":"192.0.2.3","ttl":60,"proxied":false}'
SECOND='{"id":"two","name":"two.example.com","type":"A","content":"192.0.2.4","ttl":60,"proxied":false}'
PROXIED='{"id":"proxy","name":"proxy.example.com","type":"A","content":"192.0.2.5","ttl":1,"proxied":true}'
MOCK=''

api_body() {
    printf '%s\n200' "$1"
}

curl() {
    local method='' payload='' url='' line
    while (( $# )); do
        case "$1" in
            --request) method="$2"; shift ;;
            --data-binary) payload="$2"; shift ;;
            https://*) url="$1" ;;
        esac
        shift
    done
    if [[ $url == https://api.ipify.org ]]; then
        printf '%s\n' '192.0.2.9'
        return 0
    fi
    [[ $url == https://api.cloudflare.com/client/v4/zones/* ]] || return 1
    read -r line || return 1
    if [[ $MOCK == manager ]]; then
        [[ $line == 'Authorization: Bearer manager-token' && $url == *'/22222222222222222222222222222222/'* ]] || return 1
    else
        [[ $line == 'Authorization: Bearer test-token' ]] || return 1
    fi
    printf '%s\t%s\t%s\n' "$method" "$url" "$payload" >> "$REQUEST_FILE"
    if [[ $method == PATCH && ( $MOCK == patch_error && $url == */two ||
        $MOCK == ddns_error && $url == */source ) ]]; then
        printf '%s\n403' '{"success":false,"errors":[{"message":"test write failure"}]}'
    elif [[ $method != GET ]]; then
        api_body '{"success":true}'
    elif [[ $MOCK == multiple ]]; then
        api_body "{\"success\":true,\"result\":[$SOURCE,$SOURCE]}"
    elif [[ $url == *'&name=ddns.example.com&'* ]]; then
        api_body "{\"success\":true,\"result\":[$SOURCE]}"
    elif [[ $url == *'&name='* ]]; then
        if [[ $url == *'&name=one.example.com&'* ]]; then
            api_body "{\"success\":true,\"result\":[$OTHER]}"
        else
            api_body '{"success":true,"result":[]}'
        fi
    elif [[ $MOCK == match ]]; then
        api_body "{\"success\":true,\"result\":[$SOURCE,$PROXIED]}"
    elif [[ $url == *'&page=1' ]]; then
        api_body "{\"success\":true,\"result\":[$SOURCE,$OTHER,$PROXIED],\"result_info\":{\"page\":1,\"total_pages\":2}}"
    elif [[ $url == *'&page=2' && $MOCK == page_error ]]; then
        printf '%s\n403' '{"success":false,"errors":[{"message":"test failure"}]}'
    elif [[ $url == *'&page=2' ]]; then
        api_body "{\"success\":true,\"result\":[$SECOND],\"result_info\":{\"page\":2,\"total_pages\":2}}"
    else
        return 1
    fi
}

crontab() {
    if [[ $1 == -l && $MOCK == cron_error ]]; then
        printf '%s\n' 'Permission denied' >&2
        return 1
    fi
    if [[ $1 == -l && $MOCK == cron_missing ]]; then
        printf "crontab: can't open '%s': No such file or directory\n" "$(id -un)" >&2
        return 1
    fi
    case "$1" in
        -l) cat "$CRONTAB_FILE" ;;
        -) cat > "$CRONTAB_FILE" ;;
        *) return 1 ;;
    esac
}

assert test "$(normalize_ip AAAA '2001:DB8::1')" = "$(normalize_ip AAAA '2001:0db8:0:0:0:0:0:1')"
assert test "$(normalize_ip AAAA '::192.0.2.1')" = "$(normalize_ip AAAA '::c000:201')"
if normalize_ip A '256.0.0.1' >/dev/null; then exit 1; fi
if normalize_ip AAAA '2001:::1' >/dev/null; then exit 1; fi

manual_out=$(main)
assert test ! -e "$DEFAULT_LOG"
assert test ! -e "$CUSTOM_LOG"
assert grep -F '已更新 ddns.example.com → 192.0.2.9' <<< "$manual_out"
assert grep -F $'PATCH\thttps://api.cloudflare.com/client/v4/zones/11111111111111111111111111111111/dns_records/source\t{"content":"192.0.2.9"}' "$REQUEST_FILE"

: > "$REQUEST_FILE"
preview=$(main sync --dry-run)
assert grep -F 'DDNS 源记录：ddns.example.com (A) → 192.0.2.1' <<< "$preview"
assert grep -F 'two.example.com (A)  192.0.2.4 → 192.0.2.1' <<< "$preview"
assert grep -F '&page=2' "$REQUEST_FILE"
if grep -q '^PATCH' "$REQUEST_FILE"; then exit 1; fi

: > "$REQUEST_FILE"
if (MOCK=page_error; main sync) 2>/dev/null; then exit 1; fi
if grep -q '^PATCH' "$REQUEST_FILE"; then exit 1; fi

: > "$REQUEST_FILE"
(main sync) >/dev/null
assert test "$(grep -c '^PATCH' "$REQUEST_FILE")" -eq 2
if grep -q '/dns_records/proxy' "$REQUEST_FILE"; then exit 1; fi

: > "$REQUEST_FILE"
no_change=$( (
    MOCK=match
    load_config sync
    sync_records A confirm
    read -r pending
    assert test "$pending" = y
) <<< y)
assert grep -F '没有需要更新的记录。本次未写入 Cloudflare。' <<< "$no_change"
if grep -q '^PATCH' "$REQUEST_FILE"; then exit 1; fi

: > "$REQUEST_FILE"
confirmed=$(load_config sync && sync_records A confirm <<< y)
assert grep -F '同步完成，成功 2 条，失败 0 条。' <<< "$confirmed"
assert test "$(grep -c '^GET' "$REQUEST_FILE")" -eq 3
assert test "$(grep -c '^PATCH' "$REQUEST_FILE")" -eq 2
if patch_result=$(MOCK=patch_error main sync 2>&1); then exit 1; fi
assert grep -F '更新成功：one.example.com' <<< "$patch_result"
assert grep -F '更新失败：two.example.com' <<< "$patch_result"
assert grep -F '同步完成，成功 1 条，失败 1 条。' <<< "$patch_result"

: > "$REQUEST_FILE"
(main add @ --type CNAME --ip dns.example.net --ttl auto) >/dev/null
assert grep -F '{"type":"CNAME","name":"example.com","content":"dns.example.net","ttl":1,"proxied":false}' "$REQUEST_FILE"
(main update one --type A --ttl 300 --proxied) >/dev/null
assert grep -F '{"ttl":300,"proxied":true}' "$REQUEST_FILE"
(main delete one --type A) >/dev/null
assert grep -F $'DELETE\thttps://api.cloudflare.com/client/v4/zones/11111111111111111111111111111111/dns_records/one' "$REQUEST_FILE"

: > "$REQUEST_FILE"
if (MOCK=multiple; main delete one --type A) 2>/dev/null; then exit 1; fi
if grep -q '^DELETE' "$REQUEST_FILE"; then exit 1; fi

printf '%s\n' '{"API_TOKEN":"manager-token","ZONE_ID":"22222222222222222222222222222222","BASE_DOMAIN":"example.com","DDNS_RECORD":"ddns.example.com"}' > "$MANAGER_FILE"
(MOCK=manager; main sync --dry-run) >/dev/null

: > "$REQUEST_FILE"
(main ddns --sync-others) >/dev/null
assert test "$(grep -c '^PATCH' "$REQUEST_FILE")" -eq 3
assert test "$(grep -c '192.0.2.9' "$REQUEST_FILE")" -eq 3
if grep -q '22222222222222222222222222222222\|/dns_records/proxy' "$REQUEST_FILE"; then exit 1; fi
: > "$REQUEST_FILE"
if (MOCK=ddns_error; main ddns --sync-others) 2>/dev/null; then exit 1; fi
assert test "$(grep -c '^GET' "$REQUEST_FILE")" -eq 1
assert test "$(grep -c '^PATCH' "$REQUEST_FILE")" -eq 1

printf '%s\n' 'MAILTO=ops@example.org' '7 * * * * /opt/unrelated' '9 * * * * /opt/python ddns # cloudflare-ddns:py' \
    '*/15 * * * * /opt/manual/cloudflare_ddns.sh ddns >> /var/log/manual-ddns.log 2>&1' > "$CRONTAB_FILE"
cron_before=$(cat "$CRONTAB_FILE")
printf '%s\n' '*/10 * * * * /opt/old/cloudflare_ddns.sh ddns # cloudflare-ddns:sh' \
    '0 3 * * * /opt/old/cloudflare_ddns.sh clean-logs # cloudflare-ddns:sh-logs' >> "$CRONTAB_FILE"
schedule 5 >/dev/null
assert test "$(grep -c 'cloudflare-ddns:sh' "$CRONTAB_FILE")" -eq 2
assert grep -F '*/5 * * * * umask 077;' "$CRONTAB_FILE"
assert grep -F "ddns >> $(cron_path "$DEFAULT_LOG") 2>&1 # cloudflare-ddns:sh" "$CRONTAB_FILE"
assert grep -E '^0 3 .* clean-logs # cloudflare-ddns:sh-logs$' "$CRONTAB_FILE"
schedule 5 --sync-others >/dev/null
assert grep -F "ddns --sync-others >> $(cron_path "$DEFAULT_LOG") 2>&1 # cloudflare-ddns:sh" "$CRONTAB_FILE"
assert test "$(grep -c 'cloudflare-ddns:sh' "$CRONTAB_FILE")" -eq 2
schedule 60 >/dev/null
assert grep -E '^0 \* \* \* \* umask 077;.* ddns >> .* 2>&1 # cloudflare-ddns:sh$' "$CRONTAB_FILE"
assert test "$(grep -c 'cloudflare-ddns:sh' "$CRONTAB_FILE")" -eq 2
schedule off >/dev/null
assert test "$(cat "$CRONTAB_FILE")" = "$cron_before"
if (MOCK=cron_error; schedule 5) 2>/dev/null; then exit 1; fi
assert test "$(cat "$CRONTAB_FILE")" = "$cron_before"
if (CONFIG_FILE="$TASK_DIR/missing"; schedule 5) 2>/dev/null; then exit 1; fi
assert test "$(cat "$CRONTAB_FILE")" = "$cron_before"
assert test "$(cron_path "/opt/sp ace%foo's.sh")" = "'/opt/sp ace\\%foo'\\''s.sh'"
MOCK=cron_missing schedule 5 >/dev/null
assert test "$(grep -c 'cloudflare-ddns:sh' "$CRONTAB_FILE")" -eq 2
if bash "$SCRIPT_DIR/cloudflare_ddns.sh" unknown-command >/dev/null 2>&1; then
    printf '未知命令没有返回错误。\n' >&2
    exit 1
fi

printf 'active log\n' > "$DEFAULT_LOG"
printf 'legacy custom log\n' > "$CUSTOM_LOG"
printf 'expired log\n' > "$EXPIRED_LOG"
printf 'other log\n' > "$OTHER_LOG"
touch -t 200001010000 "$EXPIRED_LOG" "$OTHER_LOG"
clean_logs
assert test -f "$DEFAULT_LOG$TEST_LOG_SUFFIX"
assert test ! -e "$DEFAULT_LOG"
assert test ! -e "$EXPIRED_LOG"
assert test -f "$OTHER_LOG"
assert test "$(cat "$CUSTOM_LOG")" = 'legacy custom log'

printf 'SH 离线测试通过。\n'
