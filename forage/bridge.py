"""forage · memory-bridge 的唯一接入点。

所有 ``sys.path.insert`` / ``.env`` 加载 / ``build_pipeline`` 都收在这一个文件里；
其它模块只通过 ``get_pipeline()`` / ``env_config()`` 拿装配好的对象图，
不直接碰 memory_bridge 的内部路径。

不做什么：
  ❌ 不写任何检索逻辑（BM25 / 向量 / RRF / 精排都在 memory-bridge）
  ❌ 不 catch ``build_pipeline`` 的异常 —— 缺 DSN / 缺模型要显式报错
  ❌ 不 import 除 memory_bridge.* 之外的第三方库

★ ``.env`` 加载陷阱（实测踩到）：``load_env_file`` 只返回 dict，不写 os.environ；
  而 ``load_config`` 只从 os.environ 读。必须显式 ``load_config(env=env)``。
"""

from __future__ import annotations

import sys
from pathlib import Path

# —— memory-bridge 根目录（跨平台：可用环境变量覆盖）——
#
# WSL/Linux 默认 /root/code/memory-bridge；Windows 默认桌面 code 下的副本。
# ★ 两侧共用同一份 PG（DSN 指向 127.0.0.1:5432，由 WSL 提供）。

def _default_mb_root() -> str:
    import os
    from pathlib import Path

    env_root = os.environ.get("FORAGE_MB_ROOT")
    if env_root:
        return env_root
    # 先找与 forage 仓库同级的 memory-bridge —— clone 下来最常见的布局，
    # 作者本机和陌生人都适用，不写死任何人的家目录。
    sibling = Path(__file__).resolve().parents[2] / "memory-bridge"
    for cand in (str(sibling), "/root/code/memory-bridge"):
        if os.path.isdir(cand):
            return cand
    return str(sibling)


MB_ROOT = _default_mb_root()
MB_ENV = str(Path(MB_ROOT) / ".env")
MB_SCRIPTS = str(Path(MB_ROOT) / "scripts")

COLLECTION = "techblog"
ACL = ["public"]

_cfg = None
_pipeline = None


def _ensure_syspath() -> None:
    """memory-bridge 不是 pip 包，靠 sys.path 接入。"""
    for path in (MB_SCRIPTS, MB_ROOT):
        if path not in sys.path:
            sys.path.insert(0, path)


def env_config():
    """返回 ``(cfg, pipeline)`` —— 惰性单例，模型只加载一次。"""
    global _cfg, _pipeline
    if _pipeline is not None:
        return _cfg, _pipeline

    _ensure_syspath()
    from memory_bridge.cli import build_pipeline
    from memory_bridge.config import load_config, load_env_file

    env = load_env_file(Path(MB_ENV))
    cfg = load_config(env=env)  # ★ 必须显式传 env=，否则 .env 不生效
    pipeline = build_pipeline(cfg)

    # 失败域⑦：模型缺失时 build_pipeline 会退到 HashEmbedder / NoopReranker。
    # 打印警告但继续（这也是 memory-bridge 自己的策略）。
    missing = list(getattr(pipeline, "missing_models", []) or [])
    if missing:
        print(
            f"[warn] memory-bridge 模型缺失：{missing}，"
            "已退到 HashEmbedder/NoopReranker，检索质量会下降",
            file=sys.stderr,
        )

    _cfg, _pipeline = cfg, pipeline
    return _cfg, _pipeline


def get_pipeline():
    """惰性单例：memory-bridge 装配好的 Pipeline。"""
    return env_config()[1]


def get_chunker():
    """两级切块器（父 2000 / 子 400 / overlap 60），与既有语料参数一致。"""
    _ensure_syspath()
    from kb_ingest import MarkdownChunker

    return MarkdownChunker()


__all__ = [
    "MB_ROOT",
    "MB_ENV",
    "MB_SCRIPTS",
    "COLLECTION",
    "ACL",
    "env_config",
    "get_pipeline",
    "get_chunker",
]
