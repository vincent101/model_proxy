#!/usr/bin/env python3
"""model_proxy 入口：转发到 core.server.main()。

入口文件名与路径（tools/model_proxy/model_proxy.py）刻意保持不变，
以维持 model_proxy_cli.sh 基于该路径的进程识别逻辑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.server import main

if __name__ == "__main__":
    main()
