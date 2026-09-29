"""forage · 查询改写层（AIE3672 tut4 A1 的落地）

口径（来自 A1 节点）：
  ★ 改写是「加一路」不是「换掉」 —— 原 query 必留（技术博客里 useEffect /
    pg_advisory_lock / v18.2 全是精确串，改写会倾向泛化把它们抹掉）
  ★ 判据：用文档自己的措辞搜排得进来吗？排不进来 = 词汇鸿沟 → 改写
     本库实测：20 个源里 15 个英文，中文查「数据库选型」0 命中、
     英文查 "database selection" 全中 → 典型词汇鸿沟
  ★ fail-open：改写后端挂了就只跑原 query，绝不让检索失败（PowerContext 口径）

设计：LLM 改写的成本/延迟很高（实测 17s），所以
  ① 结果落 SQLite 缓存，同一个 query 只调一次
  ② enable_thinking=false 关掉思考链
  ③ 失败静默降级
"""
import os, json, ssl, time, hashlib, urllib.request, re

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

def _default_secrets():
    """默认去 Hermes 的 secrets 目录找（作者本机约定）；陌生机器用 CSKB_SECRETS 覆盖。"""
    import os as _os
    base = _os.environ.get("LOCALAPPDATA") or _os.path.expanduser("~")
    return _os.path.join(base, "hermes", "secrets", "siliconflow.env")


# ★ 不在代码里写死任何人的家目录 —— 环境变量优先，找不到就退化成"无改写真跑"。
SECRETS = os.environ.get("CSKB_SECRETS") or _default_secrets()

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


def _load_env(path=SECRETS):
    env = {}
    try:
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


class Rewriter:
    """中文 → 英文查询改写（带 SQLite 缓存 + fail-open）"""

    def __init__(self, conn=None, enabled=True, timeout=45, verbose=False):
        self.conn = conn
        self.enabled = enabled
        self.timeout = timeout
        self.verbose = verbose
        self.env = _load_env()
        self.base = (self.env.get("SILICONFLOW_BASE_URL") or "").rstrip("/")
        self.key = self.env.get("SILICONFLOW_API_KEY") or ""
        self.model = self.env.get("SILICONFLOW_MODEL") or "Qwen/Qwen3-8B"
        self.n_calls = 0
        self.n_cache = 0
        if conn is not None:
            conn.executescript(CACHE_SCHEMA)
            conn.commit()
        if not (self.base and self.key):
            self.enabled = False

    # ---------------- 缓存 ----------------
    @staticmethod
    def _h(q):
        return hashlib.sha1(q.strip().lower().encode("utf-8")).hexdigest()

    def _cache_get(self, q):
        if self.conn is None:
            return None
        r = self.conn.execute("SELECT variants FROM rewrite_cache WHERE qhash=?", (self._h(q),)).fetchone()
        if not r:
            return None
        try:
            return json.loads(r[0] if isinstance(r, str) else r["variants"])
        except Exception:
            return None

    def _cache_put(self, q, variants):
        if self.conn is None:
            return
        self.conn.execute("INSERT OR REPLACE INTO rewrite_cache VALUES (?,?,?,?,?)",
                          (self._h(q), q, json.dumps(variants, ensure_ascii=False),
                           self.model, time.strftime("%Y-%m-%dT%H:%M:%S")))
        self.conn.commit()

    # ---------------- LLM ----------------
    def _llm(self, q):
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": SYS_PROMPT},
                         {"role": "user", "content": q}],
            "max_tokens": 160,
            "temperature": 0,
            "enable_thinking": False,      # ★ 关掉思考链，17s → 秒级
        }).encode()
        req = urllib.request.Request(
            self.base + "/v1/chat/completions", data=body,
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout, context=CTX) as r:
            j = json.loads(r.read().decode())
        self.n_calls += 1
        return j["choices"][0]["message"]["content"]

    # ---------------- 对外 ----------------
    @staticmethod
    def _needs_rewrite(q: str) -> bool:
        """只在查询含中文时才改写（英文查询本身就能命中英文源）。"""
        return bool(re.search(r"[\u4e00-\u9fff]", q or ""))

    def rewrite(self, query: str) -> list:
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
        except Exception as e:
            if self.verbose:
                print("  [rewrite] LLM 失败，降级只用原 query:", str(e)[:80])
            return []

        parts = [p.strip(" -•\t\"'") for p in re.split(r"[,，\n;；]", raw) if p.strip()]
        out = []
        for p in parts:
            # 丢掉带解释性的长句（模型偶尔不听话）
            if 2 <= len(p) <= 60 and not re.search(r"[\u4e00-\u9fff]", p) and p.lower() not in ("thinking",):
                out.append(p)
        out = out[:5]
        if out:
            self._cache_put(q, out)
        return out


