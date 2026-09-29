#!/usr/bin/env python3
"""forage 入口 —— 支持两条调用路径：

    python -m forage <cmd>      # 零安装（项目根目录下）
    forage <cmd>                # pip install -e . 之后

★ sys.dont_write_bytecode = True 必须在任何 forage.* 导入之前设。
  踩过三次的坑：改完 forage/ 下的模块，Python 仍加载 __pycache__ 里的旧 .pyc，
  报 ImportError / KeyError，看着像代码写错了，其实是缓存。
  在入口处关掉字节码写入 = 根治（代价只是稍慢一点）。
"""
import sys

sys.dont_write_bytecode = True

from .cli import main

if __name__ == "__main__":
    main()
