#!/bin/bash
# Cloudflare DDNS 和 DNS 记录管理
set -o pipefail

CFG_DIR="$HOME/.cloudflare_ddns"
CONFIG_FILE="$CFG_DIR/config"
PYTHON_FILE="$CFG_DIR/config.json"
MANAGER_FILE="$HOME/.cf_dns_manager/config.json"
DEFAULT_LOG="$CFG_DIR/cloudflare_ddns.log"
CRON_MARKER='# cloudflare-ddns:sh'
LOG_CRON_MARKER='# cloudflare-ddns:sh-logs'

API_TOKEN='' ZONE_ID='' RECORD_NAME='' RECORD_TYPE='' TTL='' BASE_DOMAIN=''
LOG_FILE="$DEFAULT_LOG" MTR_TARGET=''

usage() {
    cat <<'HELP'
用法：bash cloudflare_ddns.sh [命令] [参数]
  menu                     打开交互菜单（终端内无参数时默认打开）
  ddns                     更新 DDNS 主记录（非交互环境默认执行）
  configure                创建或修改配置
  add 前缀 [--type A|AAAA|CNAME] [--ip 地址] [--ttl auto|秒] [--proxied]
  update 前缀 [--type A|AAAA|CNAME] [--ip 地址] [--ttl auto|秒] [--proxied|--no-proxied]
  delete 前缀 [--type A|AAAA|CNAME]
  sync [--type A|AAAA] [--dry-run]
  schedule [status|off|1–60]
  clean-logs               轮转 SH 版日志，清理满 7 天的轮转文件
  -reconfig                清除 SH 配置，下次运行重新配置
  -delete                  清除 SH 配置和默认日志
  -h, --help               显示帮助

@ 表示区域根域名。sync 将同类型、未代理的记录对齐到 Cloudflare 上的 DDNS 源记录。
HELP
}

fail() {
    printf '%s\n' "$*" >&2
    return 1
}

log() {
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S') - $*"
    printf '%s\n' "$line"
    if [[ -n $LOG_FILE && -d $(dirname -- "$LOG_FILE") ]]; then
        (umask 077; printf '%s\n' "$line" >> "$LOG_FILE") || return 1
    fi
}