def rrf_fuse(lists, k=60, weights=None):
    """倒数排名融合（Reciprocal Rank Fusion）。

    ★ 不看分数只看名次 —— 不同路（中文 BM25 / 英文改写 BM25）的分数不可比。
    lists:    [ [row, row, ...], ... ]  每个 row 需含 'chunk_id'
    weights:  [w1, w2, ...] 与 lists 等长；★ 原查询路应给高权重
              （精确串匹配比改写路可信；改写会跑偏，给 0.5 左右）
    返回 [row, ...] 按加权 RRF 分数降序，row 里带 'rrf' 和 'n_routes'（被几路命中）
    """
    n = len(lists)
    if weights is None:
        weights = [1.0] * n
    score, seen, hits = {}, {}, {}
    for w, lst in zip(weights, lists):
        for rank, row in enumerate(lst, 1):
            cid = row["chunk_id"]
            score[cid] = score.get(cid, 0.0) + w * (1.0 / (k + rank))
            hits[cid] = hits.get(cid, 0) + 1
            if cid not in seen:
                seen[cid] = row
    out = sorted(seen.values(), key=lambda r: -score[r["chunk_id"]])
    for r in out:
        r["rrf"] = round(score[r["chunk_id"]], 6)
        r["n_routes"] = hits[r["chunk_id"]]
    return out


# ─────────────────────────────────────────────────────────────
#  MMR：最大边际相关（从推荐系统迁移，解"同一篇多 chunk 占满 top-K"）
# ─────────────────────────────────────────────────────────────

def _trigrams(s: str) -> set:
    """字符 3-gram 集合。中文无空格，字符级切分对中英都适用；不引第三方库。"""
    s = re.sub(r"\s+", "", (s or "").lower())
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _sim(a: str, b: str) -> float:
    """Jaccard 相似度（字符 trigram）。"""
    A, B = _trigrams(a), _trigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def mmr_rerank(rows, k=10, lam=0.75, sim_fn=None):
    """MMR 重排：score = λ·相关性 − (1−λ)·与已选项的最大相似度。

    ★ 迁移自推荐系统（Carbonell & Goldstein 1998），解的是同一个问题：
      "推荐十条几乎一样的" ≡ "top-5 里同一篇文章的多个 chunk"。
    ★ 相关性用 rrf 分（归一化到 0..1），没有 rrf 时按名次倒数。
    λ=1 退化成纯相关性排序；λ 越小越重视多样性。
    返回重排后的 rows（新增 'mmr' 字段）。
    """
    if not rows:
        return rows
    sim_fn = sim_fn or _sim
    rel = [r.get("rrf") or (1.0 / (60 + i + 1)) for i, r in enumerate(rows)]
    mx = max(rel) or 1.0
    rel = [x / mx for x in rel]

    idx = list(range(len(rows)))
    picked = []
    while idx and len(picked) < k:
        best, best_v = None, None
        for i in idx:
            if picked:
                # ★ 同源（source_root 相同）直接判满相似度 —— 这才是真凶
                red = max(sim_fn(rows[i]["text"], rows[j]["text"])
                          if rows[i]["source_root"] != rows[j]["source_root"] else 1.0
                          for j in picked)
            else:
                red = 0.0
            v = lam * rel[i] - (1 - lam) * red
            if best_v is None or v > best_v:
                best, best_v = i, v
        picked.append(best)
        idx.remove(best)

    out = []
    for i in picked:
        r = dict(rows[i])
        r["mmr"] = round(rel[i], 4)
        out.append(r)
    return out


# ─────────────────────────────────────────────────────────────
#  multi-query：决策型查询的多视角拆解（A1 的另一半）
# ─────────────────────────────────────────────────────────────

MQ_PROMPT = (
    "下面是一个【技术选型/架构决策】类问题。请把它拆成 3-4 个【不同侧面】的英文检索查询，"
    "用来从技术博客里找到决策所需的证据。\n"
    "侧面要求（每个查询覆盖一个不同角度，不是同义改写）：\n"
    "  · 方案 A 的优势\n  · 方案 B 的代价/坑\n  · 实际迁移或落地经验\n  · 反例或失败案例\n"
    "要求：只输出英文查询，一行一个，不要编号、不要解释。\n"
    "问题：{q}"
)


def multi_query(conn, query: str, n: int = 3, verbose: bool = False) -> list:
    """决策型查询 → N 个不同侧面的子查询（英文）。失败返回 []。

    ★ 与 rewrite() 的区别：rewrite 是 1→1 换措辞（治词汇鸿沟）；
      multi_query 是 1→N 换视角（治覆盖不足）。A1 判据决定用哪个。
    """
    import urllib.request, ssl
    rw = Rewriter(conn, verbose=verbose)
    q = (query or "").strip()
    if not rw.enabled or not q:
        return []

    cached = rw._cache_get("MQ::" + q)
    if cached is not None:
        return cached

    try:
        body = json.dumps({
            "model": rw.model,
            "messages": [{"role": "system", "content": "你是技术检索的查询分解器，只输出查询列表。"},
                         {"role": "user", "content": MQ_PROMPT.format(q=q)}],
            "max_tokens": 200, "temperature": 0, "enable_thinking": False,
        }).encode()
        req = urllib.request.Request(rw.base + "/v1/chat/completions", data=body,
                                     headers={"Authorization": "Bearer " + rw.key,
                                              "Content-Type": "application/json"})
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=rw.timeout, context=ctx) as r:
            raw = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    except Exception as e:
        if verbose:
            print("  [multi_query] 失败:", str(e)[:70])
        return []

    out = []
    for ln in raw.split("\n"):
        ln = re.sub(r"^\s*[\d\-•*.)\]\s]+", "", ln).strip().strip('"\'')
        if 6 <= len(ln) <= 70 and not re.search(r"[\u4e00-\u9fff]", ln):
            out.append(ln)
    out = out[:n]
    if out:
        rw._cache_put("MQ::" + q, out)
    return out
