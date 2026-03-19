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
import importlib.util
import re
from pathlib import Path
from datetime import datetime

# 全局配置目录
CFG_DIR = Path.home() / ".cloudflare_ddns"
CFG_FILE = CFG_DIR / "config.json"
LOG_FILE = CFG_DIR / "cloudflare_ddns.log"

# ==========================================
# 环境依赖自动准备区
# ==========================================
def check_dependencies():
    """确保必要的依赖已安装"""
    required = {'requests'}
    installed = set()
    
    for module in required:
        if importlib.util.find_spec(module) is None:
            print(f"⏳ 缺少必要模块: {module}")
            
            # 尝试使用pip安装 (加入强制绕过系统环境限制和清华源)
            python_exe = sys.executable
            pip_cmd = [
                python_exe, '-m', 'pip', 'install', module, 
                '--break-system-packages', 
                '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'
            ]
            
            print(f"正在尝试强制安装 {module}...")
            try:
                subprocess.check_call(pip_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"✅ {module} 安装成功")
                installed.add(module)
            except subprocess.CalledProcessError:
                print(f"❌ 无法自动安装 {module}")
            
            # 如果pip不可用，提示用户手动安装
            try:
                subprocess.check_call([python_exe, '-m', 'pip', '--version'], 
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.CalledProcessError, FileNotFoundError):
                print("\n" + "="*50)
                print("系统缺少pip包管理器，请手动安装:")
                print("1. 首先安装pip:")
                print("   Ubuntu/Debian: sudo apt install python3-pip")
                print("   CentOS/RHEL: sudo yum install python3-pip")
                print("2. 然后手动安装依赖:")
                print(f"   pip install {module} --break-system-packages")
                print("="*50)
                print("\n")
    
    # 再次检查所有依赖是否安装成功
    if not all(importlib.util.find_spec(m) for m in required):
        print("❌ 依赖安装失败，请手动安装必要的Python模块")
        sys.exit(1)

def check_mtr_tool():
    """检查并自动安装 mtr 工具 (仅限 Linux)"""
    if sys.platform.startswith('linux'):
        if subprocess.call("command -v mtr", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            print("⏳ 未检测到 mtr 工具，正在通过 apt 自动为您安装...")
            try:
                subprocess.check_call("apt-get update && apt-get install mtr -y", shell=True, stdout=subprocess.DEVNULL)
                print("✅ mtr 工具安装完成！")
            except Exception as e:
                print(f"⚠️ mtr 自动安装失败，请稍后手动执行: sudo apt install mtr -y")

# 在脚本开头检查依赖
check_dependencies()
check_mtr_tool()

# 导入已确认安装的模块
import requests

# ==========================================
# 核心逻辑区
# ==========================================
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
    def __init__(self):
        # 确保配置目录存在
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        self.config = self.load_config()
        self.setup_logging()
        
    def load_config(self):
        """加载或创建配置"""
        if CFG_FILE.exists():
            try:
                with open(CFG_FILE, 'r') as f:
                    config = json.load(f)
                    # 验证必要配置
                    if not config.get("API_TOKEN") or not config.get("ZONE_ID"):
                        raise ValueError("缺少必要配置")
                    
                    # 兼容旧版本配置文件，如果没有 MTR_TARGET 则设置为空
                    if "MTR_TARGET" not in config:
                        config["MTR_TARGET"] = ""
                    return config
            except Exception as e:
                print(f"配置文件损坏: {e}")
        
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
        config["API_TOKEN"] = input("1. 请输入Cloudflare API Token: ").strip()
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
        
        # 保存配置
        with open(CFG_FILE, 'w') as f:
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
    
    def get_public_ip(self):
        """获取当前公网IP (根据配置选择 MTR 路由获取 或 常规 API 获取)"""
        record_type = self.config["RECORD_TYPE"]
        mtr_target = self.config.get("MTR_TARGET", "").strip()
        
        if record_type == "A":
            # 如果配置了 MTR_TARGET，则尝试通过路由网关抓取
            if mtr_target:
                self.logger.info(f"🔍 正在通过 MTR 探测 [{mtr_target}] 的入口/网关 IP...")
                try:
                    cmd = f"mtr -rw -c 1 {mtr_target} | awk 'NR==3 {{print $2}}'"
                    result = subprocess.check_output(cmd, shell=True).decode().strip()
                    
                    # 验证是否抓取到有效的 IPv4 地址 (去除113限制)
                    if re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", result):
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
                "http://txt.go.sohu.com/ip/sohu",
                "https://api.ipify.org",
                "https://ipv4.icanhazip.com"
            ]
            
            for service in ipv4_services:
                try:
                    response = requests.get(service, timeout=10)
                    response.raise_for_status()
                    ip_match = re.search(r'\d+\.\d+\.\d+\.\d+', response.text)
                    if ip_match:
                        ip = ip_match.group()
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
                    ip = response.text.strip()
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
            if method == "GET":
                response = requests.get(url, headers=headers)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=data)
            elif method == "PUT":
                response = requests.put(url, headers=headers, json=data)
            else:
                raise ValueError(f"不支持的HTTP方法: {method}")
                
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
        query = f"dns_records?name={self.config['RECORD_NAME']}&type={self.config['RECORD_TYPE']}"
        result = self.cf_api_request("GET", query)
        
        if not result.get("success"):
            error = result.get("errors", [{}])[0].get("message", "未知错误")
            self.logger.error(f"Cloudflare API错误: {error}")
            self.logger.error("===== DDNS 更新失败 =====")
            return False
        
        records = result.get("result", [])
        
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
            self.logger.warning(f"{self.warning_symbol} 找到 {len(records)} 条匹配记录，将使用第一条")
        
        record = records[0]
        record_id = record["id"]
        existing_ip = record["content"]
        
        # 检查IP是否变化
        if existing_ip == current_ip:
            self.logger.info(f"{self.refresh_symbol} IP地址未变化 ({existing_ip})，无需更新")
            self.logger.info("===== DDNS 更新完成 =====")
            return True
        
        # 更新DNS记录
        self.logger.info(f"{self.refresh_symbol} 检测到IP变化: {existing_ip} → {current_ip}")
        update_data = {
            "type": self.config["RECORD_TYPE"],
            "name": self.config["RECORD_NAME"],
            "content": current_ip,
            "ttl": self.config["TTL"],
            "proxied": False
        }
        
        update_result = self.cf_api_request("PUT", f"dns_records/{record_id}", update_data)
        
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

if __name__ == "__main__":
    # 命令行参数解析
    parser = argparse.ArgumentParser(description='Cloudflare DDNS 更新脚本')
    parser.add_argument('-reconfig', action='store_true', help='重置配置文件并重新配置')
    parser.add_argument('-delete', action='store_true', help='删除所有配置文件和日志')
    args = parser.parse_args()
    
    # 删除配置选项
    if args.delete:
        deleted = delete()
        if deleted:
            print("✅ 已删除以下文件:")
            for file in deleted:
                print(f"  - {file}")
        else:
            print("⚠️ 未找到配置文件或日志文件")
        sys.exit()
    
    # 重新配置选项
    if args.reconfig:
        if CFG_FILE.exists():
            CFG_FILE.unlink()
            print("✅ 配置已重置")
            print("请重新运行脚本进行配置")
        else:
            print("配置文件不存在，无需重置")
        sys.exit()
    
    try:
        ddns = CloudflareDDNS()
        success = ddns.update_dns()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n操作已取消")
        sys.exit(1)
    except Exception as e:
        print(f"程序异常: {str(e)}")
        sys.exit(1)
