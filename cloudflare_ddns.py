#!/usr/bin/env python3
"""
Cloudflare DDNS 更新脚本 (Python跨平台版)
新增 MTR 动态路由网关探测 & 可选目标配置功能
"""

import os
import sys
import json
import logging
import argparse
import subprocess
import ipaddress
from getpass import getpass
from urllib.parse import urlencode
import re
from pathlib import Path

# 全局配置目录
CFG_DIR = Path.home() / ".cloudflare_ddns"
CFG_FILE = CFG_DIR / "config.json"
LOG_FILE = CFG_DIR / "cloudflare_ddns.log"

MANAGER_FILE = Path.home() / ".cf_dns_manager" / "config.json"

# 延迟加载依赖，让帮助和本地配置维护可以独立运行。
requests = None

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
        return self.setup_wizard()
    
    def setup_wizard(self):
        """交互式配置向导"""
        print("\n" + "="*50)
        print("Cloudflare DDNS 配置向导".center(50))
        print("="*50)
        print("提示：括号内为默认值，直接按回车使用默认设置\n")
        
        config = DEFAULT_CONFIG.copy()
        
        # 获取必要信息
        config["API_TOKEN"] = getpass("1. 请输入Cloudflare API Token: ").strip()
        if not config["API_TOKEN"]:
            print("错误：API Token不能为空！")
            sys.exit(1)
            
        config["ZONE_ID"] = input("2. 请输入Zone ID: ").strip()
        if not config["ZONE_ID"]:
            print("错误：Zone ID不能为空！")
            sys.exit(1)
            
        default_name = DEFAULT_CONFIG["RECORD_NAME"]
        config["RECORD_NAME"] = input(f"3. 请输入要更新的域名 (默认: {default_name}): ").strip() or default_name
        
        default_type = DEFAULT_CONFIG["RECORD_TYPE"]
        type_input = input(f"4. 记录类型 [A/AAAA] (默认: {default_type}): ").strip()
        config["RECORD_TYPE"] = type_input if type_input else default_type
        if config["RECORD_TYPE"] not in ["A", "AAAA"]:
            print(f"错误: 不支持的记录类型 '{config['RECORD_TYPE']}', 请使用 A 或 AAAA")
            sys.exit(1)
        
        default_ttl = DEFAULT_CONFIG["TTL"]
        ttl_input = input(f"5. TTL值 [1-86400] (默认: {default_ttl}): ").strip()
        config["TTL"] = int(ttl_input) if ttl_input.isdigit() else default_ttl
        
        default_log = str(LOG_FILE)
        log_input = input(f"6. 日志文件路径 (默认: {default_log}): ").strip() or default_log
        config["LOG_FILE"] = log_input

        # 新增 MTR 目标配置
        mtr_input = input("7. [高级选项] MTR探测目标地址 (留空则默认获取本机常规外网出口IP，输入如 dix.yiandrive.com 则获取第一跳网关IP): ").strip()
        config["MTR_TARGET"] = mtr_input
        
        config["BASE_DOMAIN"] = input("8. DNS 管理使用的区域域名（仅 DDNS 可留空）: ").strip()

        # 保存配置
        with open(CFG_FILE, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
            json.dump(config, f, indent=2)
            
        print("\n✅ 配置已保存至:", CFG_FILE)
        print("📝 日志将记录到:", config["LOG_FILE"])
        print("="*50)
        
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
                    response = requests.get(service, timeout=10)
                    response.raise_for_status()
                    ip = str(ipaddress.IPv4Address(response.text.strip()))
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
                    response = requests.get(service, timeout=10)
                    response.raise_for_status()
                    ip = str(ipaddress.IPv6Address(response.text.strip()))
                    if ip:
                        self.logger.info(f"获取到IPv6: {ip}")
                        return ip
                except Exception as e:
                    continue

        self.logger.error("所有IP服务均失败，无法获取公网IP地址")
        return None
    
    def cf_api_request(self, method, endpoint, data=None):
        """发送Cloudflare API请求"""
        url = f"https://api.cloudflare.com/client/v4/zones/{self.config['ZONE_ID']}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.config['API_TOKEN']}",
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.request(method, url, headers=headers, json=data, timeout=15,
                                        allow_redirects=False)
            response.raise_for_status()
            return response.json()
                
        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            try:
                error_resp = e.response.json()
                if "errors" in error_resp:
                    errors = ', '.join([err["message"] for err in error_resp["errors"]])
                    error_msg = f"{e} | {errors}"
            except:
                pass
                
            self.logger.error(f"API请求失败: {error_msg}")
            return {"success": False, "errors": [{"message": error_msg}]}
    
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

def delete():
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cloudflare DDNS 和 DNS 记录管理")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("-reconfig", action="store_true", help="移除 DDNS 配置，下次运行重新配置")
    modes.add_argument("-delete", action="store_true", help="清除本地 DDNS 配置和默认日志")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("configure", help="创建或加载 DDNS 配置，不更新 DNS")
    commands.add_parser("ddns", help="更新 DDNS 主记录（默认命令）")
    try:
        import cf_records
    except ModuleNotFoundError as exc:
        if exc.name != "cf_records":
            raise
        cf_records = None  # 保留原先只下载 cloudflare_ddns.py 的用法。
        parser.epilog = "要使用记录管理命令，请下载完整仓库。"
    if cf_records:
        cf_records.register_commands(commands)
    args = parser.parse_args(argv)
    if args.command and (args.reconfig or args.delete):
        parser.error("本地配置维护选项不能与子命令组合使用")
    if args.delete:
        for item in delete():
            print(item)
        return 0
    if args.reconfig:
        if CFG_FILE.exists():
            CFG_FILE.unlink()
        print("重新运行 ddns 命令进行配置")
        return 0
    if args.command == "configure":
        try:
            CloudflareDDNS()
            print(f"配置已就绪：{CFG_FILE}")
            return 0
        except (ValueError, OSError, EOFError) as exc:
            print(f"配置失败：{exc}", file=sys.stderr)
            return 1
    global requests
    try:
        import requests
    except ImportError:
        print("请先在 Python 环境中安装 requests", file=sys.stderr)
        return 1
    try:
        if args.command in (None, "ddns"):
            ok = CloudflareDDNS().update_dns()
        else:
            config = load_manager_config()
            if args.command != "sync" and not config.get("BASE_DOMAIN"):
                raise ValueError("请在配置中填写 BASE_DOMAIN，例如 example.com")
            client = CloudflareDDNS(config)
            ok = cf_records.run(args, config, client.cf_api_request, client.get_public_ip)
        return 0 if ok else 1
    except (KeyboardInterrupt, EOFError):
        print("操作已取消", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
