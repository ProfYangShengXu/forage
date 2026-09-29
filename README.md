# forage

技术文档爬虫 + 中文改写召回。**检索层完全复用 memory-bridge**（BM25/jieba +
bge 向量 + RRF + cross-encoder 精排 + 父子块 + pgvector），forage 只做新增的事：

1. **爬虫**：官方 feed/API → 文章正文 → `knowledge.ingest()`（**增量**）
2. **改写召回**：中文 query → 多路英文 → 加权 RRF → 返回父块
3. **可解释 + 可评测**：`explain` 回答「为什么排在这 / 为什么没进来」；`eval` 在冻结标注集上量 Recall/MRR/NDCG

旧的 SQLite FTS5 + trigram 自建检索层不在这里（它在 Windows 侧旧版，保持不动）。

## 依赖与环境

- 解释器**必须**用 memory-bridge 的 venv（memory-bridge 不是 pip 包）：
  `/root/code/memory-bridge/.venv/bin/python`
- 不新增任何依赖：只用标准库 + `memory_bridge.*` + 已装的 psycopg/sqlalchemy（间接）

```bash
cd /root/code/forage
/root/code/memory-bridge/.venv/bin/python -m forage sources
```

## 用法

```bash
# 抓取入库（collection = "techblog"；内容 hash 未变则跳过 embedding；--force 强制重算）
python -m forage crawl --source pingcap --limit 5
python -m forage crawl --source all --limit 20

# 改写召回（-k 条；--no-rewrite 只跑原 query 做对照）
python -m forage search "数据库选型" -k 5
python -m forage search "数据库选型" --no-rewrite -k 5
# 现场调参：显式各路权重 / 改写预算 / 原路阻尼
python -m forage search "数据库选型" -k 5 --weights 1.0,0.15,0.15,0.15,0.15 --dampen 1.0

# 排查「为什么排在这 / 为什么它没进来」（--doc 给 source_doc 子串）
python -m forage explain "数据库事务与分析一起处理的技术" -k 5 --doc apparently-nothing-happened

# 评测（标注集已冻结在 forage/eval_set.json；--build 才调 LLM）
python -m forage eval -k 10
python -m forage eval --build

# 列源 / 查库
python -m forage sources
python -m forage stats
```

## 目录

```
forage/
  bridge.py    memory-bridge 唯一接入点（sys.path / .env / build_pipeline / chunker）
  crawl.py     feed → robots → 正文 → knowledge.ingest（含内容 hash 增量清单）
  sources.py   39 个源定义（从旧版搬）
  fetch.py     feed 解析 + HTML 提正文（保留 markdown 标题、隔离导航/侧栏/评论）+ robots + 限速
  rewrite.py   中文 → 英文关键词（SQLite 缓存，键含 base+model；fail-open）
  recall.py    ★ 多路召回 + 加权 RRF（0-based rank）+ 原路阻尼 + provenance
  explain.py   ★ 每条结果的路/名次/原始 ANN 分；目标文档漏在哪一路
  eval.py      冻结标注集上的 Recall/MRR/NDCG + 原 query 保留率
  eval_set.json  冻结的标注 query 集（17 条，GT 是 source_doc）
  cli.py       crawl / search / explain / eval / sources / stats
tests/         RRF/权重/阻尼、标题保留、噪声隔离、增量清单、改写 fail-open
```

## RRF 口径（与 memory-bridge 对齐）

```
score(d) = Σ  w_i / (60 + rank_i(d))     rank 从 0 开始
w0（原 query） = 1.0
改写路：合计预算 0.60 按路均分（4 路 → 每路 0.15），最多 4 路
原路阻尼：原 query 已召回到的文档，改写加分 ×0.35；原路没召回到的拿满分
```

- ★ 0-based 是与 memory-bridge 的 `rrf_fuse` 对齐的**刻意选择**：原始论文
  （Cormack 2009）与 pgvector 示例是 1-based，差一个常数。单测钉住了。
- ★ 预算 + 阻尼是为了修掉「4 路改写合力把原 query 独有命中挤出 top5」：
  在 17 条冻结评测集上，原 query 独有 top5 保留率 **0.381 → 0.762**，
  整体 top5 保留率 **0.647 → 0.859**（MRR 0.623→0.686，NDCG 0.658→0.705）。
  用 `--budget` / `--weights` / `--dampen` 可现场换挡（调大预算换召回，见下）。

## 失败域

| 域 | 行为 |
|---|---|
| 网络（5xx/429/超时/DNS） | 重试 2 次 + 退避；仍失败记 fail，继续下一个源 |
| 4xx（404 等） | 不重试，立即 fail |
| robots 拒绝 | 记 skip + 原因；**绝不退回 feed 摘要** |
| robots.txt 5xx / 不可达 | 按 RFC 9309 视为**完全禁止** → skip |
| 正文 < 600 字 | 记 skip + 实际字数（摘要不是正文） |
| 内容 hash 未变 | 跳过 embedding（增量清单 `.cache/crawl_manifest.json`） |
| PG 不可用 | 启动 ping + 写入报错 → **非 0 退出**，不跑成「全失败但退出码 0」 |
| 改写后端不可用 | fail-open：只跑原 query，检索绝不失败 |
| 模型缺失 | 启动警告，继续（memory-bridge 退到 HashEmbedder/NoopReranker） |

## 已知局限

- 加法式 RRF 无法 100% 保住原路顺序（要硬保证只能上分层配额，那会让补召回回退）。
- 默认口径的收益在排序（MRR/NDCG），不在 Recall@10；想要补召回用
  `--budget 1.4`（评测集 Recall 0.765→0.824，但 top5 保留率下降）。

## 测试

```bash
cd /root/code/forage
/root/code/memory-bridge/.venv/bin/python -m unittest discover -s tests -v
```
