# Cloudflare DDNS 与 DNS 记录管理

使用同一个 Python 入口更新动态公网 IP、管理单条 DNS 记录，以及将其他地址记录同步到指定的 DDNS 记录。已有 `cloudflare_ddns.py` 无参数定时任务继续执行单条 DDNS 更新。原管理器的 `cf_dns_manager.py add|update|delete|sync` 命令也可通过本仓库的兼容入口调用。

## 安装

在 Linux 或 macOS 上克隆整个仓库，使入口脚本和记录管理模块 `cf_records.py` 位于同一目录。Python 版依赖 Python 3 和 `requests`。

```bash
git clone https://github.com/chenzai666/cloudflare-ddns.git
cd cloudflare-ddns
python3 -m venv .venv
.venv/bin/python -m pip install requests
.venv/bin/python cloudflare_ddns.py --help
```

Windows 可用 `py -3 -m venv .venv` 建立环境，再用 `.venv\Scripts\python.exe` 运行上述 Python 命令。

## 首次配置和更新

如需先配置、稍后再更新 DNS，可先执行 `python cloudflare_ddns.py configure`。配置 MTR 探测目标时，请先安装系统的 `mtr` 命令。

准备目标区域的 Zone ID、具备该区域 DNS 编辑权限的 API Token，以及要更新的完整域名。首次执行下列命令时会启动配置向导，随后检查并更新配置指定的 A 或 AAAA 记录。

```bash
.venv/bin/python cloudflare_ddns.py ddns
```

Python DDNS 配置保存在 `~/.cloudflare_ddns/config.json`。前缀操作使用 `BASE_DOMAIN` 拼接完整域名，例如将 `home` 拼为 `home.example.com`。已有 DDNS 配置要启用这些操作时，请在原 JSON 中补充 `"BASE_DOMAIN": "example.com"`，保留原凭据及其他字段。请仅允许运行脚本的用户读取配置。

| 操作 | 读取的配置 |
| --- | --- |
| 无参数或 `ddns` | 使用 `~/.cloudflare_ddns/config.json`，将探测到的地址写入 `RECORD_NAME`。 |
| `add`、`update`、`delete`、`sync` | 如果已有 `~/.cf_dns_manager/config.json`，沿用其中的账号、区域、`BASE_DOMAIN`、`DDNS_RECORD` 和 `DEFAULT_TTL`。否则复用 DDNS 配置的账号、区域、`RECORD_NAME` 和 `TTL`。 |

原管理器与 DDNS 使用不同区域时，两个配置文件可同时保留。升级前请备份配置和原脚本，先用测试子域名验证。仅更新单文件的旧 DDNS 安装仍可执行原无参数任务；使用新增管理命令时请安装完整仓库。

## 命令示例

| 用途 | 命令（使用虚拟环境中的 Python） |
| --- | --- |
| 更新 DDNS 记录 | `python cloudflare_ddns.py ddns` |
| 新增子域名 A 记录 | `python cloudflare_ddns.py add home --ip 192.0.2.10 --ttl auto` |
| 新增 CNAME | `python cloudflare_ddns.py add www --type CNAME --ip home.example.com` |
| 更新指定 AAAA 记录 | `python cloudflare_ddns.py update home --type AAAA --ip 2001:db8::10` |
| 调整根域名 A 记录 TTL | `python cloudflare_ddns.py update @ --type A --ttl 300` |
| 删除指定记录 | `python cloudflare_ddns.py delete home --type A` |
| 查看子命令参数 | `python cloudflare_ddns.py update --help` |

A、AAAA 记录在新增时可省略 `--ip` 以探测相应地址。新增或更新记录可用 `--proxied` 启用代理，更新时可用 `--no-proxied` 关闭代理。修改或删除前请核对当前区域、记录名称和类型。旧选项 `-reconfig` 用于重置本地 DDNS 配置，`-delete` 用于清理本地 DDNS 配置和日志，`delete <前缀>` 操作的是 Cloudflare 远端记录。

### 批量同步

`sync` 读取 Cloudflare 上的 DDNS 源记录，将**整个目标区域内其他同类型且未启用代理的地址记录**对齐到该地址。若只需要更新一个动态域名，使用上面的 `ddns`。批量同步前先预览目标，确认后再执行第二条命令。

```bash
.venv/bin/python cloudflare_ddns.py sync --type A --dry-run
.venv/bin/python cloudflare_ddns.py sync --type A
```

原管理器命令也可继续使用，例如 `.venv/bin/python cf_dns_manager.py sync --type AAAA --dry-run`。

## 定时运行

在已完成配置的用户下执行 `crontab -e`，按实际安装路径设置定时任务。以下示例每五分钟核对一次动态域名。原有无参数 cron 命令也可继续使用。

```cron
*/5 * * * * /opt/cloudflare-ddns/.venv/bin/python /opt/cloudflare-ddns/cloudflare_ddns.py ddns
```

默认 DDNS 日志位于 `~/.cloudflare_ddns/cloudflare_ddns.log`，可用 `tail -f ~/.cloudflare_ddns/cloudflare_ddns.log` 查看。

## 原有工具

- `cloudflare_ddns.sh` 提供 Bash 版 DDNS，沿用 `~/.cloudflare_ddns/config`。使用 `bash cloudflare_ddns.sh -h` 查看原有选项。
- `ip_monitor.py` 提供独立的公网 IP 监控，使用 `python ip_monitor.py --help` 查看监控参数。
