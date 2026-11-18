#!/usr/bin/env python3
"""Cloudflare DDNS 与 DNS 记录管理。"""

import os
import sys
import json
import logging
import argparse
import subprocess
import shlex
import tempfile
import time
from datetime import datetime
import ipaddress
from getpass import getpass
from http.client import HTTPException
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import re
from pathlib import Path

# 全局配置目录
CFG_DIR = Path.home() / ".cloudflare_ddns"
CFG_FILE = CFG_DIR / "config.json"
LOG_FILE = CFG_DIR / "cloudflare_ddns.log"

MANAGER_FILE = Path.home() / ".cf_dns_manager" / "config.json"

# 配置模板
DEFAULT_CONFIG = {
    "API_TOKEN": "",
    "ZONE_ID": "",
    "RECORD_NAME": "ddns.example.com",
    "RECORD_TYPE": "A",
    "TTL": 60,
    "LOG_FILE": str(LOG_FILE),
    "MTR_TARGET": ""  # 新增配置项：MTR探测目标地址
}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CloudflareDDNS:
    def __init__(self, config=None):
        # 确保配置目录存在
        if config is None:
            CFG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            config = self.load_config()
        self.config = config
        self.setup_logging()
        
    def load_config(self):
        """加载或创建配置"""
        if CFG_FILE.exists():
            try:
                with open(CFG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    # 验证必要配置
                    if not config.get("API_TOKEN") or not config.get("ZONE_ID"):
                        raise ValueError("缺少必要配置")
                    
                    # 兼容旧版本配置文件，如果没有 MTR_TARGET 则设置为空
                    if "MTR_TARGET" not in config:
                        config["MTR_TARGET"] = ""
                    return config
            except Exception as e:
                raise ValueError(f"请检查配置文件 {CFG_FILE}：{e}") from e
        
        # 首次运行，交互式配置
        if not sys.stdin.isatty():
            raise ValueError("请先在终端中选择配置，或执行 configure")
        return self.setup_wizard()
    
    def setup_wizard(self, existing=None):
        """保存配置，不执行同步。已有值可直接回车保留。"""
        config = dict(DEFAULT_CONFIG, **(existing or {}))
        print("Cloudflare DDNS 配置")
        token = getpass("API Token（已有令牌可回车保留）：").strip()
        config["API_TOKEN"] = token or config["API_TOKEN"]
        config["ZONE_ID"] = input(f"Zone ID [{config['ZONE_ID']}]：").strip() or config["ZONE_ID"]
        if not config["API_TOKEN"] or not config["ZONE_ID"]:
            raise ValueError("API Token 和 Zone ID 不能为空")
        record = input(f"DDNS 完整域名 [{config['RECORD_NAME']}]：").strip() or config["RECORD_NAME"]
        config["RECORD_NAME"] = _name(record, wildcard=True)
        base = config.get("BASE_DOMAIN", "")
        value = input(f"区域域名 [{base or '可留空，管理时查询'}]：").strip() or base
        config["BASE_DOMAIN"] = _name(value) if value else ""
        kind = input(f"记录类型 [{config['RECORD_TYPE']}]：").strip().upper() or config["RECORD_TYPE"]
        if kind not in ("A", "AAAA"):
            raise ValueError("记录类型必须为 A 或 AAAA")
        config["RECORD_TYPE"] = kind
        config["TTL"] = _ttl(input(f"TTL [{config['TTL']}]：").strip() or config["TTL"])
        if input("保存配置？[y/N]：").strip().lower() != "y":
            raise ValueError("已取消保存")
        CFG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CFG_DIR,
                                             prefix=".config-", delete=False) as f:
                temporary = Path(f.name)
                json.dump(config, f, indent=2, ensure_ascii=False)
            temporary.replace(CFG_FILE)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        print(f"配置已保存到 {CFG_FILE}")
        return config

    def setup_logging(self):
        """配置日志系统 - 解决Windows编码问题"""
        log_path = Path(self.config["LOG_FILE"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.logger = logging.getLogger("CloudflareDDNS")
        self.logger.setLevel(logging.INFO)
        
        # 移除所有已存在的处理器
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()
        
        # 日志文件处理器 - 使用UTF-8编码
        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s', 
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        self.logger.addHandler(file_handler)
        
        if sys.platform.startswith('win'):
            self.success_symbol = "[成功]"
            self.refresh_symbol = "=>"
            self.error_symbol = "[错误]"
            self.warning_symbol = "[警告]"
        else:
            self.success_symbol = "✅"
            self.refresh_symbol = "🔄"
            self.error_symbol = "❌"
            self.warning_symbol = "⚠️"
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(message)s', 
            '%H:%M:%S'
        ))
        self.logger.addHandler(console_handler)
    
    def get_public_ip(self, record_type=None):
        """获取当前公网IP (根据配置选择 MTR 路由获取 或 常规 API 获取)"""
        record_type = record_type or self.config["RECORD_TYPE"]
        mtr_target = self.config.get("MTR_TARGET", "").strip()
        
        if record_type == "A":
            # 如果配置了 MTR_TARGET，则尝试通过路由网关抓取
            if mtr_target:
                self.logger.info(f"🔍 正在通过 MTR 探测 [{mtr_target}] 的入口/网关 IP...")
                try:
                    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", mtr_target):
                        raise ValueError("MTR 目标不是有效的地址或主机名")
                    output = subprocess.check_output(
                        ["mtr", "-n", "-rw", "-c", "1", mtr_target], text=True, timeout=15
                    )
                    match = re.search(r"^\s*1\.\|--\s+(\S+)", output, re.MULTILINE)
                    result = str(ipaddress.IPv4Address(match.group(1))) if match else ""
                    if result:
                        self.logger.info(f"🎯 成功定位到网关/入口 IP: {result}")
                        return result
                    else:
                        self.logger.warning(f"{self.warning_symbol} MTR 未抓到有效 IP (当前: {result})，降级使用常规接口...")
                except Exception as e:
                    self.logger.error(f"{self.error_symbol} MTR 探测出错: {e}，降级使用常规接口...")
            else:
                self.logger.info("🔍 未配置 MTR 探测目标，默认获取本机外网出口 IP...")

            # 常规/备用 IPv4 接口池
            ipv4_services = [
                "https://api.ipify.org",
                "https://ipv4.icanhazip.com"
            ]
            
            for service in ipv4_services:
                try:
                    with urlopen(service, timeout=10) as response:
                        ip = str(ipaddress.IPv4Address(response.read(128).decode().strip()))
                    self.logger.info(f"获取到公网IP: {ip} (来自 {service})")
                    return ip
                except Exception as e:
                    self.logger.debug(f"IP服务 {service} 失败: {str(e)}")
                    continue
                    
        elif record_type == "AAAA":
            ipv6_services = [
                "https://api6.ipify.org",
                "https://ipv6.icanhazip.com",
                "https://v6.ident.me"
            ]
            for service in ipv6_services:
                try:
                    with urlopen(service, timeout=10) as response:
                        ip = _content(response.read(128).decode().strip(), "AAAA")
                    if ip:
                        self.logger.info(f"获取到IPv6: {ip}")
                        return ip
                except Exception as e:
                    continue

        self.logger.error("所有IP服务均失败，无法获取公网IP地址")
        return None
    
    def cf_api_request(self, method, endpoint, data=None):
        """使用 Python 标准库访问 Cloudflare，带凭据的请求禁止跟随重定向。"""
        zone_id = quote(self.config["ZONE_ID"], safe="")
        suffix = f"/{endpoint}" if endpoint else ""
        url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}{suffix}"
        headers = {
            "Authorization": f"Bearer {self.config['API_TOKEN']}",
            "Content-Type": "application/json",
        }
        body = json.dumps(data).encode("utf-8") if data is not None else None
        try:
            with build_opener(NoRedirect()).open(Request(url, body, headers, method=method), timeout=15) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("响应不是 JSON 对象")
            return result
        except (OSError, ValueError, HTTPException) as error:
            self.logger.error(f"Cloudflare API 请求失败：{error}")
            return {"success": False, "errors": [{"message": str(error)}]}

    def update_dns(self):
        """主更新逻辑"""
        self.logger.info(f"===== DDNS 更新开始 ({self.config['RECORD_NAME']}) =====")
        
        # 获取当前IP
        current_ip = self.get_public_ip()
        if not current_ip:
            self.logger.error("===== DDNS 更新失败 =====")
            return False
        
        # 查询现有DNS记录
        query = "dns_records?" + urlencode({"name": self.config["RECORD_NAME"], "type": self.config["RECORD_TYPE"]})
        result = self.cf_api_request("GET", query)
        
        if not result.get("success"):
            error = result.get("errors", [{}])[0].get("message", "未知错误")
            self.logger.error(f"Cloudflare API错误: {error}")
            self.logger.error("===== DDNS 更新失败 =====")
            return False
        
        records = result.get("result")
        if not isinstance(records, list):
            self.logger.error("Cloudflare 未返回有效的记录列表")
            return False
        
        # 记录不存在则创建
        if not records:
            self.logger.warning(f"{self.warning_symbol} 记录不存在，正在创建: {self.config['RECORD_NAME']}")
            record_data = {
                "type": self.config["RECORD_TYPE"],
                "name": self.config["RECORD_NAME"],
                "content": current_ip,
                "ttl": self.config["TTL"],
                "proxied": False
            }
            
            create_result = self.cf_api_request("POST", "dns_records", record_data)
            
            if create_result.get("success"):
                record_id = create_result["result"]["id"]
                self.logger.info(f"{self.success_symbol} 创建成功! 记录ID: {record_id}")
                self.logger.info("===== DDNS 更新完成 =====")
                return True
            else:
                error = create_result.get("errors", [{}])[0].get("message", "未知错误")
                self.logger.error(f"{self.error_symbol} 创建失败: {error}")
                self.logger.error("===== DDNS 更新失败 =====")
                return False
        
        # 处理多条记录
        if len(records) > 1:
            self.logger.error("同名同类型记录有多条，请先确定唯一 DDNS 主记录")
            return False
        
        record = records[0]
        record_id = record["id"]
        existing_ip = record["content"]
        
        # 检查IP是否变化
        if ipaddress.ip_address(existing_ip) == ipaddress.ip_address(current_ip):
            self.logger.info(f"{self.refresh_symbol} IP地址未变化 ({existing_ip})，无需更新")
            self.logger.info("===== DDNS 更新完成 =====")
            return True
        
        # 更新DNS记录
        self.logger.info(f"{self.refresh_symbol} 检测到IP变化: {existing_ip} → {current_ip}")
        update_data = {"content": current_ip}
        if not record.get("proxied"):
            update_data["ttl"] = self.config["TTL"]
        update_result = self.cf_api_request("PATCH", f"dns_records/{record_id}", update_data)

        if update_result.get("success"):
            self.logger.info(f"{self.success_symbol} 更新成功! {self.config['RECORD_NAME']} → {current_ip}")
            self.logger.info("===== DDNS 更新完成 =====")
            return True
        else:
            error = update_result.get("errors", [{}])[0].get("message", "未知错误")
            self.logger.error(f"{self.error_symbol} 更新失败: {error}")
            self.logger.error("===== DDNS 更新失败 =====")
            return False

