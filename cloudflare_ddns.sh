#!/bin/bash

# Cloudflare DDNS 更新脚本 (统一配置版)
# 配置文件与日志存放在同一目录，交互输入支持默认值

# 配置目录路径（所有配置和日志存储在此）
CFG_DIR="$HOME/.cloudflare_ddns"

# 配置文件路径
CONFIG_FILE="$CFG_DIR/config"

# 日志函数
log() {
    local msg="$1"
    local log_only=${2:-0}  # 可选参数：1=仅记录到文件
    
    # 格式化日志信息
    local log_entry="$(date +'%Y-%m-%d %H:%M:%S') - $msg"
    
    # 写入日志文件
    echo "$log_entry" >> "$LOG_FILE"
    
    # 输出到控制台（除非指定仅记录）
    if [[ $log_only -eq 0 ]]; then
        echo "$log_entry"
    fi
}

# 创建配置目录
create_config_dir() {
    if [ ! -d "$CFG_DIR" ]; then
        mkdir -p "$CFG_DIR"
        chmod 700 "$CFG_DIR"
    fi
}

# 读取或创建配置
init_config() {
    create_config_dir
    
    if [ -f "$CONFIG_FILE" ]; then
        # 加载现有配置
        source "$CONFIG_FILE"
        log "已加载配置文件: $CONFIG_FILE" 1
        return 0
    fi
    
    # 默认日志文件路径
    LOG_FILE="${CFG_DIR}/cloudflare_ddns.log"
    
    # 交互式创建新配置
    clear
    echo "╔══════════════════════════════════════════════════╗"
    echo "║           Cloudflare DDNS 配置向导               ║"
    echo "║  所有配置将存储在: $CFG_DIR  ║"
    echo "╚══════════════════════════════════════════════════╝"
    echo ""
    echo "提示：括号内为默认值，直接按回车使用默认设置"
    echo "──────────────────────────────────────────────────"
    
    read -p "1. 请输入Cloudflare API Token: " API_TOKEN
    [ -z "$API_TOKEN" ] && { echo "错误：API Token不能为空！"; exit 1; }
    
    read -p "2. 请输入Zone ID: " ZONE_ID
    [ -z "$ZONE_ID" ] && { echo "错误：Zone ID不能为空！"; exit 1; }
    
    read -p "3. 请输入要更新的域名 (例如：ddns.example.com): " RECORD_NAME
    RECORD_NAME=${RECORD_NAME:-ddns.example.com}
    
    read -p "4. 记录类型 [A/AAAA] (默认: A，可直接按回车): " RECORD_TYPE
    RECORD_TYPE=${RECORD_TYPE:-A}
    
    read -p "5. TTL值 [1-86400] (默认: 60，可直接按回车): " TTL
    TTL=${TTL:-60}
    
    read -p "6. 日志文件路径 (默认: ${CFG_DIR}/cloudflare_ddns.log，可直接按回车): " input_log
    LOG_FILE=${input_log:-"${CFG_DIR}/cloudflare_ddns.log"}
    
    # 初始化日志文件
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "===== DDNS 配置创建于 $(date) =====" > "$LOG_FILE"
    
    # 保存配置到文件
    echo "#!/bin/bash" > "$CONFIG_FILE"
    echo "# Cloudflare DDNS 配置文件" >> "$CONFIG_FILE"
    echo "API_TOKEN='$API_TOKEN'" >> "$CONFIG_FILE"
    echo "ZONE_ID='$ZONE_ID'" >> "$CONFIG_FILE"
    echo "RECORD_NAME='$RECORD_NAME'" >> "$CONFIG_FILE"
    echo "RECORD_TYPE='$RECORD_TYPE'" >> "$CONFIG_FILE"
    echo "TTL='$TTL'" >> "$CONFIG_FILE"
    echo "LOG_FILE='$LOG_FILE'" >> "$CONFIG_FILE"
    
    # 设置配置文件权限
    chmod 600 "$CONFIG_FILE"
    
    echo "──────────────────────────────────────────────────"
    echo "✅ 配置已保存到: $CONFIG_FILE"
    echo "📝 日志将记录到: $LOG_FILE"
    echo "下次运行脚本将自动使用这些配置"
    echo "══════════════════════════════════════════════════"
}

