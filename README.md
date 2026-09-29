# forage

> **forage** /ˈfɒrɪdʒ/ —— 觅食。动物四处找吃的，把能用的东西叼回来。
> 这个工具干的是同一件事：从你信任的技术博客里把有用的段落叼回来，喂给开发决策。

**一个零依赖的本地检索层：把技术博客抓下来切成块，让你用中文问、从英文源里答。**

```
Python 标准库实现 · SQLite FTS5 · 无需 GPU · 无需 embedding · 无需向量库
```

---

## 它解决什么（一个真实例子）

技术博客绝大部分是英文站。你在写代码时想问的是中文，就像这样：

```bash
$ python -m forage search "数据库选型" --no-rewrite
query: 数据库选型   (k=10, 命中 0, 0 路, 0.0s)
（无结果）
```

**库里明明有内容，但一条都搜不到。** 直接查数据库确认不是检索器坏了：

```
「数据库选型」  0 篇      「database」  125 篇
「索引优化」    0 篇      「index」      73 篇
「事务隔离」    0 篇      「isolation」  51 篇
```

同一批内容，中文查不到、英文全能 —— 这叫**词汇鸿沟**（vocabulary gap）。
不是内容缺失，是**你用的词和文档用的词对不上**。

forage 的处理方式不是"再爬点中文源"，而是**把中文查询也送进检索**：

```bash
$ python -m forage search "数据库选型"          # 默认开改写
[rewrite] 新增 4 路（缓存命中 1）
query: 数据库选型   (k=3, 命中 3, 2 路, 0.3s)
[1] (cmudb · 2026-05-06) Carnegie Mellon Database Group ...
[2] (cmudb · 2026-01-30) PostgreSQL vs. The World Seminar Series ...
[3] (dbweekly · 2021-06-18) The largest database company investment ever?
```

---

## 实测成绩

43 条评测集，**中文 query → 找英文原文**，k=10：

| 配置 | Recall@10 | MRR@10 | NDCG@10 | Div@10 | 耗时 |
|---|---|---|---|---|---|
| A 裸查（单路 BM25） | 0.372 | 0.263 | 0.288 | 0.395 | 0.6s |
| B + 查询改写 | **0.837** | **0.755** | **0.775** | 1.000 | 43.3s |
| C + 改写 + MMR | 0.837 | 0.757 | 0.777 | 1.000 | 29.5s |
| D + 改写 + MMR + multi-query | 0.837 | 0.757 | 0.777 | 1.000 | 32.1s |

```
Δ vs A：Recall +125.0%   MRR +187.5%   NDCG +169.3%

分类型        fact(31)     decision(12)
A 裸查        11/31        5/12
B +改写       24/31       12/12
```

**这个 +125% 就是词汇鸿沟的严重程度。** 评测集本身就是"中文 query 找英文原文"，
所以提升量直接等于鸿沟的量级。

### 三条诚实的解读

**① 改写是真功劳**，其余两个不是。

**② `Div@10` 0.395 → 1.000 是「同源去重」的功劳，不是 MMR 的。**
MMR 的额外贡献只有 `MRR +0.002`。一开始只测前三个指标，MMR 看起来毫无作用；
补上多样性指标才看清 —— **功劳是同源去重的，差点记错账**。
结论：同源去重已解决大部分冗余，**MMR 在当前规模下是锦上添花**。

**③ multi-query 零增益** —— decision 类判定太宽松（关键词命中即算过），已 12/12 满分，
没有提升空间。**功能实现了，但没被测出价值**，这里不硬说它有用。

### 评测口径本身的缺陷（已知，未修）

失败案例里有多条其实是"命中了**同主题的另一篇**"：

```
「Postgres 19 发布遇到什么问题？」
   应命中: Postgres 19's bumpy road to release
   实返回: PostgreSQL 19 Beta 4 Released!     ← 同主题，不该算完全失败
```

→ **单相关文档的评测低估了实际可用性。** 下一步该做主题级判定（但要防循环论证）。

---

## 快速开始

```bash
git clone https://github.com/ProfYangShengXu/forage.git
cd forage

python -m forage init                     # 建库
python -m forage sources                  # 看有哪些源（39 个，按类分组）
python -m forage add all --limit 30       # 抓取入库
python -m forage search "数据库选型"        # 检索
python -m forage stats                    # 统计
```

**装成命令**（可选，之后可以直接敲 `forage`）：

```bash
pip install -e .
forage search "数据库选型"
```

**注意**：仓库不含数据。`data/kb.sqlite` 里是抓来的第三方文章全文，**有版权，不随仓库分发** ——
clone 之后自己跑 `python -m forage add all` 建库（首次约 10 分钟，取决于源数量）。

### 常用参数

