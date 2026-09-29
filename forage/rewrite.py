"""forage · 查询改写层（搬自旧版，删掉里面的检索逻辑）。

职责只剩一件事：中文 query → 3-5 组英文关键词（OpenAI 兼容 /chat/completions）。

口径：
  ★ 改写是「加一路」不是「换掉」 —— 原 query 必留（技术博客里 useEffect /
    pg_advisory_lock / v18.2 全是精确串，改写会倾向泛化把它们抹掉）
  ★ fail-open：改写后端挂了/超时/返回垃圾 → 返回 []，绝不让检索失败
  ★ 结果落 SQLite 缓存，同一个 query 只调一次
  ★ enable_thinking=false（旧版实测：17s → ~1.1s）

★ 与旧版的关键差异：旧的 RRF / MMR / multi_query 全部删除（检索与融合
  已经归 memory-bridge + forage/recall.py），这里只保留「生成改写词」。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import ssl
import time
import urllib.request
from pathlib import Path

# ⚠️ 沿用旧版的 TLS 设置，兼容自签/链不全的 OpenAI 兼容端点。
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = os.environ.get("FORAGE_REWRITE_CACHE") or str(
    _PROJECT_ROOT / ".cache" / "rewrite.sqlite"
)


def _env(*names, default=None):
    """按顺序读第一个有值的环境变量（兼顾旧前缀 FORAGE_ / CSKB_）。"""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _default_secrets() -> str:
    """找一个凭据文件。环境变量优先，其次几个常见位置。

    ★ 不在代码里写死任何人的家目录 —— 找不到就返回中性默认，
      调用方据此降级成"只跑原查询"，检索本身不受影响。
    ★ 额外加了 WSL 下 Windows 侧的挂载路径（旧版只在 Windows 原生跑，
      没这个需求）；仍然用 glob，不写死用户名。
    """
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    candidates = [
        _env("FORAGE_SECRETS", "CSKB_SECRETS", default=""),
        os.path.expanduser("~/.config/forage/secrets.env"),
        os.path.expanduser("~/.forage/secrets.env"),
        os.path.join(local, "hermes", "secrets", "siliconflow.env"),
    ]
    # WSL 里 Windows 侧的凭据目录挂载在 /mnt/c；只在它确实存在时才找。
    if os.path.isdir("/mnt/c/Users"):
        candidates += sorted(
            glob.glob("/mnt/c/Users/*/AppData/Local/hermes/secrets/siliconflow.env")
        )
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return candidates[1]


SECRETS = _default_secrets()

SYS_PROMPT = (
    "你是技术检索的查询改写器。把用户的中文技术查询改写成【英文检索关键词】。"
    "要求：\n"
    "1. 输出 3-5 个英文关键词组，用逗号分隔，每组 1-4 个词\n"
    "2. 保留技术专有名词（如 PostgreSQL、Kafka、Kubernetes）\n"
    "3. 只输出关键词，不要编号、不要解释、不要引号\n"
    "4. 直接用英文回答，不要思考过程"
)

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rewrite_cache (
    qhash      TEXT PRIMARY KEY,
    query      TEXT NOT NULL,
    variants   TEXT NOT NULL,     -- JSON list[str]
    model      TEXT,
    at         TEXT NOT NULL
);
"""


def _load_env(path: str = SECRETS) -> dict:
    env: dict = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip()
    except Exception:
        pass
    return env