def delete_local_config():
    """删除配置文件和日志文件"""
    deleted_files = []
    
    if CFG_FILE.exists():
        CFG_FILE.unlink()
        deleted_files.append(f"配置文件: {CFG_FILE}")
    
    log_file = CFG_DIR / "cloudflare_ddns.log"
    if log_file.exists():
        log_file.unlink()
        deleted_files.append(f"日志文件: {log_file}")
    
    try:
        if CFG_DIR.exists() and not any(CFG_DIR.iterdir()):
            CFG_DIR.rmdir()
            deleted_files.append(f"配置目录: {CFG_DIR}")
    except OSError:
        pass
    
    return deleted_files

RECORD_TYPES = ("A", "AAAA", "CNAME")


def register_commands(subparsers):
    add = subparsers.add_parser("add", help="添加记录")
    add.add_argument("prefix", help="记录前缀，@ 表示根域名")
    add.add_argument("--type", choices=RECORD_TYPES, default="A")
    add.add_argument("--ip", help="IP 地址或 CNAME 目标域名")
    add.add_argument("--ttl", help="TTL 秒数或 auto")
    add.add_argument("--proxied", action="store_true", help="启用代理")

    update = subparsers.add_parser("update", help="修改记录")
    update.add_argument("prefix")
    update.add_argument("--type", choices=RECORD_TYPES)
    update.add_argument("--ip", help="新 IP 地址或 CNAME 目标域名")
    update.add_argument("--ttl", help="TTL 秒数或 auto")
    proxy = update.add_mutually_exclusive_group()
    proxy.add_argument("--proxied", dest="proxied", action="store_true")
    proxy.add_argument("--no-proxied", dest="proxied", action="store_false")
    update.set_defaults(proxied=None)

    delete = subparsers.add_parser("delete", help="删除一条匹配的记录")
    delete.add_argument("prefix")
    delete.add_argument("--type", choices=RECORD_TYPES)

    sync = subparsers.add_parser("sync", help="将区域内未代理的同类型地址记录对齐到 DDNS 记录")
    sync.add_argument("--type", choices=("A", "AAAA"), help="指定源记录类型")
    sync.add_argument("--dry-run", action="store_true", help="列出预计变更")