```bash
python -m forage search "数据库选型" --k 8            # 返回 8 条
python -m forage search "..." --json                 # JSON 输出，给程序调用
python -m forage search "..." --no-rewrite           # 关改写，看原查询自己命中几条（诊断用）
python -m forage search "..." --since 2025-01-01     # 只要这个日期之后的
python -m forage search "..." --source meituan --lang zh   # 限定源/语言
python -m forage search "..." --no-dedup             # 关闭同源去重，看原始排序
python -m forage search "..." --mmr                  # 多样性重排
python -m forage search "该用 A 还是 B" --multi-query  # 决策型查询多视角拆解
python -m forage add db                              # 只抓某一类（db / sys / ai）
```

### 查询改写是可选能力

**不配任何 API key 也能跑** —— 只走原查询 BM25，上面 A 档那行就是它的成绩。

配了改写后端之后，中文查询才能命中英文源。任何 OpenAI 兼容的 `/chat/completions` 都行：

```bash
cp .env.example .env      # 填 BASE_URL / API_KEY / MODEL
```

```
FORAGE_REWRITE_BASE_URL=https://api.siliconflow.cn
FORAGE_REWRITE_API_KEY=sk-xxxx
FORAGE_REWRITE_MODEL=Qwen/Qwen3-8B
```

> **fail-open 是硬约定**：改写后端挂了、超时了、返回垃圾，都只降级成"只跑原查询"，
> **绝不让检索本身失败**。检索是主链路，改写是增强。

---

## 为什么这样设计

几条**不显然的技术判断** —— 每条都对应一个具体的失败模式，不是风格偏好。

### 1. 中文查询不能按空格拆成 OR

`trigram` 分词器按 3 字符窗口切，中文没有空格。把「Agent 评测」按空格拆成 OR，
等于把**高频词和高 IDF 关键词平权**：

```
"Agent"      出现在 644/1710 块（38%）   → IDF 极低，匹配它几乎不加分
"评测"        出现在  39/1710 块（2.3%）  → IDF 高
"Agent评测"   出现在   8/1710 块（0.5%）  → IDF 最高

三种 MATCH 写法的实测区分度：
  '"Agent" OR "评测"'    → -0.68 / -0.68 / -0.68    ❌ 三篇并列，排序完全失效
  '"Agent 评测"'         → -8.14 / -7.99 / -7.32    ✅ 但只中带空格的写法
  '"Agent评测"'（去空格） → -10.06 / -8.73 / -8.29   ✅✅ 区分度差 15 倍
```

**做法**：中文先**去掉空格整体成短语**作为第一路精确查询，再用分词 OR 兜底。
**精确优先 + 放宽补齐，不是替换** —— 这条口径贯穿全项目（见第 2 条）。

### 2. 改写是「加一路」，不是「换掉」

技术博客里 `useEffect` / `pg_advisory_lock` / `v18.2` 全是**精确串**，
LLM 改写倾向泛化，会把这些抹掉。所以原 query **必留**，改写只作为新增一路：

```python
rrf_fuse([原查询结果, 改写路结果], weights=[1.0, 0.45])
```

**实测印证**（查「消息队列选型」）：原查询命中的那条**仍然是第 1 名**，
改写路只在后面补充 `Kafka 101` / `SQS`。如果当初是"用改写替换原查询"，
这条精确匹配就丢了。

### 3. 用 RRF 融合，因为不同路的分数不可比

中文 BM25 的分数和英文 BM25 的分数**不是一个量纲**，直接比大小没有意义。
RRF（Reciprocal Rank Fusion）**只看名次不看分数**：

```
score(d) = Σ  w_route / (k + rank_route(d))
```

原查询路权重高（1.0），改写路低（0.45）—— 精确串匹配比 LLM 改写可信。

### 4. URL 必须归一化，否则 MMR 是假的

```python
normalize_url("https://example.com/post?utm_source=rss&id=3")
normalize_url("https://example.com/post?id=3")
# → 必须相等
```

RSS 给的链接带 `?utm_source=rss`，站内链接不带。**不归一化 = 同一篇文章被当成两篇**，
于是：

- 「同源去重」永远打不中 → top-5 里照旧出现同一篇文章的两块
- 「同一篇文章的 3 个 chunk」被当成 3 路独立印证，可信度虚高

`source_root` 字段就是干这个的，它是 MMR 同源判定的地基。

---

## 架构