# 获取当前公网IP
get_ip() {
    local ip_services
    local max_retry=3
    
    if [ "$RECORD_TYPE" = "A" ]; then
        ip_services=(
            "https://api.ipify.org"
            "https://ipv4.icanhazip.com"
            "https://checkip.amazonaws.com"
        )
    else
        ip_services=(
            "https://api64.ipify.org"
            "https://ipv6.icanhazip.com"
            "https://v6.ident.me"
        )
    fi
    
    for service in "${ip_services[@]}"; do
        for ((i=1; i<=max_retry; i++)); do
            ip=$(curl -${RECORD_TYPE/#A/4} -s --fail --max-time 10 "$service" 2>/dev/null)
            if [ -n "$ip" ]; then
                echo "$ip"
                return 0
            fi
            sleep 1
        done
    done
    
    return 1
}

# 发送Cloudflare API请求
cf_api_request() {
    local method="$1"
    local endpoint="$2"
    local data="${3:-}"
    local url="https://api.cloudflare.com/client/v4/zones/$ZONE_ID/$endpoint"
    
    local curl_cmd="curl -s -X $method '$url' \
        -H 'Authorization: Bearer $API_TOKEN' \
        -H 'Content-Type: application/json'"
    
    [ -n "$data" ] && curl_cmd+=" --data '$data'"
    
    # 执行请求并返回响应
    eval "$curl_cmd"
}

# 主函数
main() {
    # 初始化配置
    init_config
    
    # 记录操作开始
    log "===== DDNS 更新开始 ($RECORD_NAME) ====="
    
    # 获取当前公网IP
    log "正在获取公网IP地址..." 1
    CURRENT_IP=$(get_ip)
    if [ -z "$CURRENT_IP" ]; then
        log "❌ 错误：无法获取公网IP地址，请检查网络连接"
        log "===== DDNS 更新失败 ====="
        return 1
    fi
    log "当前公网IP: $CURRENT_IP"
    
    # 获取Cloudflare DNS记录信息
    log "查询Cloudflare DNS记录..."
    RECORD_INFO=$(cf_api_request "GET" "dns_records?name=$RECORD_NAME&type=$RECORD_TYPE")
    
    # 检查API响应
    if ! jq -e '.success' <<< "$RECORD_INFO" >/dev/null; then
        ERROR_MSG=$(jq -r '.errors[0].message' <<< "$RECORD_INFO" 2>/dev/null || echo "未知错误")
        log "❌ Cloudflare API错误: $ERROR_MSG"
        log "===== DDNS 更新失败 ====="
        return 1
    fi
    
    RECORD_COUNT=$(jq -r '.result | length' <<< "$RECORD_INFO")
    
    # 检查记录是否存在
    if [ "$RECORD_COUNT" -eq 0 ] || [ "$RECORD_COUNT" = "null" ]; then
        log "⚠️ 未找到DNS记录 '$RECORD_NAME'，正在创建..."
        
        # 创建新的DNS记录
        CREATE_DATA="{\"type\":\"$RECORD_TYPE\",\"name\":\"$RECORD_NAME\",\"content\":\"$CURRENT_IP\",\"ttl\":$TTL,\"proxied\":false}"
        CREATE_RESULT=$(cf_api_request "POST" "dns_records" "$CREATE_DATA")
        
        # 检查创建结果
        if jq -e '.success' <<< "$CREATE_RESULT" >/dev/null; then
            NEW_RECORD_ID=$(jq -r '.result.id' <<< "$CREATE_RESULT")
            log "✅ 创建成功: $RECORD_NAME ($CURRENT_IP) 记录ID: $NEW_RECORD_ID"
            log "===== DDNS 更新完成 ====="
            return 0
        else
            ERROR_MSG=$(jq -r '.errors[0].message' <<< "$CREATE_RESULT" 2>/dev/null || echo "未知错误")
            log "❌ 创建失败: $ERROR_MSG"
            log "===== DDNS 更新失败 ====="
            return 1
        fi
        
    elif [ "$RECORD_COUNT" -gt 1 ]; then
        log "⚠️ 找到 $RECORD_COUNT 条匹配记录，将使用第一条记录"
    fi
    
    RECORD_ID=$(jq -r '.result[0].id' <<< "$RECORD_INFO")
    EXISTING_IP=$(jq -r '.result[0].content' <<< "$RECORD_INFO")
    
    log "Cloudflare当前记录IP: $EXISTING_IP"
    
    # 检查IP是否变化
    if [ "$CURRENT_IP" = "$EXISTING_IP" ]; then
        log "🔄 IP地址未变化，无需更新"
        log "===== DDNS 更新完成 ====="
        return 0
    fi
    
    # 检测到IP变化
    log "🔄 检测到IP变化: $EXISTING_IP → $CURRENT_IP，更新中..."
    
    # 更新Cloudflare DNS记录
    UPDATE_DATA="{\"type\":\"$RECORD_TYPE\",\"name\":\"$RECORD_NAME\",\"content\":\"$CURRENT_IP\",\"ttl\":$TTL}"
    UPDATE_RESULT=$(cf_api_request "PUT" "dns_records/$RECORD_ID" "$UPDATE_DATA")
    
    # 检查更新结果
    if jq -e '.success' <<< "$UPDATE_RESULT" >/dev/null; then
        log "✅ 更新成功: $RECORD_NAME 已设置为 $CURRENT_IP"
        log "===== DDNS 更新完成 ====="
        return 0
    else
        ERROR_MSG=$(jq -r '.errors[0].message' <<< "$UPDATE_RESULT" 2>/dev/null || echo "未知错误")
        log "❌ 更新失败: $ERROR_MSG"
        log "===== DDNS 更新失败 ====="
        return 1
    fi
}

# 检查jq依赖
if ! command -v jq &> /dev/null; then
    echo "❌ 错误：本脚本需要 jq 工具来处理JSON数据"
    echo "请安装 jq:"
    echo "  Ubuntu/Debian: sudo apt install jq"
    echo "  CentOS/RHEL: sudo yum install jq"
    echo "  macOS: brew install jq"
    exit 1
fi

# 执行主函数
main
