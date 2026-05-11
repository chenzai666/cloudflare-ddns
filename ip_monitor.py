#!/usr/bin/env python3
"""
IP监控脚本 - 公网IP专用版本
功能：
1. 准确获取公网IP地址
2. 使用人类可读的日志格式
3. 跨平台兼容（Windows / Linux / macOS）
4. 支持代理
"""

import os
import sys
import argparse
import platform
import json
import ssl
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# 默认设置（跨平台）
DEFAULT_LOG_DIR = str(Path.home() / ".ip_monitor")
DEFAULT_LOG_FILE = os.path.join(DEFAULT_LOG_DIR, "ip_history.log")

IP_API_SERVICES = [
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
    "https://ipinfo.io/ip"
]

def get_public_ip(proxy=None):
    """准确获取公网IP地址"""
    # 注意：禁用SSL验证存在安全风险，仅用于IP查询场景
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # 将自定义 SSL 上下文包装成 HTTPSHandler，传给 build_opener
    https_handler = urllib.request.HTTPSHandler(context=ctx)

    handlers = [https_handler]
    if proxy:
        # proxy 格式如 "http://127.0.0.1:7890"
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))

    opener = urllib.request.build_opener(*handlers)

    for service in IP_API_SERVICES:
        try:
            req = urllib.request.Request(service, headers={"User-Agent": user_agent})
            with opener.open(req, timeout=10) as response:
                ip = response.read().decode('utf-8').strip()
                if ip and validate_ip(ip):
                    return ip
        except (urllib.error.URLError, urllib.error.HTTPError,
                ConnectionResetError, TimeoutError) as e:
            if hasattr(e, 'reason'):
                reason = str(e.reason)
            else:
                reason = str(e)
            print(f"  - 服务 {service} 失败: {reason}")
            continue
        except Exception as e:
            print(f"  - 服务 {service} 出错: {str(e)}")
            continue

    print("错误: 所有公网IP服务均失败，无法获取公网IP!")
    return None


def validate_ip(ip):
    """验证IP地址格式，并排除私有地址"""
    parts = ip.split('.')
    if len(parts) != 4:
        return False

    nums = []
    for part in parts:
        if not part.isdigit():
            return False
        num = int(part)
        if num < 0 or num > 255:
            return False
        nums.append(num)

    # 排除私有IP地址范围（RFC 1918）
    # 10.0.0.0/8
    if nums[0] == 10:
        return False
    # 172.16.0.0/12 → 172.16.0.0 ~ 172.31.255.255
    if nums[0] == 172 and 16 <= nums[1] <= 31:
        return False
    # 192.168.0.0/16
    if nums[0] == 192 and nums[1] == 168:
        return False

    return True


def setup_logging(log_path):
    """创建日志目录和文件"""
    try:
        log_dir = os.path.dirname(log_path)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
            print(f"已创建日志目录: {log_dir}")

        if not os.path.exists(log_path):
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write("# IP监控日志 - 格式: [时间] [状态] [IP地址] [变化详情]\n")
                f.write("# 状态: INIT=初始, CHANGED=变更, UNCHANGED=未变\n")
                f.write("#" * 80 + "\n")
            try:
                os.chmod(log_path, 0o644)
            except AttributeError:
                pass  # Windows 不支持 chmod
            print(f"已创建日志文件: {log_path}")

        return True
    except Exception as e:
        print(f"日志设置失败: {str(e)}")
        return False


def write_log_entry(log_path, timestamp, status, ip, previous_ip=None):
    """写入人类可读的日志条目"""
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            if status == "INIT":
                f.write(f"[{timestamp}] INIT      {ip} (首次记录)\n")
            elif status == "CHANGED":
                f.write(f"[{timestamp}] CHANGED   {previous_ip} -> {ip}\n")
            else:  # UNCHANGED
                f.write(f"[{timestamp}] UNCHANGED {ip} (与上次相同)\n")
        return True
    except Exception as e:
        print(f"日志写入失败: {str(e)}")
        return False