def _name(value, wildcard=False):
    if not isinstance(value, str) or not value:
        raise ValueError("域名不能为空")
    ascii_name = value.rstrip(".").encode("idna").decode("ascii").lower()
    labels = ascii_name.split(".")
    for index, label in enumerate(labels):
        if wildcard and index == 0 and label == "*":
            continue
        if not re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", label):
            raise ValueError(f"无效域名 {value}")
    if len(ascii_name) > 253:
        raise ValueError(f"域名过长 {value}")
    return ascii_name


def _content(value, kind):
    if kind == "CNAME":
        if not value:
            raise ValueError("请用 --ip 指定 CNAME 的目标域名")
        name = _name(value)
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return name
        raise ValueError("CNAME 的目标必须是域名")
    if not value:
        raise ValueError(f"未获取到 {kind} 地址")
    ip = ipaddress.ip_address(value)
    if ip.version != {"A": 4, "AAAA": 6}[kind] or "%" in str(ip):
        raise ValueError(f"{value} 与记录类型 {kind} 不匹配")
    return str(ip)


def _ttl(value):
    ttl = 1 if str(value).lower() == "auto" else int(value)
    if not 1 <= ttl <= 86400:
        raise ValueError("TTL 必须在 1 到 86400 之间，或设为 auto")
    return ttl


