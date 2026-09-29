#!/usr/bin/env python3
"""forage 入口：``python -m forage <cmd>``。

★ ``sys.dont_write_bytecode = True`` 必须在任何 forage.* 导入之前设：
  旧版踩过三次的坑 —— 改完代码仍加载 __pycache__ 里的旧 .pyc，
  报 ImportError / KeyError，看着像代码写错了，其实是缓存。
"""
import sys

sys.dont_write_bytecode = True

from .cli import main  # noqa: E402

if __name__ == "__main__":
    main()