```
① 采集  sources.py   39 个源，全部走官方 feed/API（不解析列表页 HTML）
② 提正文 fetch.py    stdlib HTMLParser：去 script/style/nav，按块级标签断行
③ 切块  chunk.py     两级切：结构优先（按标题）→ 超长按段落二次切（带 overlap）
④ 存储  db.py        SQLite + FTS5(trigram)  自带 BM25，零外部依赖
⑤ 检索  search.py    BM25 + 元数据过滤（过滤写进 WHERE，保证「排序前过滤」）
⑥ 改写  rewrite.py   中文 query → 英文关键词，作为新增一路
⑦ 融合  rewrite.py   加权 RRF → （可选）MMR 多样性重排
⑧ 评测  eval.py      自动构造评测集 + Recall/MRR/NDCG/Div
```

### 为什么是 SQLite FTS5

- **自带 BM25 排序**（`ORDER BY bm25(...)`），不用 `rank_bm25`
- **零外部依赖** —— 纯标准库，单文件、可移植、可备份
- **trigram 分词器**：`unicode61` 对中文完全无效（实测查「索引」命中 0 条），
  trigram 按 3 字符窗口切，中英通吃

### Schema 里三个不是装饰的字段

| 字段 | 防的是什么 |
|---|---|
| `source_root` | URL 归一化后的文章标识。不做归一化 → `?utm_source=` 一变就当两篇 → MMR 同源判定失效 |
| `published_at` | 技术博客的毒药是老文章（2019 年的《React 最佳实践》讲 class component）。**时间衰减救不了** —— 老文章在它那个年代是对的，只能靠日期过滤 |
| `tech_version` | 同上但更精确：`react@16` vs `react@18`。回答"该用哪个 API"必须按项目当前版本过滤 |

三个字段都**同时挂在 `docs` 和 `chunks` 两级** —— 过滤要能在 chunk 级做，
否则"召回前过滤"退化成"召回后再筛"，版本/时效全部失效。

---

## 已支持的源（39 个，全部实测可用）

| 类 | 源 |
|---|---|
| **数据库**（12） | CMU DB Group · PostgreSQL 官方 · Postgres Weekly · DB Weekly · Brent Ozar · TDengine · Database Internals · PingCAP · AWS Database · ScyllaDB · SingleStore · pgAdmin |
| **系统/后端**（14） | AWS 架构 · Julia Evans · High Scalability · Stripe Eng · Meta Eng · Slack Eng · Fly.io · Kubernetes · martinfowler · Netflix · 云风 · 风雪之隅 · ARTHURCHIAO · Xargin |
| **中文**（13） | 极客兔兔 · 程序猿DD · 后端进阶 · 码志 · Jserv · Mohuishou · polarisxu(Go) · Luyu Huang · 后端技术杂谈 |
| **AI/Agent**（5） | 美团技术 · InfoQ中文 · 掘金 · dev.to · GitHub Blog |

### 被 robots.txt 拒绝的（已排除，别再加）

```
PostgreSQL Planet · Percona · Neo4j · Redis · DuckDB · Cloudflare
CoolShell · draveness(面向信仰编程) · piglei · Jimmy Song · Python猫
```

里面有不少公认的经典博客（CoolShell、draveness）—— **被拦是遗憾，但静默退化成摘要入库更糟**。

**硬加的后果**：代码会退回 feed 摘要，库里出现 200 字的"假文档"，
而且**日志显示 `ok=N fail=0`，看起来完全成功** —— 只有去查 `words` 字段才看得出来。

---

## 不做什么（明确边界）

- **不做向量检索 / embedding**。本机无 GPU 是起因，但更重要的是：
  先用 BM25 把链路跑通，**等评测显示"多跳类查询持续失败"再考虑加**。现在加是凭空上复杂度。
- **不做知识图谱**。每 chunk 一次 LLM 调用建图，10 万篇文章 = 10 万次调用。
  只在评测显示出多跳失败时才做，否则会建一张没人查的图。
- **不绕反爬、不解析列表页**。只走官方 feed/API + robots.txt 允许的文章页。
- **不分发抓来的内容**。仓库只有代码，数据自己爬（版权 + 体积两个原因）。
- **不提供问答 / 不做 RAG 生成**。它是**检索层**，只负责把相关段落找出来。
  生成交给上面调它的 agent —— 这也是为什么它是个 CLI + skill，而不是一个聊天界面。

---

## 常见故障表

| 症状 | 根因 | 处治 |
|---|---|---|
| 中文查询 0 命中，英文能中 | 词汇鸿沟（源以英文为主） | 配改写后端；或 `--no-rewrite` 确认它确实是鸿沟而非内容缺失 |
| 所有查询都 0 命中 | 库是空的（仓库不含数据） | `python -m forage add all` |
| 改完代码不生效 | `__pycache__` 里的旧 `.pyc` | 删掉 `__pycache__/`；入口已设 `sys.dont_write_bytecode`（见下） |
| 某源抓取 `ok` 数远小于条目数 | feed 的 `<link>` 全指向同一页面 | 看是不是 URL 重复 —— **`skip` 和 `fail` 是两回事** |
| 某源入库但内容很短 | robots 禁抓 → 退化成 feed 摘要 | 查 `avg(words)`，低于 800 就该弃用该源 |
| 换台机器跑不起来 | 写死了本机路径 | 用 `FORAGE_DB` / `FORAGE_SECRETS` 覆盖 |

