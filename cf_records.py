"""Cloudflare DNS 记录管理。所有网络访问由调用方提供。"""

import ipaddress
import re
import sys
from urllib.parse import quote, urlencode


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