class Rewriter:
    """中文 → 英文查询改写（SQLite 缓存 + fail-open）。"""

    def __init__(self, cache_path: str | None = None, enabled: bool = True,
                 timeout: int = 45, verbose: bool = False):
        self.enabled = enabled
        self.timeout = timeout
        self.verbose = verbose
        self.env = _load_env()
        # ★ 环境变量优先，其次 secrets 文件里的键 —— 两条路都通，
        #   免得文档写了一套名字、代码读另一套（那等于文档里的配置项不存在）。
        self.base = (
            _env("FORAGE_REWRITE_BASE_URL", "SILICONFLOW_BASE_URL")
            or self.env.get("FORAGE_REWRITE_BASE_URL")
            or self.env.get("SILICONFLOW_BASE_URL")
            or ""
        ).rstrip("/")
        self.key = (
            _env("FORAGE_REWRITE_API_KEY", "SILICONFLOW_API_KEY")
            or self.env.get("FORAGE_REWRITE_API_KEY")
            or self.env.get("SILICONFLOW_API_KEY")
            or ""
        )
        self.model = (
            _env("FORAGE_REWRITE_MODEL", "SILICONFLOW_MODEL")
            or self.env.get("FORAGE_REWRITE_MODEL")
            or self.env.get("SILICONFLOW_MODEL")
            or "Qwen/Qwen3-8B"
        )
        self.n_calls = 0
        self.n_cache = 0
        self._cache_path = cache_path or DEFAULT_CACHE
        self._conn: sqlite3.Connection | None = None
        if self._cache_path:
            self._open_cache()
        if not (self.base and self.key):
            self.enabled = False

    # ---------------- 缓存 ----------------
    def _open_cache(self) -> None:
        try:
            if self._cache_path != ":memory:":
                Path(self._cache_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._cache_path, timeout=5)
            self._conn.executescript(CACHE_SCHEMA)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - 缓存坏了不该影响检索
            self._conn = None
            if self.verbose:
                print(f"  [rewrite] 缓存不可用，本次不缓存：{exc}")

    def _h(self, q: str) -> str:
        """缓存键 = query + base_url + model。

        ★ 必须把 base/model 算进键：否则同一 query 在 A 后端上的改写会被
          B 后端（甚至已挂掉的后端）直接复用，fail-open 永远测不到，
          换模型后也会读到旧模型的词。
        """
        key = f"{q.strip().lower()}\x00{self.base}\x00{self.model}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def _cache_get(self, q: str):
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT variants FROM rewrite_cache WHERE qhash=?", (self._h(q),)
            ).fetchone()
            return json.loads(row[0]) if row else None
        except Exception:
            return None

    def _cache_put(self, q: str, variants: list) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO rewrite_cache VALUES (?,?,?,?,?)",
                (
                    self._h(q),
                    q,
                    json.dumps(variants, ensure_ascii=False),
                    self.model,
                    time.strftime("%Y-%m-%dT%H:%M:%S"),
                ),
            )
            self._conn.commit()
        except Exception:
            pass

    # ---------------- LLM ----------------
    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 160,
        temperature: float = 0.0,
    ) -> str:
        """通用 OpenAI 兼容调用（评测集生成等复用；仍走同一后端与超时）。"""
        if not (self.base and self.key):
            raise RuntimeError("rewrite 后端未配置（缺 base_url / api_key）")
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "enable_thinking": False,  # ★ 关掉思考链，17s → 秒级
            }
        ).encode()
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout, context=CTX) as resp:
            payload = json.loads(resp.read().decode())
        self.n_calls += 1
        return payload["choices"][0]["message"]["content"]

    def _llm(self, q: str) -> str:
        return self.chat(SYS_PROMPT, q, max_tokens=160, temperature=0)

    # ---------------- 对外 ----------------
    @staticmethod
    def _needs_rewrite(q: str) -> bool:
        """只在查询含中文时才改写（英文查询本身就能命中英文源）。"""
        return bool(re.search(r"[\u4e00-\u9fff]", q or ""))

    def variants(self, query: str) -> list:
        """返回英文改写候选（不含原 query）。失败/不需要时返回 []。"""
        q = (query or "").strip()
        if not self.enabled or not q or not self._needs_rewrite(q):
            return []

        cached = self._cache_get(q)
        if cached is not None:
            self.n_cache += 1
            return cached

        try:
            raw = self._llm(q)
        except Exception as exc:  # noqa: BLE001 - fail-open 是硬约定
            if self.verbose:
                print("  [rewrite] LLM 失败，降级只用原 query:", str(exc)[:80])
            return []

        parts = [p.strip(" -•\t\"'") for p in re.split(r"[,，\n;；]", raw) if p.strip()]
        out: list = []
        for p in parts:
            if (
                2 <= len(p) <= 60
                and not re.search(r"[\u4e00-\u9fff]", p)
                and p.lower() not in ("thinking",)
            ):
                out.append(p)
        out = out[:5]
        if out:
            self._cache_put(q, out)
        return out

    # 旧版调用名，保留兼容
    rewrite = variants