def _request(api, method, endpoint, data=None):
    response = api(method, endpoint, data)
    if not isinstance(response, dict) or response.get("success") is not True:
        errors = response.get("errors") if isinstance(response, dict) else None
        reason = ", ".join(str(error.get("message", "")) for error in (errors or []) if isinstance(error, dict))
        raise ValueError(f"{method} {endpoint} 失败：{reason or 'Cloudflare 未确认操作成功'}")
    return response


def _records(api, **filters):
    page = 1
    while True:
        params = dict(filters, page=page, per_page=100)
        response = _request(api, "GET", "dns_records?" + urlencode(params))
        rows = response["result"]
        if not isinstance(rows, list):
            raise ValueError("记录列表格式错误")
        yield from rows
        info = response.get("result_info", {})
        if "total_pages" in info:
            if page >= int(info["total_pages"]):
                return
        elif len(rows) < 100:
            return
        if not rows or int(info.get("page", page)) != page:
            raise ValueError("分页信息与查询不符")
        page += 1


def _lookup(api, name, kind=None):
    filters = {"name": name}
    if kind:
        filters["type"] = kind
    records = (r for r in _records(api, **filters)
               if r["name"].lower().rstrip(".") == name and r["type"] in RECORD_TYPES
               and (kind is None or r["type"] == kind))
    first = next(records, None)
    if next(records, None) is not None:
        raise ValueError(f"{name} 匹配多条记录，请指定唯一目标")
    return first


def _endpoint(record):
    if not record.get("id"):
        raise ValueError("记录缺少 ID")
    return "dns_records/" + quote(record["id"], safe="")