def read_last_ip(log_path):
    """从日志文件中读取上一次记录的IP（二进制读取，兼容任何编码）"""
    if not os.path.exists(log_path):
        return None

    try:
        import re
        # 用二进制模式读取，完全不依赖文件编码
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size == 0:
                return None

            pos = file_size
            buf = bytearray()
            while pos > 0:
                pos -= 1
                f.seek(pos)
                ch = f.read(1)
                if ch == b'\n' and len(buf) > 0:
                    line = buf.decode('ascii', errors='replace')[::-1].strip()
                    if any(k in line for k in ('UNCHANGED', 'CHANGED', 'INIT')):
                        match = re.search(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', line)
                        if match:
                            return match.group(1)
                    buf = bytearray()
                else:
                    buf.extend(ch)
            # 文件末尾没有换行的情况
            if buf:
                line = buf.decode('ascii', errors='replace')[::-1].strip()
                match = re.search(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', line)
                if match:
                    return match.group(1)
    except Exception as e:
        print(f"读取日志文件失败: {str(e)}")

    return None


def main():
    print("=" * 60)
    print(f"IP监控脚本启动 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"操作系统: {platform.system()} {platform.release()}")
    print(f"Python版本: {platform.python_version()}")
    print("=" * 60)

    parser = argparse.ArgumentParser(
        description='公网IP监控工具',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        '--log', default=DEFAULT_LOG_FILE,
        help='日志文件路径'
    )
    parser.add_argument(
        '--proxy', default=None,
        help='使用代理服务器（格式: http://proxy:port）'
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='显示详细输出'
    )
    args = parser.parse_args()

    log_path = os.path.abspath(args.log)

    # 创建日志目录和文件
    if not setup_logging(log_path):
        print("错误: 日志设置失败，退出程序")
        sys.exit(1)

    # 获取当前时间和公网IP
    start_time = datetime.now()
    timestamp = start_time.strftime('%Y-%m-%d %H:%M:%S')

    print("正在获取公网IP...")
    if args.verbose:
        print(f"  - 使用代理: {args.proxy if args.proxy else '无'}")
        print(f"  - 尝试的服务: {', '.join(IP_API_SERVICES)}")

    current_ip = get_public_ip(args.proxy)

    if not current_ip:
        error_msg = f"[{timestamp}] 错误: 无法获取公网IP"
        print(error_msg)
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] ERROR 无法获取公网IP\n")
        except Exception:
            pass
        sys.exit(1)

    # 读取上次记录的IP
    last_ip = read_last_ip(log_path)

    # 确定状态
    if last_ip is None:
        status = "INIT"
    elif current_ip != last_ip:
        status = "CHANGED"
    else:
        status = "UNCHANGED"

    # 写入日志文件
    write_log_entry(log_path, timestamp, status, current_ip, last_ip)

    # 控制台输出
    exec_time = (datetime.now() - start_time).total_seconds()

    print("\n公网IP监控结果:")
    print(f"  - 公网IP地址: {current_ip}")

    # 再次检查（validate_ip 已排除私有IP，能到这里说明是公网IP）
    # 额外警告：如果 validate_ip 被绕过，这里兜底提示
    if (current_ip.startswith("10.") or
            current_ip.startswith("192.168.") or
            (current_ip.startswith("172.") and
             16 <= int(current_ip.split('.')[1]) <= 31)):
        print("  ⚠ 警告: 检测到私有IP地址，可能未正确获取公网IP")

    if status == "INIT":
        print(f"  - 状态: 首次记录")
    elif status == "CHANGED":
        print(f"  - 状态: IP变更 ({last_ip} -> {current_ip})")
    else:
        print(f"  - 状态: IP未变化")

    print(f"  - 日志文件: {log_path}")
    print(f"  - 执行耗时: {exec_time:.2f}秒")
    print("-" * 60)
    print(f"IP监控完成 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
