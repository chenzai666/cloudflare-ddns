#!/usr/bin/env python3
"""兼容原 DNS 管理器的命令入口。"""

import sys

from cloudflare_ddns import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["--help"]))
