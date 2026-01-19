#!/usr/bin/env python3
"""
Cloudflare DDNS 更新脚本 (Python跨平台版)
修复Windows编码问题，增强日志兼容性
"""

import os
import sys
import json
import logging
import argparse
import subprocess
import importlib.util
from pathlib import Path
from datetime import datetime

# 全局配置目录
CFG_DIR = Path.home() / ".cloudflare_ddns"
CFG_FILE = CFG_DIR / "config.json"
LOG_FILE = CFG_DIR / "cloudflare_ddns.log"

# 检查并安装依赖
def check_dependencies():
    """确保必要的依赖已安装"""
    required = {'requests'}
    installed = set()
    
    for module in required:
        if importlib.util.find_spec(module) is None:
            print(f"缺少必要模块: {module}")
            
            # 尝试使用pip安装
            python_exe = sys.executable
            pip_cmd = [python_exe, '-m', 'pip', 'install', module]
            
            print(f"正在尝试安装 {module}...")
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
                print("   Windows: python -m ensurepip")
                print("2. 然后手动安装依赖:")
                print(f"   pip install {module}")
                print("="*50)
                print("\n")
    
    # 再次检查所有依赖是否安装成功
    if not all(importlib.util.find_spec(m) for m in required):
        print("❌ 依赖安装失败，请手动安装必要的Python模块")
        print("   运行: pip install requests")
        sys.exit(1)

# 在脚本开头检查依赖
check_dependencies()

# 导入已确认安装的模块
import requests

# 配置模板
DEFAULT_CONFIG = {
    "API_TOKEN": "",
    "ZONE_ID": "",
    "RECORD_NAME": "ddns.example.com",
    "RECORD_TYPE": "A",
    "TTL": 60,
    "LOG_FILE": str(LOG_FILE)
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
        config["RECORD_TYPE"] = input(f"4. 记录类型 [A/AAAA] (默认: {default_type}): ").strip() or default_type
        
        default_ttl = DEFAULT_CONFIG["TTL"]
        ttl_input = input(f"5. TTL值 [1-86400] (默认: {default_ttl}): ").strip()
        config["TTL"] = int(ttl_input) if ttl_input.isdigit() else default_ttl
        
        default_log = str(LOG_FILE)
        log_input = input(f"6. 日志文件路径 (默认: {default_log}): ").strip() or default_log
        config["LOG_FILE"] = log_input
        
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
        
        # ASCII字符替代方案（Windows兼容）
        # 在Windows上使用纯ASCII字符，其他平台使用Unicode符号
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
        
        # 控制台处理器 - 使用安全的编码处理
        console_handler = logging.StreamHandler()
        try:
            # 尝试设置控制台编码为UTF-8
            if sys.stdout.encoding != 'utf-8':
                import io
                sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        except:
            pass
        
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(message)s', 
            '%H:%M:%S'
        ))
        self.logger.addHandler(console_handler)
    
    def get_public_ip(self):
        """获取当前公网IP"""
        services = {
            "A": [
                "https://api.ipify.org",
                "https://ipv4.icanhazip.com",
                "https://checkip.amazonaws.com"
            ],
            "AAAA": [
                "https://api6.ipify.org",
                "https://ipv6.icanhazip.com",
                "https://v6.ident.me"
            ]
        }
        
        record_type = self.config["RECORD_TYPE"]
        for service in services[record_type]:
            try:
                response = requests.get(service, timeout=10)
                response.raise_for_status()
                ip = response.text.strip()
                if ip:
                    self.logger.info(f"获取到公网IP: {ip}")
                    return ip
            except Exception as e:
                self.logger.debug(f"IP服务 {service} 失败: {str(e)}")
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
            # 提取JSON错误信息（如果存在）
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
        """主更新逻辑 - 使用平台相关符号"""
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
            self.logger.info(f"{self.refresh_symbol} IP地址未变化，无需更新")
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

if __name__ == "__main__":
    # 命令行参数解析
    parser = argparse.ArgumentParser(description='Cloudflare DDNS 更新脚本')
    parser.add_argument('--reconfigure', action='store_true', help='重新配置参数')
    args = parser.parse_args()
    
    # 重新配置选项
    if args.reconfigure:
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
        # 使用简单的日志记录避免编码问题
        print(f"程序异常: {str(e)}")
        sys.exit(1)