def _sync(args, config, api):
    name = _name(config["RECORD_NAME"])
    kind = args.type or config.get("RECORD_TYPE")
    source = _lookup(api, name, kind)
    if not source or source["type"] not in ("A", "AAAA"):
        raise ValueError(f"未找到可用的 A/AAAA 源记录 {name}")
    kind = source["type"]
    address = _content(source["content"], kind)
    changes = []
    for r in _records(api, type=kind):
        if (r["type"] == kind and r["name"].lower().rstrip(".") != name
                and r["proxied"] is False and _content(r["content"], kind) != address):
            changes.append((_endpoint(r), r["name"], r["content"]))
    success = True
    for endpoint, domain, previous in changes:
        print(f"{domain} ({kind})  {previous} → {address}")
        if not args.dry_run:
            try:
                _request(api, "PATCH", endpoint, {"content": address})
            except (OSError, ValueError) as error:
                print(error, file=sys.stderr)
                success = False
    print(f"待变更记录数：{len(changes)}" if args.dry_run else f"本次匹配的变更数：{len(changes)}")
    return success


def run(args, config, api, get_ip):
    """执行已解析的命令，返回所有请求是否成功。"""
    try:
        if args.command == "sync":
            return _sync(args, config, api)
        base = _name(config.get("BASE_DOMAIN"))
        name = base if args.prefix == "@" else _name(f"{args.prefix}.{base}", wildcard=True)
        record = _lookup(api, name, args.type)
        if args.command == "add":
            if record:
                raise ValueError(f"记录已存在：{name} ({args.type})")
            value = args.ip
            if not value and args.type != "CNAME":
                value = get_ip(args.type)
            data = {"name": name, "type": args.type, "content": _content(value, args.type),
                    "ttl": _ttl(args.ttl or config.get("DEFAULT_TTL", config.get("TTL", 1))),
                    "proxied": args.proxied}
            _request(api, "POST", "dns_records", data)
        elif not record:
            raise ValueError(f"未找到目标记录 {name}")
        elif args.command == "delete":
            _request(api, "DELETE", _endpoint(record))
        elif args.command == "update":
            changes = {}
            if args.ip is not None:
                value = _content(args.ip, record["type"])
                if value != _content(record["content"], record["type"]):
                    changes["content"] = value
            if args.ttl is not None:
                changes["ttl"] = _ttl(args.ttl)
            if args.proxied is not None:
                changes["proxied"] = args.proxied
            if args.ip is None and args.ttl is None and args.proxied is None:
                raise ValueError("请指定 IP、TTL 或代理状态")
            changes = {k: v for k, v in changes.items() if record.get(k) != v}
            if changes:
                _request(api, "PATCH", _endpoint(record), changes)
            else:
                print(f"{name} 无需修改")
                return True
        else:
            raise ValueError(f"不支持的命令 {args.command}")
        print(f"{args.command} 成功：{name}")
        return True
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"记录管理失败：{error}", file=sys.stderr)
        return False


def load_manager_config():
    """优先沿用原记录管理器的账号和区域，避免混用不同区域的配置。"""
    path = MANAGER_FILE if MANAGER_FILE.exists() else CFG_FILE
    if not path.exists():
        raise ValueError("请先运行 configure 完成配置，或恢复原管理器 config.json")
    with path.open(encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict) or not config.get("API_TOKEN") or not config.get("ZONE_ID"):
        raise ValueError(f"请检查 {path} 中的 API_TOKEN 和 ZONE_ID")
    if path == MANAGER_FILE:
        config["RECORD_NAME"] = config.get("DDNS_RECORD")
        config.setdefault("DEFAULT_TTL", "auto")
        config.setdefault("LOG_FILE", str(path.with_name("cf_dns_manager.log")))
    else:
        config.setdefault("LOG_FILE", str(LOG_FILE))
    return config


CRON_MARKER = "# cloudflare-ddns:python"
WINDOWS_TASK = r"\Cloudflare-DDNS-Python"