**源健康度体检**（加源后必跑）：

```sql
SELECT source, count(*) n, avg(words) w
FROM docs GROUP BY source ORDER BY w ASC;
```

`avg(words) < 800` → 该源在退化成摘要，**别急着用，先查原因**。
这个判据一次抓出过 cloudflare（235 字）和 pgAdmin。

---

## 测试

```bash
pip install pytest
python -m pytest tests/ -q      # 37 passed
```

**为什么测这五个**：它们都是**纯函数**，而且各自封装着一条踩出来的教训 ——
测试在这里的作用不只是防回归，**是把"为什么这样写"钉成断言**：

| 文件 | 钉住的教训 |
|---|---|
| `test_fts_query.py` | 中文必须去空格整体成短语（否则区分度掉 15 倍） |
| `test_normalize.py` | `?utm_source=` 必须归并（否则 MMR 同源判定失效） |
| `test_rrf.py` | 被多路命中的排前；原查询路权重必须更高 |
| `test_mmr.py` | 同源强制判满相似度（不靠文本相似度判断） |
| `test_chunk.py` | 阈值标定：60 字兜底 / 1200 字二次切 |

---

## 给 agent 用

**这个项目的主要用户是 agent，所以带了 [`SKILL.md`](SKILL.md)。**
README 是给人的送达路径；agent 不看 README，它看 skill catalog。

```bash
# Hermes Agent
cp -r SKILL.md ~/AppData/Local/hermes/skills/research/forage/

# Claude Code
cp -r SKILL.md .claude/skills/forage/

# dsh (DeepSeek Harness) —— 注意 frontmatter 只认 name + description
mkdir -p ~/.dsh/skills/forage && cp SKILL.md ~/.dsh/skills/forage/
```

装上之后 agent 会在遇到**选型 / 架构取舍 / 依赖版本 / 踩坑**类问题时先查本地库。

---

## 一个反复踩到的坑：`__pycache__`

```
现象：往 sources.py 加了新源，运行时仍报 KeyError: '<新源 key>'
根因：Python 加载的是 __pycache__ 里的旧 .pyc
```

**修法**：入口 `forage/__main__.py` 里设了 `sys.dont_write_bytecode = True`（必须在任何
`forage.*` 导入之前）—— 代价只是稍慢一点，换来"改完就生效"。

---

## 设计出处

**注入策略参考 [memory-bridge](https://github.com/ProfYangShengXu/memory-bridge)** —— 它解决的是
「单个 agent 怎么才真的记住东西」（什么时候、把什么塞进 prompt），与本项目的检索链路正交，
但决定了三处设计：

| memory-bridge 的决策 | 在本项目的落地 |
|---|---|
| **常驻 > 检索** | `db.decisions` 表 —— 结论进常驻记忆，**完整版 + 证据引用留库**（记忆放得下结论，放不下证据） |
| **确定性触发 > 模型自主** | **[`SKILL.md`](SKILL.md) 里写死触发词** —— 不赌模型自己想起来查，这是 SKILL.md 存在的理由 |
| **可变内容只放 prompt 末尾** | SKILL.md 的「别做的事」—— 检索结果当尾部附件，不写进常驻记忆（否则每次都让缓存失效） |

**检索链路本身**不是凭空设计的，每一步都对应一个具体的失败模式：

- **查询改写 / 词汇鸿沟 / 「加一路不换掉」** —— Advanced RAG 的 query rewriting 一节
- **实体消解 / 关系收敛 / 图谱建图成本** —— GraphRAG / KG 构建；图谱的成本判断（每 chunk 一次 LLM 调用）直接来自这里
- **召回前过滤（权限/版本/时效）** —— 过滤必须发生在排序之前，否则等于没过滤
- **来源合规（只走官方 feed、robots 检查、拒绝静默退化）** —— 抓取层的基本纪律
- **RRF / MMR** —— RRF 出自 Cormack et al. 2009；MMR 出自 Carbonell & Goldstein 1998（原用于推荐系统，本仓复用的是同一个问题：**防止同源内容占满 top-K**）

---

## 许可证

代码 **MIT**（见 [`LICENSE`](LICENSE)）。

**抓取的内容不属于本仓库** —— `data/` 目录被 `.gitignore` 排除。
你自己爬回来的文章，版权归**原文作者**，请遵守各站 robots.txt 与使用条款，
不要用本工具重新分发他人内容。
