# cloudflare-ddns使用方法
先准备以下信息

╔══════════════════════════════════════════════════╗

║           Cloudflare DDNS 配置向导               ║

║  所有配置将存储在: /root/.cloudflare_ddns         ║

╚══════════════════════════════════════════════════╝

提示：括号内为默认值，直接按回车使用默认设置

──────────────────────────────────────────────────

1. 请输入Cloudflare API Token: 你的API Token
2. 请输入Zone ID: 你的域名区域ID
3. 请输入要更新的域名 (例如：ddns.example.com): 你的域名
4. 记录类型 [A/AAAA] (默认: A，可直接按回车): 
5. TTL值 [1-86400] (默认: 60，可直接按回车): 
6. 日志文件路径 (默认: /root/.cloudflare_ddns/cloudflare_ddns.log，可直接按回车):

──────────────────────────────────────────────────

✅ 配置已保存到: /root/.cloudflare_ddns/config

📝 日志将记录到: /root/.cloudflare_ddns/cloudflare_ddns.log
下次运行脚本将自动使用这些配置
══════════════════════════════════════════════════


### 1. 安装必要依赖

确保已安装 `jq` 工具：
### Debian/Ubuntu
`sudo apt update && sudo apt install -y jq curl`

### CentOS/RHEL
`sudo yum install -y epel-release`
`sudo yum install -y jq curl`


### 2. 获取 DDNS 脚本并执行
`curl -# -o /usr/local/bin/cloudflare_ddns.sh https://raw.githubusercontent.com/chenzai666/cloudflare-ddns/refs/heads/main/cloudflare_ddns.sh && chmod +x /usr/local/bin/cloudflare_ddns.sh && /usr/local/bin/cloudflare_ddns.sh`

保存脚本路径为 `/usr/local/bin/cloudflare_ddns.sh`

###也可使用python版本的脚本
`curl -# -o /usr/local/bin/cloudflare_ddns.py https://raw.githubusercontent.com/chenzai666/cloudflare-ddns/refs/heads/main/cloudflare_ddns.py && chmod +x /usr/local/bin/cloudflare_ddns.py && /usr/local/bin/cloudflare_ddns.py`

保存脚本路径为 `/usr/local/bin/cloudflare_ddns.py`


### 3. 设置 crontab 定时任务

编辑当前用户的 crontab：
`crontab -e`


添加以下内容（每5分钟运行一次并记录日志）（shell版）：
### 每5分钟运行一次DDNS脚本并记录日志
`*/5 * * * * /usr/local/bin/cloudflare_ddns.sh >> /root/.cloudflare_ddns/cloudflare_ddns.log 2>&1`

### 可选：每天凌晨清理日志（保留7天日志）
`0 0 * * * find /root/.cloudflare_ddns/cloudflare_ddns.log -mtime +7 -delete`

添加以下内容（每5分钟运行一次并记录日志）（python版）：
### 每5分钟运行一次DDNS脚本并记录日志
`*/5 * * * * /usr/local/bin/cloudflare_ddns.py >> /root/.cloudflare_ddns/cloudflare_ddns.log 2>&1`

### 可选：每天凌晨清理日志（保留7天日志）
`0 0 * * * find /root/.cloudflare_ddns/cloudflare_ddns.log -mtime +7 -delete`


### 4. 监控日志文件

你可以使用以下命令监控日志：

# 实时查看日志
`tail -f /root/.cloudflare_ddns/cloudflare_ddns.log`

# 查看最后20条日志
`tail -n 20 /root/.cloudflare_ddns/cloudflare_ddns.log`

# 搜索错误
`grep -i "error\|fail\|not found\|无法" /root/.cloudflare_ddns/cloudflare_ddns.log`


### 5. 添加邮件通知（可选）

如果你想在更新失败时收到邮件通知，可以修改 crontab：


# 每5分钟运行一次，失败时发送邮件
`*/5 * * * * /usr/local/bin/cloudflare_ddns.sh >> /root/.cloudflare_ddns/cloudflare_ddns.log 2>&1 || mail -s "Cloudflare DDNS Update Failed" your@email.com < /root/.cloudflare_ddns/cloudflare_ddns.log`


确保系统已配置邮件服务（如 `sendmail` 或 `postfix`）。

### 高级调试选项

如果你想在脚本中添加调试输出，可以在脚本开头添加：


# 启用详细调试
`set -x  # 启用命令跟踪`
# 或者
`export DEBUG=true`

# 在脚本中
`if [ "$DEBUG" = "true" ]; then
    # 输出调试信息
fi`


然后在 crontab 中设置环境变量：

`*/5 * * * * DEBUG=true /usr/local/bin/cloudflare_ddns.sh >> /root/.cloudflare_ddns/cloudflare_ddns.log 2>&1`


### 注意事项

1. **权限问题**：确保脚本和日志文件有正确的读写权限
2. **API Token权限**：确认API Token有足够的权限（Zone DNS Edit）
3. **日志轮转**：对于长期运行，考虑设置日志轮转
4. **IP获取服务**：如果某些IP服务被屏蔽，可以编辑 `get_ip()` 函数尝试其他服务

这种设置会在系统后台定期运行DDNS更新脚本，同时在日志文件中记录所有操作和错误，便于监控更新状态和排查问题。