def clean_logs():
    """轮转 DDNS 日志，清理七天前由本脚本生成的日志归档。"""
    with CFG_FILE.open(encoding="utf-8") as f:
        log = Path(json.load(f).get("LOG_FILE") or LOG_FILE)
    prefix = log.name + ".ddns-python-"
    if log.is_file() and not log.is_symlink() and log.stat().st_size:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        log.rename(log.with_name(prefix + stamp))
    if not log.parent.is_dir():
        return
    archive_name = re.compile(re.escape(prefix) + r"[0-9]{8}-[0-9]{6}-[0-9]{6}\Z")
    cutoff = time.time() - 7 * 24 * 60 * 60
    for archive in log.parent.iterdir():
        if (archive_name.fullmatch(archive.name) and not archive.is_symlink()
                and archive.is_file() and archive.stat().st_mtime < cutoff):
            archive.unlink()


def _cron_schedule(action, command):
    env = dict(os.environ, LC_ALL="C")
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True, env=env)
    if current.returncode:
        error = current.stderr.strip()
        if current.returncode != 1 or not (
            error.startswith("no crontab for ")
            or (error.startswith("crontab: can't open ") and error.endswith("No such file or directory"))
        ):
            raise ValueError(error or "读取当前用户的 crontab 失败")
    lines = current.stdout.splitlines(keepends=True)
    markers = (" " + CRON_MARKER, " " + CRON_MARKER + "-logs")
    owned = [line for line in lines if line.rstrip().endswith(markers)]
    if action == "status":
        print("".join(owned).strip() or "尚未设置 Python 版定时任务")
        return
    if action == "off" and not owned:
        print("尚未设置 Python 版定时任务")
        return
    table = "".join(line for line in lines if line not in owned)
    if action != "off":
        if any("\n" in arg or "\r" in arg for arg in command):
            raise ValueError("脚本路径不能包含换行")
        line = " ".join(shlex.quote(arg) for arg in command).replace("%", r"\%")
        timing = "0 * * * *" if action == "60" else f"*/{action} * * * *"
        if table and not table.endswith("\n"):
            table += "\n"
        table += f"{timing} {line} ddns {CRON_MARKER}\n"
        table += f"0 3 * * * {line} clean-logs {CRON_MARKER}-logs\n"
    subprocess.run(["crontab", "-"], input=table, text=True, env=env, check=True)
    print("已删除两项定时任务" if action == "off"
          else f"已设置每 {action} 分钟更新 DDNS，每天 03:00 轮转并清理日志")


def schedule(action):
    if action not in ("status", "off"):
        if not action.isascii() or not action.isdecimal() or not 1 <= int(action) <= 60:
            raise ValueError("间隔请输入 1 到 60 分钟")
        action = str(int(action))
        if not CFG_FILE.exists():
            raise ValueError("请先完成 DDNS 配置")
    command = [sys.executable, str(Path(__file__).resolve())]
    if sys.platform == "win32":
        failed = []
        for name, operation in ((WINDOWS_TASK, "ddns"), (WINDOWS_TASK + "-Logs", "clean-logs")):
            if action == "status":
                args = ["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"]
            elif action == "off":
                args = ["schtasks", "/Delete", "/TN", name, "/F"]
            else:
                timing = ["/SC", "MINUTE", "/MO", action] if operation == "ddns" else ["/SC", "DAILY", "/ST", "03:00"]
                args = ["schtasks", "/Create", "/TN", name, *timing,
                        "/TR", subprocess.list2cmdline(command + [operation]), "/IT", "/F"]
            if subprocess.run(args).returncode:
                failed.append(name)
        if failed:
            raise ValueError(f"以下任务操作未成功，请查看任务状态：{', '.join(failed)}")
    else:
        _cron_schedule(action, command)


def _record_menu(command):
    prefix = input("记录前缀（根域名填 @）：").strip()
    if not prefix:
        return
    args = [command, prefix]
    kind = input("记录类型 A/AAAA/CNAME（回车默认或自动匹配）：").strip().upper()
    if kind:
        args.extend(["--type", kind])
    if command != "delete":
        address = input("IP 或 CNAME 目标（回车使用默认行为）：").strip()
        if address:
            args.extend(["--ip", address])
        ttl = input("TTL 秒数或 auto（回车使用默认值）：").strip()
        if ttl:
            args.extend(["--ttl", ttl])
        proxy = input("代理 [on/off，回车沿用默认或当前状态]：").strip().lower()
        if proxy not in ("", "on", "off"):
            raise ValueError("代理状态请填写 on 或 off")
        if proxy == "on":
            args.append("--proxied")
        if proxy == "off" and command == "update":
            args.append("--no-proxied")
    elif input(f"删除记录 {prefix}？[y/N]：").strip().lower() != "y":
        return
    main(args)


