# Cloudflare DDNS 与 DNS 管理

任选 **SH** 或 **Python**，下载一个文件就能打开菜单。SH 版需要 Bash、curl 和 jq，适合 Linux、OpenWrt 和 NAS。Python 版只需要 Python 3，也可在 Windows 和 macOS 上运行。

## 下载并打开菜单

**SH 版**

```bash
curl -fLO https://raw.githubusercontent.com/chenzai666/cloudflare-ddns/main/cloudflare_ddns.sh
bash cloudflare_ddns.sh
```

**Python 版**

```bash
curl -fLO https://raw.githubusercontent.com/chenzai666/cloudflare-ddns/main/cloudflare_ddns.py
python3 cloudflare_ddns.py
```

Windows 可下载 Python 文件后运行 `py -3 cloudflare_ddns.py`。在终端里无参数运行会打开菜单，也可以显式加 `menu`。首次选择 **1**，输入 Cloudflare API Token、Zone ID 和要更新的完整域名。默认使用 A 记录。需要按前缀管理记录时，可以一并填写区域域名，例如 `example.com`。

## 菜单

| 选项 | 用途 |
| --- | --- |
| 1 配置或修改 | 保存 DDNS 设置。已有值可按回车保留，Token 输入时隐藏。 |
| 2 立即更新 DDNS | 探测当前公网 IP，更新指定的 Cloudflare A 或 AAAA 主记录。每次都查询远端，手动改过的 IP 会在下一轮纠正。 |
| 3、4、5 新增、修改、删除 | 管理单条 A、AAAA 或 CNAME 记录，支持根域名 `@`、TTL 和代理状态。 |
| 6 批量同步 | 读取 Cloudflare 上的 DDNS 源记录，预览区域内同类型、未开启代理的其他地址记录，确认后才同步。 |
| 7 定时任务 | 添加、查看或删除同一组的两项任务。 |
| 0 退出 | 关闭菜单。 |

### 定时任务

在菜单里选 **7 → 1** 添加两项任务。DDNS 更新默认每 5 分钟执行一次，间隔可修改。日志每天 **03:00** 轮转，并清理本版本生成的 7 天前归档。选 **7 → 2** 可一起移除这两项任务，选 **7 → 3** 查看。

Linux、OpenWrt 和 macOS 使用当前用户的 `crontab`，需有运行中的 cron 服务。Windows Python 版使用当前用户的任务计划程序，两项任务在该用户登录期间运行。请先把脚本放到长期使用的目录，再选一个版本添加任务。菜单仅替换或删除自己创建的任务，之前手写的 cron 仍由原位置管理。

### 旧配置和命令

升级前备份原脚本和配置，同路径替换对应版本即可。SH 沿用 `~/.cloudflare_ddns/config`，Python 沿用 `~/.cloudflare_ddns/config.json`。两版的管理功能都会优先读取已有的 `~/.cf_dns_manager/config.json`，以保留原管理器使用的区域。没有旧管理器配置时，管理命令复用 DDNS 配置。区域域名可在菜单 **1** 中填写。留空时查询区域详情需要 Zone Read 权限。配置文件应限制为当前用户读取，API Token 只授予目标区域所需权限。

原来不带参数的非交互定时任务继续执行单条 DDNS 更新。也可以明确写出子命令，例如 SH 版：

```bash
bash cloudflare_ddns.sh ddns
bash cloudflare_ddns.sh sync --type A --dry-run
bash cloudflare_ddns.sh schedule 5
bash cloudflare_ddns.sh schedule off
```

Python 版使用相同子命令，例如 `python3 cloudflare_ddns.py ddns`。两版都可以用 `--help` 查看命令。旧 `-reconfig`、`-delete` 维护的是本地配置，`delete <前缀>` 删除的是远端记录。MTR 探测属于旧配置的可选项，使用时先安装 `mtr`。独立的 `ip_monitor.py` 仍可用于公网 IP 监控。
