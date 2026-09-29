"""forage · 切块（两级切，口径沿用 lec11）

第一级：结构优先 —— 按标题切小节（节内主题一致）
第二级：超长再切 —— 小节超过 MAX 时按段落二次切，带 overlap
短节不合并（lec11 口径：合并会主题混杂）
"""
import re

MAX_CHARS = 1200    # 单块上限（中文约 600 token）
MIN_CHARS = 120     # 低于此值的块直接丢（噪声）
OVERLAP = 150       # 二次切的段落重叠


def _split_sections(text: str, fallback_title: str = "") -> list:
    """按 markdown 标题 / 全角标题行切小节。返回 [(heading, body), ...]"""
    lines = text.split("\n")
    secs, cur_h, buf = [], fallback_title, []
    head_re = re.compile(r"^\s{0,3}(#{1,6})\s+(.{2,120})$")

    for ln in lines:
        m = head_re.match(ln)
        if m:
            if buf:
                secs.append((cur_h, "\n".join(buf).strip()))
            cur_h, buf = m.group(2).strip(), []
        else:
            buf.append(ln)
    if buf:
        secs.append((cur_h, "\n".join(buf).strip()))
    if not secs:
        secs = [(fallback_title, text)]
    return secs


def _split_long(body: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list:
    """按段落贪心装箱；超长段落再按句切。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paras:
        return []
    out, buf, size = [], [], 0
    for p in paras:
        # 单段就超上限 → 先按句子硬切
        if len(p) > max_chars:
            if buf:
                out.append("\n\n".join(buf)); buf, size = [], 0
            sents = re.split(r"(?<=[。！？.!?；;])\s*", p)
            cb = ""
            for s in sents:
                if len(cb) + len(s) > max_chars and cb:
                    out.append(cb.strip()); cb = cb[-overlap:] + s if overlap else s
                else:
                    cb += s
            if cb.strip():
                out.append(cb.strip())
            continue
        if size + len(p) > max_chars and buf:
            out.append("\n\n".join(buf))
            # 带 overlap 重开
            tail = ""
            if overlap:
                acc = []
                for q in reversed(buf):
                    acc.insert(0, q)
                    if sum(len(x) for x in acc) >= overlap:
                        break
                tail = "\n\n".join(acc)
            buf = [tail, p] if tail else [p]
            size = sum(len(x) for x in buf)
        else:
            buf.append(p); size += len(p)
    if buf:
        out.append("\n\n".join(buf))
    return [c for c in out if c.strip()]


def chunk_article(text: str, title: str = "", max_chars: int = MAX_CHARS) -> list:
    """返回 [{"heading": str, "text": str}, ...]"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks = []
    for h, body in _split_sections(text, title):
        if len(body) <= max_chars:
            if len(body) >= MIN_CHARS:
                chunks.append({"heading": h, "text": body})
        else:
            for i, piece in enumerate(_split_long(body, max_chars)):
                if len(piece) >= MIN_CHARS:
                    chunks.append({"heading": "%s%s" % (h, "" if i == 0 else " (%d)" % (i + 1)),
                                   "text": piece})
    # 全文极短时至少留一块
    if not chunks and len(text) >= 60:
        chunks.append({"heading": title, "text": text[:max_chars]})
    return chunks