def menu():
    while True:
        print("""
Cloudflare DDNS 与 DNS 管理
  1. 配置或修改 DDNS          2. 立即更新 DDNS
  3. 新增 DNS 记录             4. 修改 DNS 记录
  5. 删除 DNS 记录             6. 预览并同步其他记录
  7. 查看或设置定时任务        0. 退出
""")
        try:
            choice = input("请选择：").strip()
            if choice == "0":
                return 0
            if choice == "1":
                main(["configure"])
            elif choice == "2":
                main(["ddns"])
            elif choice in ("3", "4", "5"):
                _record_menu({"3": "add", "4": "update", "5": "delete"}[choice])
            elif choice == "6":
                kind = input("源记录类型 A/AAAA（回车使用配置）：").strip().upper()
                args = ["sync"] + (["--type", kind] if kind else [])
                if main(args + ["--dry-run"]) == 0 and input("确认同步？[y/N]：").strip().lower() == "y":
                    main(args)
            elif choice == "7":
                action = input("1 添加两项任务，2 删除两项任务，3 查看，0 返回：").strip()
                if action == "1":
                    minutes = input("DDNS 执行间隔（分钟，默认 5）：").strip() or "5"
                    schedule(minutes)
                elif action == "2" and input("删除两项定时任务？[y/N]：").strip().lower() == "y":
                    schedule("off")
                elif action == "3":
                    schedule("status")
            else:
                print("请输入菜单中的数字")
        except (EOFError, KeyboardInterrupt):
            return 0
        except SystemExit:
            continue  # argparse 已显示错误，允许在菜单中重新选择。
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"操作失败：{exc}", file=sys.stderr)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(description="Cloudflare DDNS 和 DNS 记录管理")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("-reconfig", action="store_true", help="移除 DDNS 配置，下次运行重新配置")
    modes.add_argument("-delete", action="store_true", help="清除本地 DDNS 配置和默认日志")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("menu", help="打开交互菜单")
    commands.add_parser("configure", help="创建或编辑 DDNS 配置")
    commands.add_parser("ddns", help="更新 DDNS 主记录")
    scheduler = commands.add_parser("schedule", help="查看、设置或取消 DDNS 定时任务")
    scheduler.add_argument("action", nargs="?", default="status", help="status、off 或间隔分钟数")
    commands.add_parser("clean-logs", help="轮转日志并删除七天前的归档")
    register_commands(commands)
    args = parser.parse_args(argv)
    if args.command and (args.reconfig or args.delete):
        parser.error("本地配置维护选项不能与子命令组合使用")
    if args.delete:
        for item in delete_local_config():
            print(item)
        return 0
    if args.reconfig:
        if CFG_FILE.exists():
            CFG_FILE.unlink()
        print("重新运行 ddns 命令进行配置")
        return 0
    try:
        if args.command == "menu" or (not argv and sys.stdin.isatty() and sys.stdout.isatty()):
            return menu()
        if args.command == "schedule":
            schedule(args.action)
            return 0
        if args.command == "clean-logs":
            clean_logs()
            return 0
        if args.command == "configure":
            client = CloudflareDDNS.__new__(CloudflareDDNS)
            existing = client.load_config() if CFG_FILE.exists() else None
            client.setup_wizard(existing)
            return 0
        if args.command in (None, "ddns"):
            ok = CloudflareDDNS().update_dns()
        else:
            config = load_manager_config()
            client = CloudflareDDNS(config)
            if args.command != "sync" and not config.get("BASE_DOMAIN"):
                zone = _request(client.cf_api_request, "GET", "")["result"]
                config["BASE_DOMAIN"] = _name(zone["name"])
            ok = run(args, config, client.cf_api_request, client.get_public_ip)
        return 0 if ok else 1
    except (KeyboardInterrupt, EOFError):
        print("操作已取消", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
