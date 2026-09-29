"""forage · 技术文档爬虫 + 改写召回

重做目标：检索层直接复用 memory-bridge（BM25/jieba + bge 向量 + RRF + 精排 +
父子块 + pgvector），forage 只保留两块真正新增的能力：

  ① 技术文档爬虫（feed/API → 正文 → knowledge.ingest）
  ② 改写召回（中文 query → 多路英文 → RRF 融合）

旧的 SQLite FTS5 + trigram 检索层不在这里 —— 它在 Windows 侧旧版里，保持不动。
"""

__version__ = "0.2.0"
