#!/bin/bash

# Cloudflare DDNS 更新脚本 (统一配置版)
# 新增: MTR 动态路由网关探测、旧配置删除、依赖自动安装
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
    
    # 写入日志文件 (如果目录存在)
    if [ -d "$(dirname "$LOG_FILE")" ]; then
        echo "$log_entry" >> "$LOG_FILE"
    fi
    
    # 输出到控制台（除非指定仅记录）
    if [[ $log_only -eq 0 ]]; then
        echo "$log_entry"
    fi
}

# 环境依赖检查与自动安装
check_dependencies() {
    local pkg_manager=""
    if command -v apt-get &> /dev/null; then
        pkg_manager="apt-get"
    elif command -v yum &> /dev/null; then
        pkg_manager="yum"
    fi

    # 检查 jq
    if ! command -v jq &> /dev/null; then
        echo "⏳ 未检测到 jq 工具，正在尝试自动安装..."
        if [ "$pkg_manager" = "apt-get" ]; then
            sudo apt-get update && sudo apt-get install jq -y > /dev/null 2>&1
            echo "✅ jq 安装完成！"
        elif [ "$pkg_manager" = "yum" ]; then
            sudo yum install jq -y > /dev/null 2>&1
            echo "✅ jq 安装完成！"
        else
            echo "❌ 错误：需要jq工具但自动安装失败，请手动安装 (例如: apt install jq)。"
            exit 1
        fi
    fi

    # 检查 mtr
    if ! command -v mtr &> /dev/null; then
        echo "⏳ 未检测到 mtr 工具，正在尝试自动安装..."
        if [ "$pkg_manager" = "apt-get" ]; then
            sudo apt-get update && sudo apt-get install mtr -y > /dev/null 2>&1
            echo "✅ mtr 安装完成！"
        elif [ "$pkg_manager" = "yum" ]; then
            sudo yum install mtr -y > /dev/null 2>&1
            echo "✅ mtr 安装完成！"
        else
            echo "⚠️ 警告：无法自动安装 mtr，MTR 探测功能可能无法使用，建议手动安装。"
        fi
    fi
}

# 创建配置目录
create_config_dir() {
    if [ ! -d "$CFG_DIR" ]; then
        mkdir -p "$CFG_DIR"
        chmod 700 "$CFG_DIR"
    fi
}

# 删除配置文件
delete_config() {
    local config_dir=$(dirname "$CONFIG_FILE")
    local deleted_files=()
    
    # 删除配置文件
    if [ -f "$CONFIG_FILE" ]; then
        rm -f "$CONFIG_FILE"
        deleted_files+=("配置文件: $CONFIG_FILE")
    fi
    
    # 删除日志文件（如果存在）
    if [ -f "$LOG_FILE" ]; then
        rm -f "$LOG_FILE"
        deleted_files+=("日志文件: $LOG_FILE")
    fi
    
    # 尝试删除配置目录（如果为空）
    if [ -d "$config_dir" ]; then
        if rmdir "$config_dir" 2>/dev/null; then
            deleted_files+=("配置目录: $config_dir")
        fi
    fi
    
    if [ ${#deleted_files[@]} -gt 0 ]; then
        echo "✅ 已删除以下文件:"
        for file in "${deleted_files[@]}"; do
            echo "  - $file"
        done
    else
        echo "⚠️ 未找到配置文件或日志文件"
    fi
}

# 读取或创建配置
init_config() {
    create_config_dir

    usage() {
        echo
        echo "Cloudflare DDNS 更新脚本"
        echo
        echo "option:"
        echo "  -h, --help            显示此帮助信息"
        echo "  -reconfig             重置配置文件并重新配置"
        echo "  -delete               删除所有配置和日志文件"
    }
    
    # 处理命令行选项
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -delete)
            delete_config
            exit 0
            ;;
        -reconfig)
            if [ -f "$CONFIG_FILE" ]; then
                rm -f "$CONFIG_FILE"
                echo "✅ 配置已重置"
                echo "请重新运行脚本进行配置"
                exit 0
            else
                echo "配置文件不存在，无需重置"
                exit 0
            fi
            ;;
    esac
    
    if [ -f "$CONFIG_FILE" ]; then
        # 兼容旧版本：默认MTR_TARGET为空
        MTR_TARGET=""
        
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

    read -p "7. [高级选项] MTR探测目标地址 (留空则获取本机常规外网出口IP，输入如 xxx.com 则获取网关IP): " MTR_TARGET
    
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
    echo "MTR_TARGET='$MTR_TARGET'" >> "$CONFIG_FILE"
    
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
    local ip=""
    
    if [ "$RECORD_TYPE" = "A" ]; then
        # ========= 新增 MTR 探测逻辑 =========
        if [ -n "$MTR_TARGET" ]; then
            log "🔍 正在通过 MTR 探测 [$MTR_TARGET] 的入口/网关 IP..." 1
            local mtr_ip
            # 抓取第三行（即第一跳），过滤出 IP
            mtr_ip=$(mtr -rw -c 1 "$MTR_TARGET" 2>/dev/null | awk 'NR==3 {print $2}')
            
            # 简单验证IPv4格式
            if [[ "$mtr_ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
                log "🎯 成功定位到网关/入口 IP: $mtr_ip" 1
                echo "$mtr_ip"
                return 0
            else
                log "⚠️ MTR 未抓到有效 IP (当前: $mtr_ip)，降级使用常规接口..." 1
            fi
        else
            log "🔍 未配置 MTR 探测目标，默认获取本机外网出口 IP..." 1
        fi
        # =====================================

        ip_services=(
            "http://txt.go.sohu.com/ip/sohu"
            "https://api.ipify.org"
            "https://ipv4.icanhazip.com"
        )
    else
        ip_services=(
            "https://api6.ipify.org"
            "https://ipv6.icanhazip.com"
            "https://v6.ident.me"
        )
    fi
    
    for service in "${ip_services[@]}"; do
        for ((i=1; i<=max_retry; i++)); do
            # 针对国内的搜狐接口需要特殊正则提取
            if [[ "$service" == *"sohu"* ]]; then
                ip=$(curl -s --max-time 10 "$service" 2>/dev/null | grep -oE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' | head -n 1)
            else
                ip=$(curl -${RECORD_TYPE/#A/4} -s --fail --max-time 10 "$service" 2>/dev/null)
            fi

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
    init_config "$@"
    
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
    log "当前探测到的公网IP: $CURRENT_IP"
    
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
        
        # 创建新的DNS记录 (默认关闭代理 proxied:false)
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
        log "🔄 IP地址未变化 ($EXISTING_IP)，无需更新"
        log "===== DDNS 更新完成 ====="
        return 0
    fi
    
    # 检测到IP变化
    log "🔄 检测到IP变化: $EXISTING_IP → $CURRENT_IP，更新中..."
    
    # 更新Cloudflare DNS记录
    UPDATE_DATA="{\"type\":\"$RECORD_TYPE\",\"name\":\"$RECORD_NAME\",\"content\":\"$CURRENT_IP\",\"ttl\":$TTL,\"proxied\":false}"
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

# 主程序入口
check_dependencies
main "$@"