normalize_name() {
    local name="$1" label
    local -a labels
    name="${name%.}"
    [[ $name && $name != .* && $name != *. && $name != *..* && ${#name} -le 253 ]] || return 1
    if [[ ${2:-} == wildcard && $name == '*.'* ]]; then
        IFS='.' read -r -a labels <<< "${name#*.}"
    else
        IFS='.' read -r -a labels <<< "$name"
    fi
    for label in "${labels[@]}"; do
        [[ $label =~ ^[a-zA-Z0-9_]([a-zA-Z0-9_-]{0,61}[a-zA-Z0-9_])?$ ]] || return 1
    done
    jq -rn --arg name "$name" '$name | ascii_downcase'
}

normalize_ip() {
    local kind="$1" address="$2" octet part left right output='' tail dotted
    local -a ipv4=() first=() last=()
    if [[ $kind == A ]]; then
        [[ $address =~ ^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$ ]] || return 1
        IFS='.' read -r -a ipv4 <<< "$address"
        for octet in "${ipv4[@]}"; do (( octet <= 255 )) || return 1; done
        printf '%s\n' "$address"
        return
    fi
    [[ $kind == AAAA && $address == *:* ]] || return 1
    if [[ $address == *.* ]]; then
        dotted="$(normalize_ip A "${address##*:}")" || return 1
        IFS='.' read -r -a ipv4 <<< "$dotted"
        printf -v tail '%x:%x' "$((ipv4[0]*256+ipv4[1]))" "$((ipv4[2]*256+ipv4[3]))"
        address="${address%:*}:$tail"
    fi
    [[ $address =~ ^[0-9A-Fa-f:]+$ ]] || return 1
    if [[ $address == *::* ]]; then
        left="${address%%::*}" right="${address#*::}"
        [[ $left != :* && $left != *: && $right != :* && $right != *: && $right != *::* ]] || return 1
        [[ -z $left ]] || IFS=':' read -r -a first <<< "$left"
        [[ -z $right ]] || IFS=':' read -r -a last <<< "$right"
        (( ${#first[@]} + ${#last[@]} < 8 )) || return 1
        while (( ${#first[@]} + ${#last[@]} < 8 )); do first+=(0); done
    else
        [[ $address != :* && $address != *: ]] || return 1
        IFS=':' read -r -a first <<< "$address"
        (( ${#first[@]} == 8 )) || return 1
    fi
    for part in "${first[@]}" "${last[@]}"; do
        [[ $part =~ ^[[:xdigit:]]{1,4}$ ]] || return 1
        printf -v tail '%04x' "$((16#$part))"
        output="${output:+$output:}$tail"
    done
    printf '%s\n' "$output"
}

normalize_ttl() {
    local ttl="$1"
    [[ $ttl != auto ]] || ttl=1
    [[ $ttl =~ ^[0-9]{1,5}$ ]] && (( 10#$ttl >= 1 && 10#$ttl <= 86400 )) || return 1
    printf '%d\n' "$((10#$ttl))"
}

check_credentials() {
    [[ $ZONE_ID =~ ^[[:xdigit:]]{32}$ ]] || { fail "Zone ID 应为 32 位十六进制字符"; return; }
    [[ $API_TOKEN && $API_TOKEN != *[[:space:]]* ]] || fail "请填写有效的 API Token"
}

load_assignments() {
    local key value line
    while IFS= read -r line || [[ -n $line ]]; do
        line="${line%$'\r'}"
        [[ -n $line && $line != '#'* ]] || continue
        [[ $line == *=* ]] || { fail "配置文件中有无效的配置行：$CONFIG_FILE"; return; }
        key="${line%%=*}" value="${line#*=}"
        case "$key" in
            API_TOKEN|ZONE_ID|RECORD_NAME|RECORD_TYPE|TTL|BASE_DOMAIN|LOG_FILE|MTR_TARGET)
                if [[ $value == \'*\' && ${#value} -ge 2 ]]; then
                    value="${value:1:${#value}-2}"
                fi
                printf -v "$key" '%s' "$value"
                ;;
        esac
    done < "$CONFIG_FILE"
}

load_json_config() {
    local file="$1" assignments key line
    assignments=$(jq -er '
        if type != "object" then error("配置应为 JSON 对象") else . end
        | {API_TOKEN, ZONE_ID, BASE_DOMAIN, RECORD_TYPE, LOG_FILE, MTR_TARGET,
           RECORD_NAME: (.DDNS_RECORD // .RECORD_NAME),
           TTL: (.DEFAULT_TTL // .TTL)}
        | to_entries[]
        | select(.value != null)
        | .value |= tostring
        | if (.value | test("[\r\n\u0000]")) then error("配置含控制字符") else . end
        | "\(.key)=\(.value)"' "$file") || return 1
    while IFS= read -r line; do
        key="${line%%=*}"
        printf -v "$key" '%s' "${line#*=}"
    done <<< "$assignments"
}

configure() {
    local input temp
    [[ -t 0 ]] || { fail "请在交互终端运行 configure"; return; }
    if [[ -e $CONFIG_FILE ]]; then
        [[ ! -L $CONFIG_FILE ]] || { fail "请直接维护配置文件指向的位置"; return; }
        load_assignments || return 1
    elif [[ -e $PYTHON_FILE ]]; then
        load_json_config "$PYTHON_FILE" || return 1
    fi
    if [[ $API_TOKEN ]]; then
        read -rs -p 'API Token（回车保留已有 Token）: ' input || return 1
    else
        read -rs -p 'Cloudflare API Token: ' input || return 1
    fi
    printf '\n'
    API_TOKEN="${input:-$API_TOKEN}"
    read -r -p "Zone ID [${ZONE_ID:-必填}]: " input || return 1
    ZONE_ID="${input:-$ZONE_ID}"
    check_credentials || return 1
    read -r -p "DDNS 完整域名 [${RECORD_NAME:-ddns.example.com}]: " input || return 1
    RECORD_NAME="$(normalize_name "${input:-${RECORD_NAME:-ddns.example.com}}" wildcard)" || { fail "DDNS 域名无效"; return; }
    read -r -p "记录类型 A/AAAA [${RECORD_TYPE:-A}]: " input || return 1
    RECORD_TYPE="${input:-${RECORD_TYPE:-A}}"
    [[ $RECORD_TYPE == A || $RECORD_TYPE == AAAA ]] || { fail "记录类型须为 A 或 AAAA"; return; }
    read -r -p "TTL [${TTL:-60}]: " input || return 1
    TTL="$(normalize_ttl "${input:-${TTL:-60}}")" || { fail "TTL 无效"; return; }
    read -r -p "区域域名 [${BASE_DOMAIN:-留空时根据 Zone ID 查询}]: " input || return 1
    BASE_DOMAIN="${input:-$BASE_DOMAIN}"
    if [[ $BASE_DOMAIN ]]; then
        BASE_DOMAIN="$(normalize_name "$BASE_DOMAIN")" || { fail "区域域名无效"; return; }
    fi
    read -r -p "日志路径 [$LOG_FILE]: " input || return 1
    LOG_FILE="${input:-$LOG_FILE}"
    read -r -p "MTR 目标 [${MTR_TARGET:-跳过，输入 - 清除}]: " input || return 1
    MTR_TARGET="${input:-$MTR_TARGET}"
    [[ $MTR_TARGET != - ]] || MTR_TARGET=''
    read -r -p '保存配置？[y/N]: ' input || return 1
    [[ $input == [yY] ]] || { printf '已取消保存。\n'; return 1; }
    (umask 077; mkdir -p -- "$CFG_DIR") || return 1
    temp=$(mktemp "$CFG_DIR/.config.XXXXXX") || return 1
    if ! printf "API_TOKEN='%s'\nZONE_ID='%s'\nRECORD_NAME='%s'\nRECORD_TYPE='%s'\nTTL='%s'\nBASE_DOMAIN='%s'\nLOG_FILE='%s'\nMTR_TARGET='%s'\n" \
        "$API_TOKEN" "$ZONE_ID" "$RECORD_NAME" "$RECORD_TYPE" "$TTL" "$BASE_DOMAIN" "$LOG_FILE" "$MTR_TARGET" > "$temp" ||
        ! mv -f -- "$temp" "$CONFIG_FILE"; then
        rm -f -- "$temp"
        return 1
    fi
    printf '配置已保存：%s\n' "$CONFIG_FILE"
}

load_config() {
    local command="$1"
    if [[ $command != ddns && -e $MANAGER_FILE ]]; then
        load_json_config "$MANAGER_FILE" || return 1
        TTL="${TTL:-1}"
    elif [[ -e $CONFIG_FILE ]]; then
        load_assignments || return 1
    elif [[ -e $PYTHON_FILE ]]; then
        load_json_config "$PYTHON_FILE" || return 1
    else
        configure || return 1
    fi
    check_credentials
}

api() {
    local method="$1" endpoint="${2:-}" data="${3:-}" reply status body reason
    local -a args=(--request "$method" --header @- --header 'Content-Type: application/json')
    [[ ! $data ]] || args+=(--data-binary "$data")
    # 认证头走标准输入，避免进入 curl 的命令行参数。
    reply=$(printf 'Authorization: Bearer %s\n' "$API_TOKEN" |
        curl -q --silent --show-error --proto '=https' --connect-timeout 5 --max-time 15 \
        --write-out $'\n%{http_code}' "${args[@]}" \
        "https://api.cloudflare.com/client/v4/zones/$ZONE_ID${endpoint:+/$endpoint}") || return 1
    status="${reply##*$'\n'}" body="${reply%$'\n'*}"
    if [[ ! $status =~ ^2[0-9][0-9]$ ]] || ! jq -e '.success == true' <<< "$body" >/dev/null; then
        reason=$(jq -r '[.errors[]?.message] | join("；")' <<< "$body" 2>/dev/null)
        fail "Cloudflare 请求失败（HTTP $status）${reason:+：$reason}"
        return 1
    fi
    printf '%s\n' "$body"
}

get_ip() {
    local kind="$1" family=4 service candidate output
    local -a services=(https://api.ipify.org https://ipv4.icanhazip.com)
    if [[ $kind == AAAA ]]; then
        family=6
        services=(https://api6.ipify.org https://ipv6.icanhazip.com https://v6.ident.me)
    elif [[ $MTR_TARGET =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] && command -v mtr >/dev/null && command -v timeout >/dev/null; then
        output=$(timeout 15 mtr -4 -n -rw -c 1 "$MTR_TARGET" 2>/dev/null)
        if [[ $output =~ [[:space:]]1\.\|--[[:space:]]+([0-9.]+) ]]; then
            candidate="${BASH_REMATCH[1]}"
            if normalize_ip A "$candidate" >/dev/null; then
                printf '%s\n' "$candidate"
                return
            fi
        fi
    fi
    for service in "${services[@]}"; do
        candidate=$(curl -q "-$family" --silent --fail --proto '=https' \
            --connect-timeout 5 --max-time 10 "$service") || continue
        if normalize_ip "$kind" "$candidate" >/dev/null; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    fail "无法获取 $kind 公网地址"
}

list_records() {
    local name="$1" kind="$2" page=1 reply count total current query
    query=$(jq -rn --arg name "$name" --arg kind "$kind" '
        "dns_records?per_page=100" + (if $name == "" then "" else "&name=" + ($name | @uri) end)
        + (if $kind == "" then "" else "&type=" + ($kind | @uri) end)') || return 1
    while :; do
        reply=$(api GET "$query&page=$page") || return 1
        count=$(jq -er '.result | if type == "array" then length else error("返回记录格式无效") end' <<< "$reply") || return 1
        jq -c '.result[]' <<< "$reply" || return 1
        total=$(jq -r '.result_info.total_pages // empty' <<< "$reply") || return 1
        current=$(jq -r '.result_info.page // empty' <<< "$reply") || return 1
        if [[ $current && $current != "$page" ]]; then
            fail "Cloudflare 返回的分页位置无效"
            return
        fi
        if [[ $total ]]; then
            [[ $total =~ ^[0-9]{1,6}$ ]] || { fail "Cloudflare 返回的分页总数无效"; return; }
            (( page < 10#$total )) || return 0
        else
            (( count >= 100 )) || return 0
        fi
        if (( count == 0 || page >= 10000 )); then
            fail "Cloudflare 返回的分页数据不完整"
            return
        fi
        ((page+=1))
    done
}

find_record() {
    local name="$1" kind="$2" rows
    rows=$(list_records "$name" "$kind") || return 1
    jq -cs --arg name "$name" --arg kind "$kind" '
        map(select((.name | ascii_downcase | rtrimstr(".")) == $name
            and (.type == "A" or .type == "AAAA" or .type == "CNAME")
            and ($kind == "" or .type == $kind)))
        | if length > 1 then error("匹配多条记录，请指定唯一目标")
          elif length == 0 then empty else .[0] end' <<< "$rows"
}

make_payload() {
    jq -nc --arg type "$1" --arg name "$2" --arg content "$3" --argjson ttl "$4" \
        --argjson proxied "$5" '{$type, $name, $content, $ttl, $proxied}'
}

record_id() {
    jq -er '.id | select(type == "string" and test("^[a-zA-Z0-9_-]+$"))' <<< "$1"
}

ddns() {
    local ip record old id payload
    RECORD_NAME="$(normalize_name "$RECORD_NAME" wildcard)" || { fail "请在配置中填写有效的 DDNS 域名"; return; }
    [[ $RECORD_TYPE == A || $RECORD_TYPE == AAAA ]] || { fail "记录类型须为 A 或 AAAA"; return; }
    TTL="$(normalize_ttl "${TTL:-60}")" || { fail "TTL 无效"; return; }
    ip=$(get_ip "$RECORD_TYPE") || return 1
    record=$(find_record "$RECORD_NAME" "$RECORD_TYPE") || return 1
    if [[ ! $record ]]; then
        payload=$(make_payload "$RECORD_TYPE" "$RECORD_NAME" "$ip" "$TTL" false) || return 1
        api POST dns_records "$payload" >/dev/null || return 1
        log "已创建 $RECORD_NAME → $ip"
        return
    fi
    old=$(jq -er '.content' <<< "$record") || return 1
    if [[ $(normalize_ip "$RECORD_TYPE" "$old") == "$(normalize_ip "$RECORD_TYPE" "$ip")" ]]; then
        log "$RECORD_NAME 地址未变 ($ip)"
        return
    fi
    id=$(record_id "$record") || return 1
    payload=$(jq -c --arg content "$ip" --argjson ttl "$TTL" \
        'if .proxied == true then {$content} else {$content, $ttl} end' <<< "$record") || return 1
    api PATCH "dns_records/$id" "$payload" >/dev/null || return 1
    log "已更新 $RECORD_NAME → $ip"
}

sync_records() {
    local kind="$1" dry_run="$2" name source ip normalized rows row id previous value payload
    local status=0 changes=0 succeeded=0
    local -a plan=()
    name=$(normalize_name "$RECORD_NAME" wildcard) || { fail "请配置 DDNS 源记录"; return; }
    source=$(find_record "$name" "${kind:-$RECORD_TYPE}") || return 1
    [[ $source ]] || { fail "未找到 DDNS 源记录 $name"; return; }
    kind=$(jq -er '.type' <<< "$source") || return 1
    ip=$(jq -er '.content' <<< "$source") || return 1
    normalized=$(normalize_ip "$kind" "$ip") || { fail "源记录须为有效的 A 或 AAAA 地址"; return; }
    rows=$(list_records '' "$kind") || return 1
    rows=$(jq -c --arg name "$name" --arg kind "$kind" '
        select(.type == $kind and (.name | ascii_downcase | rtrimstr(".")) != $name and .proxied == false)' <<< "$rows") || return 1
    while IFS= read -r row; do
        [[ $row ]] || continue
        previous=$(jq -er '.content' <<< "$row") || return 1
        value=$(normalize_ip "$kind" "$previous") || { fail "列表包含无效的 $kind 地址"; return; }
        if [[ $value != "$normalized" ]]; then
            record_id "$row" >/dev/null || return 1
            plan+=("$row")
        fi
    done <<< "$rows"
    payload=$(jq -nc --arg content "$ip" '{$content}') || return 1
    for row in "${plan[@]}"; do
        name=$(jq -er '.name' <<< "$row") || return 1
        previous=$(jq -er '.content' <<< "$row") || return 1
        id=$(record_id "$row") || return 1
        printf '%s (%s)  %s → %s\n' "$name" "$kind" "$previous" "$ip"
        ((changes+=1))
        if [[ $dry_run != true ]]; then
            if api PATCH "dns_records/$id" "$payload" >/dev/null; then ((succeeded+=1)); else status=1; fi
        fi
    done
    if [[ $dry_run == true ]]; then
        printf '待变更记录数：%s\n' "$changes"
    else
        printf '已更新 %s/%s 条记录\n' "$succeeded" "$changes"
    fi
    return "$status"
}

validate_content() {
    if [[ $1 == CNAME ]]; then
        normalize_name "$2" >/dev/null
    else
        normalize_ip "$1" "$2" >/dev/null
    fi
}

manage_record() {
    local command="$1" prefix="$2" kind="$3" ip="$4" ttl="$5" proxy="$6"
    local response name record id payload old
    if [[ ! $BASE_DOMAIN ]]; then
        response=$(api GET) || { fail "请运行 configure 或在配置中填写 BASE_DOMAIN"; return; }
        BASE_DOMAIN=$(jq -er '.result.name' <<< "$response") || return 1
    fi
    BASE_DOMAIN=$(normalize_name "$BASE_DOMAIN") || { fail "配置的区域域名无效"; return; }
    if [[ $prefix == @ ]]; then
        name="$BASE_DOMAIN"
    else
        name=$(normalize_name "$prefix.$BASE_DOMAIN" wildcard) || { fail "记录前缀无效"; return; }
    fi
    record=$(find_record "$name" "$kind") || return 1
    if [[ $command == add ]]; then
        [[ ! $record ]] || { fail "记录已存在：$name ($kind)"; return; }
        if [[ ! $ip && $kind != CNAME ]]; then ip=$(get_ip "$kind") || return 1; fi
        validate_content "$kind" "$ip" || { fail "请提供有效的 $kind 记录内容"; return; }
        ttl=$(normalize_ttl "${ttl:-${TTL:-60}}") || { fail "TTL 无效"; return; }
        payload=$(make_payload "$kind" "$name" "$ip" "$ttl" "${proxy:-false}") || return 1
        api POST dns_records "$payload" >/dev/null || return 1
    else
        [[ $record ]] || { fail "未找到记录 $name"; return; }
        id=$(record_id "$record") || return 1
        if [[ $command == delete ]]; then
            api DELETE "dns_records/$id" >/dev/null || return 1
        else
            kind=$(jq -er '.type' <<< "$record") || return 1
            if [[ $ip ]]; then
                validate_content "$kind" "$ip" || { fail "请提供有效的 $kind 记录内容"; return; }
                old=$(jq -er '.content' <<< "$record") || return 1
                if [[ $kind != CNAME && $(normalize_ip "$kind" "$old") == "$(normalize_ip "$kind" "$ip")" ]]; then
                    ip=''
                fi
            fi
            if [[ $ttl ]]; then ttl=$(normalize_ttl "$ttl") || { fail "TTL 无效"; return; }; fi
            payload=$(jq -c --arg content "$ip" --arg ttl "$ttl" --arg proxied "$proxy" '
                . as $old
                | ({content: $content, ttl: (if $ttl == "" then null else ($ttl|tonumber) end),
                    proxied: (if $proxied == "" then null else ($proxied == "true") end)})
                | with_entries(select(.value != "" and .value != null and .value != $old[.key]))' <<< "$record") || return 1
            if [[ $payload == '{}' ]]; then
                printf '%s 无需修改\n' "$name"
                return
            fi
            api PATCH "dns_records/$id" "$payload" >/dev/null || return 1
        fi
    fi
    printf '%s 成功：%s\n' "$command" "$name"
}

cron_path() {
    local path="$1"
    [[ $path == /* && $path != *$'\n'* && $path != *$'\r'* && $path != *\\* ]] ||
        { fail "定时任务需要可安全引用的绝对路径"; return; }
    path="${path//\'/\'\\\'\'}"
    path="${path//%/\\%}"
    printf "'%s'" "$path"
}

schedule() {
    local value="${1:-status}" current next output status=0 line count=0 interval script bash_bin quoted_bash quoted_script user
    command -v crontab >/dev/null || { fail "未找到 crontab，请先安装并启用 Cron 服务"; return; }
    current=$(LC_ALL=C crontab -l 2>&1) || status=$?
    if (( status != 0 )); then
        user=$(id -un) || return 1
        if (( status == 1 )) && [[ $current == "no crontab for $user" ||
            $current == "crontab: can't open '$user': No such file or directory" ]]; then
            current=''
        else
            fail "读取当前用户的 crontab 失败：$current"
            return
        fi
    fi
    next=''
    while IFS= read -r line; do
        if [[ $line != '#'* && ( $line == *" ddns $CRON_MARKER" || $line == *" clean-logs $LOG_CRON_MARKER" ) ]]; then
            ((count+=1))
            if [[ $value == status ]]; then printf '%s\n' "$line"; fi
        elif [[ $line || $current ]]; then
            next+="$line"$'\n'
        fi
    done <<< "$current"
    if [[ $value == status ]]; then
        if (( count == 0 )); then printf '尚未配置 SH 版定时任务。\n'; fi
        return 0
    fi
    if [[ $value == off ]]; then
        if (( count == 0 )); then printf '尚未配置 SH 版定时任务。\n'; return 0; fi
    else
        [[ $value =~ ^[0-9]{1,2}$ ]] && (( 10#$value >= 1 && 10#$value <= 60 )) ||
            { fail "间隔须为 1–60 分钟，或使用 status/off"; return; }
        [[ -f $CONFIG_FILE || -f $PYTHON_FILE ]] ||
            { fail "请先完成 DDNS 配置，再设置定时任务"; return; }
        interval=$((10#$value))
        if (( interval == 60 )); then interval=0; else interval="*/$interval"; fi
        script=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && printf '%s/%s' "$PWD" "$(basename -- "${BASH_SOURCE[0]}")") || return 1
        bash_bin=$(type -P bash) || return 1
        quoted_bash=$(cron_path "$bash_bin") && quoted_script=$(cron_path "$script") || return 1
        next+="$interval * * * * $quoted_bash $quoted_script ddns $CRON_MARKER"$'\n'
        next+="0 3 * * * $quoted_bash $quoted_script clean-logs $LOG_CRON_MARKER"$'\n'
    fi
    if ! output=$(printf '%s' "$next" | LC_ALL=C crontab - 2>&1); then
        fail "写入当前用户的 crontab 失败：$output"
        return
    fi
    if [[ $value == off ]]; then
        printf '已取消 SH 版 DDNS 和日志清理任务。\n'
    else
        printf '已设置 SH 版定时 DDNS 和每天 03:00 清理日志。请确认 Cron 服务正在运行。\n'
    fi
}

clean_logs() {
    local rotated path stamp expired
    if [[ -e $CONFIG_FILE ]]; then
        load_assignments || return 1
    elif [[ -e $PYTHON_FILE ]]; then
        load_json_config "$PYTHON_FILE" || return 1
    else
        return 0
    fi
    [[ $LOG_FILE ]] || return 0
    [[ $LOG_FILE == /* ]] || LOG_FILE="./$LOG_FILE"
    stamp=$(date '+%Y-%m-%d') || return 1
    rotated="$LOG_FILE.cloudflare-ddns-sh.$stamp"
    if [[ -f $LOG_FILE && ! -L $LOG_FILE && ! -e $rotated && ! -L $rotated ]]; then
        mv -n -- "$LOG_FILE" "$rotated" && touch -- "$rotated" || return 1
    fi
    for path in "$LOG_FILE".cloudflare-ddns-sh.[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]; do
        [[ -f $path && ! -L $path ]] || continue
        expired=$(find "$path" -prune -type f -mtime +6 -print) || return 1
        if [[ $expired ]]; then
            rm -- "$path" || return 1
        fi
    done
}

schedule_menu() {
    local choice value
    while :; do
        printf '\n%s\n' '定时任务' '1. 添加或更新 DDNS 和日志清理任务' '2. 删除这两项定时任务' '3. 查看定时任务' '0. 返回'
        read -r -p '请选择: ' choice || return 0
        case "$choice" in
            0) return 0 ;;
            1)
                read -r -p 'DDNS 间隔（1–60 分钟，回车 5）: ' value || return 0
                schedule "${value:-5}"
                ;;
            2) schedule off ;;
            3) schedule status ;;
            *) printf '请选择菜单中的数字。\n' ;;
        esac
    done
}

menu_record() {
    local action="$1" prefix kind value ttl proxy confirmed
    local -a args=()
    read -r -p '记录前缀（根域名用 @）: ' prefix || return 1
    [[ $prefix ]] || { fail "请输入记录前缀"; return; }
    args+=("$prefix")
    if [[ $action == add ]]; then
        read -r -p '记录类型 A/AAAA/CNAME [A]: ' kind || return 1
    else
        read -r -p '记录类型 A/AAAA/CNAME（留空自动匹配）: ' kind || return 1
    fi
    [[ ! $kind ]] || args+=(--type "$kind")
    if [[ $action == delete ]]; then
        read -r -p "确认删除前缀 $prefix 的记录？[y/N]: " confirmed || return 1
        [[ $confirmed == [yY] ]] || return 0
    else
        if [[ $action == add ]]; then
            read -r -p 'IP 或 CNAME 目标（A/AAAA 留空自动探测）: ' value || return 1
            read -r -p 'TTL 或 auto（留空使用配置值）: ' ttl || return 1
            read -r -p '启用代理？[y/N]: ' proxy || return 1
            case "$proxy" in [yY]) args+=(--proxied) ;; ''|[nN]) ;; *) fail "代理选项无效"; return ;; esac
        else
            read -r -p '新 IP 或 CNAME 目标（留空保留）: ' value || return 1
            read -r -p '新 TTL 或 auto（留空保留）: ' ttl || return 1
            read -r -p '代理 开/关 [y/n，留空保留]: ' proxy || return 1
            case "$proxy" in [yY]) args+=(--proxied) ;; [nN]) args+=(--no-proxied) ;; '') ;; *) fail "代理选项无效"; return ;; esac
        fi
        [[ ! $value ]] || args+=(--ip "$value")
        [[ ! $ttl ]] || args+=(--ttl "$ttl")
    fi
    main "$action" "${args[@]}"
}

menu() {
    local choice value confirmed
    local -a args=()
    while :; do
        printf '\n%s\n' 'Cloudflare DDNS 管理'
        printf '%s\n' '1. 创建或修改配置' '2. 立即更新 DDNS' '3. 新增 DNS 记录' '4. 修改 DNS 记录' \
            '5. 删除 DNS 记录' '6. 批量同步' '7. 定时任务' '0. 退出'
        read -r -p '请选择: ' choice || return 0
        case "$choice" in
            0) return 0 ;;
            1) (main configure) ;;
            2) (main ddns) ;;
            3) (menu_record add) ;;
            4) (menu_record update) ;;
            5) (menu_record delete) ;;
            6)
                read -r -p '记录类型 [A/AAAA，留空跟随配置]: ' value || return 0
                args=()
                [[ ! $value ]] || args+=(--type "$value")
                if (main sync "${args[@]}" --dry-run); then
                    read -r -p '执行以上批量同步？[y/N]: ' confirmed || return 0
                    if [[ $confirmed == [yY] ]]; then (main sync "${args[@]}"); fi
                fi
                ;;
            7) schedule_menu ;;
            *) printf '请选择菜单中的数字。\n' ;;
        esac
    done
}

main() {
    local command="${1:-ddns}" prefix='' kind='' ip='' ttl='' proxy='' dry_run=false
    if (( $# )); then
        shift
    elif [[ -t 0 && -t 1 ]]; then
        command=menu
    fi
    case "$command" in
        -h|--help) usage; return 0 ;;
        -reconfig|-delete)
            [[ $# == 0 ]] || { fail "本地配置维护选项不接受额外参数"; return; }
            rm -f -- "$CONFIG_FILE" || return 1
            if [[ $command == -delete ]]; then rm -f -- "$DEFAULT_LOG" || return 1; fi
            printf '已清理 SH 版的本地配置\n'
            return 0
            ;;
        configure|ddns|add|update|delete|sync|menu|schedule|clean-logs) ;;
        *) fail "未知命令 $command。使用 --help 查看帮助"; return ;;
    esac
    if [[ ${1:-} == -h || ${1:-} == --help ]]; then usage; return 0; fi
    case "$command" in
        menu)
            [[ $# == 0 ]] || { fail "menu 不接受额外参数"; return; }
            menu
            return
            ;;
        schedule)
            [[ $# -le 1 ]] || { fail "schedule 只接受一个参数"; return; }
            schedule "$@"
            return
            ;;
        clean-logs)
            [[ $# == 0 ]] || { fail "clean-logs 不接受额外参数"; return; }
            clean_logs
            return
            ;;
        add|update|delete)
            [[ $# != 0 && $1 != -* ]] || { fail "请指定记录前缀，根域名用 @"; return; }
            prefix="$1"
            shift
            ;;
    esac
    while (( $# )); do
        case "$command:$1" in
            add:--type|update:--type|delete:--type|sync:--type)
                [[ $# -ge 2 && $2 && $2 != -* ]] || { fail "--type 缺少值"; return; }
                kind="$2"; shift ;;
            add:--ip|update:--ip)
                [[ $# -ge 2 && $2 && $2 != -* ]] || { fail "--ip 缺少值"; return; }
                ip="$2"; shift ;;
            add:--ttl|update:--ttl)
                [[ $# -ge 2 && $2 && $2 != -* ]] || { fail "--ttl 缺少值"; return; }
                ttl="$2"; shift ;;
            add:--proxied|update:--proxied)
                [[ $proxy != false ]] || { fail "代理选项冲突"; return; }
                proxy=true ;;
            update:--no-proxied)
                [[ $proxy != true ]] || { fail "代理选项冲突"; return; }
                proxy=false ;;
            sync:--dry-run) dry_run=true ;;
            *) fail "无效参数 $1。使用 --help 查看帮助"; return ;;
        esac
        shift
    done
    case "$kind" in
        ''|A|AAAA) ;;
        CNAME) [[ $command != sync ]] || { fail "sync 只支持 A 和 AAAA"; return; } ;;
        *) fail "不支持的记录类型 $kind"; return ;;
    esac
    if [[ $command == update && ! $ip && ! $ttl && ! $proxy ]]; then
        fail "请指定要修改的 IP、TTL 或代理状态"
        return
    fi
    command -v jq >/dev/null || { fail "缺少 jq，请先安装 jq"; return; }
    if [[ $command == configure ]]; then configure; return; fi
    command -v curl >/dev/null || { fail "缺少 curl，请先安装 curl"; return; }
    load_config "$command" || return 1
    case "$command" in
        ddns) ddns ;;
        sync) sync_records "$kind" "$dry_run" ;;
        add) manage_record "$command" "$prefix" "${kind:-A}" "$ip" "$ttl" "$proxy" ;;
        *) manage_record "$command" "$prefix" "$kind" "$ip" "$ttl" "$proxy" ;;
    esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
