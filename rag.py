"""
rag.py —— 知识库检索（RAG 里的那个 R）

【这个文件干什么】
把 docs/ 和 knowledge/ 里的 markdown 文档，切成一小块一小块（chunk），
建成一份"关键词倒排索引"，然后支持用一句话搜出最相关的几块。

    python rag.py build              建索引
    python rag.py search 空槽机制     命令行试搜
    python rag.py stats              看索引里有什么

【为什么第一版是关键词检索，而不是"向量检索"】
向量检索需要 embedding 模型把文字变成一串数字。但有两个卡点：

  1. DeepSeek 官方**不提供** embedding 接口（没有 /embeddings 端点），
     你现在这个 Key 只能聊天，变不出向量；
  2. 本地跑 embedding 模型要装 numpy / torch，会破坏"零第三方依赖"。

所以第一版用 BM25 —— 一种 1994 年就有的经典关键词打分算法，
纯 Python 三十行能写完，中文也能用。它有个硬伤：不懂"同义词"，
搜"工作区"找不到写着"沙箱"的段落。但作为第一版它足够能跑，
而且以后加上向量检索就自动变成"混合检索"（面试必问的那套）。

【BM25 是怎么打分的】
想象你在图书馆找书：
  - 一个词在**越少**的书里出现，它越"值钱"（专业术语比"的"值钱）
    → 这叫 IDF，逆文档频率
  - 一个词在某本书里出现**越多次**，这本书越相关
    → 这叫 TF，词频
  - 但长书天然词多，要给个"长度惩罚"，否则长文档永远赢
    → 这就是公式里 b 参数干的事

公式（背不下来没关系，知道这三件事就行）：

    score = Σ  IDF(词) × (词频 × (k1+1)) / (词频 + k1 × (1 - b + b × 文档长度/平均长度))

k1 控制"词频饱和"（出现 10 次不比出现 5 次强一倍），b 控制长度惩罚的力度。
业界默认 k1=1.5、b=0.75，这里沿用。

【中文怎么切词】
中文没有空格，切词要分词器（jieba 之类），但那是第三方库。
这里用了个经典土办法：**二元组（bigram）**——把"知识库"切成"知识"+"识库"。
好处：零依赖、不需要词典、新词也能命中。
坏处：索引会变大（大约每个字产生一个词），且会有一些无意义的组合。
Lucene 的中文分析器就是这么干的，够用。

【为什么不走 safe_path】
项目里 read_file 这些工具被"工作区"锁着（默认是 sandbox/xxx）。
但知识库是**项目级共享的只读资源**，不该被某个会话的沙箱挡住。
所以这里自己按项目根目录拼路径 —— 只读，不写用户文件，边界是安全的。
（这也是个值得记住的设计点：不同能力的信任边界可以不一样。）
"""

import json
import math
import re
from pathlib import Path

import config

# 项目根目录（rag.py 所在目录）
ROOT = Path(__file__).resolve().parent

# 索引放在 knowledge/.index/ 下（这个目录要加进 .gitignore）
INDEX_DIR = ROOT / "knowledge" / ".index"
INDEX_FILE = INDEX_DIR / "index.json"

# ---- 从 config 读参数，读不到就用默认值（这样少一个配置也能跑）----
MAX_CHUNK = getattr(config, "KB_CHUNK_MAX", 800)
OVERLAP = getattr(config, "KB_CHUNK_OVERLAP", 120)
DEFAULT_TOP_K = getattr(config, "KB_TOP_K", 5)

# 标题里的词算几遍（1 = 和正文一样，3 = 权重三倍）。
# 调大 → 更相信"章节标题"，调小 → 更相信"正文内容反复出现"。
TITLE_WEIGHT = getattr(config, "KB_TITLE_WEIGHT", 3)

# ---- BM25 的两个魔法数字（业界默认，一般不用改）----
K1 = 1.5
B = 0.75

# 标题行：## 二级标题、### 三级标题……
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# 连续的中文（用来切二元组）—— [\u4e00-\u9fff] 是常用汉字的编码范围
CJK_RUN = re.compile("[\u4e00-\u9fff]+")

# 英文单词 / 数字 / 下划线（代码里的函数名、变量名靠它）
WORD_RE = re.compile(r"[a-z0-9_]+")

# 索引文件支持的后缀
SUFFIXES = {".md", ".txt"}

# 一批"没有信息量"的汉字。由它们两两拼出来的词基本是虚词
# （什么、怎么、时候、这个、我们……），留着只会制造噪声。
# 规则：一个二元组里**两个字都**是虚词，才丢掉。
# 注意别把有实义的字塞进来，比如"内部"的"部"——丢了"部"就会误伤"内部"。
WEAK_CHARS = set(
    "的了是在和或也都就而及等这那有无不为以于对从到被把让使会能可要需应该"
    "你我他它们个些什么怎候呢吧啊之其每各上下前后中内外里时"
)


def _is_weak(tok):
    """这个词是不是"纯虚词"（该被扔掉）。"""
    if len(tok) == 1:
        return tok in WEAK_CHARS
    return all(ch in WEAK_CHARS for ch in tok)


# ============================================================
# 第一部分：切词
# ============================================================

def tokenize(text):
    """把一段文字切成"检索用的词"。

    规则：
      - 英文/数字：按单词切（toolbox、api、2026 各算一个词）
      - 中文：切成相邻两字的组合（知识库 → 知识、识库）
      - 单个汉字（孤立出现的）也算一个词

    注意：建索引和搜索必须用**同一个**切词函数，否则对不上。
    """
    text = text.lower()
    tokens = WORD_RE.findall(text)

    for run in CJK_RUN.findall(text):
        if len(run) == 1:
            if not _is_weak(run):
                tokens.append(run)
        else:
            for i in range(len(run) - 1):
                gram = run[i:i + 2]
                # 跳过"什么""时候"这种纯虚词组合
                if not _is_weak(gram):
                    tokens.append(gram)

    return tokens


# ============================================================
# 第二部分：切分文档（面试里的 "chunking"）
# ============================================================

def _split_sections(text):
    """按 markdown 标题把文件切成若干"节"。

    返回 [(标题路径, [(行号, 行内容), ...]), ...]

    标题路径长这样："核心概念 > 空槽机制"
    —— 为什么要记它？因为一个片段脱离上下文就没意义了。
       比如一段话写着"它用 append 注册"，你得知道"它"指的是"空槽机制"。
       把标题一起带上，检索命中和模型理解都会好很多。
       （这一招在业界叫 "contextual retrieval"，Anthropic 有篇研究专门讲）
    """
    lines = text.splitlines()
    sections = []
    stack = []      # [(层级, 标题文字)]，比如 [(2,"核心概念"), (3,"空槽机制")]
    buf = []        # 当前这一节攒的正文

    def flush():
        if buf:
            title = " > ".join(t for _, t in stack)
            sections.append((title, list(buf)))
            buf.clear()

    for line_no, line in enumerate(lines, 1):
        m = HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            # 遇到同级或更高级的标题，把栈里更深的都弹掉
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
        else:
            buf.append((line_no, line))

    flush()
    return sections


def _split_paragraphs(lines):
    """把一节内容按空行切成段落。

    有一个特殊处理：**代码块里的空行不算分段**。
    否则一个 mermaid 图或者一段 Python 会被切成好几块，语义就碎了。
    （这就是"结构感知切分"的最小例子：先看结构，再决定在哪切。）
    """
    paras = []
    cur = []
    cur_start = None
    in_code = False

    for line_no, line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code

        is_blank = (line.strip() == "")

        if is_blank and not in_code:
            if cur:
                paras.append((cur_start, "\n".join(cur)))
                cur = []
                cur_start = None
        else:
            if cur_start is None:
                cur_start = line_no
            cur.append(line)

    if cur:
        paras.append((cur_start, "\n".join(cur)))

    return paras


def _split_long(text, start_line):
    """单个段落如果超过上限，硬切（带重叠）。

    重叠（overlap）的作用：防止一句话正好被切在边界上，两边都不完整。
    代价是索引变大、可能重复召回。一般取 chunk 大小的 10%~20%。
    """
    if len(text) <= MAX_CHUNK:
        return [(start_line, text)]

    step = max(1, MAX_CHUNK - OVERLAP)
    return [(start_line, text[i:i + MAX_CHUNK])
            for i in range(0, len(text), step)]


def _pack(title_path, lines, source):
    """把一节的段落"打包"成若干个 chunk，每块不超过 MAX_CHUNK 字符。"""
    chunks = []
    buf = []        # 当前块攒的段落 [(行号, 正文)]
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return

        body = "\n\n".join(t for _, t in buf)
        chunks.append({
            "source": source,
            "heading": title_path or "(正文)",
            "line": buf[0][0],
            "text": body,
        })

        # 留最后一段作为下一块的开头（重叠）
        if OVERLAP > 0 and len(buf) > 1 and len(buf[-1][1]) <= OVERLAP:
            buf = [buf[-1]]
            buf_len = len(buf[0][1])
        else:
            buf = []
            buf_len = 0

    for line_no, para in _split_paragraphs(lines):
        for piece_line, piece in _split_long(para, line_no):
            if buf_len + len(piece) > MAX_CHUNK and buf:
                flush()
            buf.append((piece_line, piece))
            buf_len += len(piece)

    flush()
    return chunks


def split_file(text, source):
    """一个文件 → 一堆 chunk。"""
    result = []
    for title_path, lines in _split_sections(text):
        result.extend(_pack(title_path, lines, source))
    return result


# ============================================================
# 第三部分：建索引
# ============================================================

def _collect_files():
    """找出所有要进知识库的文件。"""
    sources = getattr(config, "KNOWLEDGE_SOURCES", ["docs", "knowledge"])
    files = []

    for src in sources:
        base = ROOT / src
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SUFFIXES:
                continue
            # 跳过索引目录自己
            if INDEX_DIR in p.parents:
                continue
            files.append(p)

    return files


def build(verbose=True):
    """扫一遍所有源文件，重新建索引。

    第一版是"全量重建"：每次把所有文件重新切一遍。
    7.8 万字大概几十毫秒，够用。
    等文档涨到几百万字，才需要做"增量"（只处理改动过的文件）——
    那时靠 index 里存的 files 签名（修改时间+大小）来判断。
    """
    files = _collect_files()

    chunks = []
    signatures = {}

    for path in files:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            if verbose:
                print(f"  跳过（读不了）：{rel}")
            continue

        st = path.stat()
        signatures[rel] = [st.st_mtime, st.st_size]

        file_chunks = split_file(text, rel)
        chunks.extend(file_chunks)

        if verbose:
            print(f"  {rel}  →  {len(file_chunks)} 块")

    # ---- 建倒排索引：词 → 出现在哪些块里、各出现几次 ----
    postings = {}
    doc_len = []

    for cid, ch in enumerate(chunks):
        body_toks = tokenize(ch["text"])
        head_toks = tokenize(ch["heading"])

        # ★ 标题里的词算 TITLE_WEIGHT 遍。标题是整块内容的概括，
        #   命中标题通常比命中正文更准。这是 BM25F（分字段加权）的土办法：
        #   不真的分区，只把标题词的次数翻倍。想试效果就改 config.KB_TITLE_WEIGHT。
        toks = body_toks + head_toks * TITLE_WEIGHT

        # 长度按真实词数算（不加权），否则标题写得长的块会被莫名惩罚
        doc_len.append(len(body_toks) + len(head_toks))

        for t in toks:
            bucket = postings.setdefault(t, {})
            bucket[cid] = bucket.get(cid, 0) + 1

    # dict 转成 list，方便存 json（也更省空间）
    postings = {t: [[cid, tf] for cid, tf in d.items()]
                for t, d in postings.items()}

    avgdl = (sum(doc_len) / len(doc_len)) if doc_len else 0.0

    index = {
        "version": 1,
        "chunks": chunks,
        "postings": postings,
        "doc_len": doc_len,
        "avgdl": avgdl,
        "files": signatures,
    }

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False),
        encoding="utf-8",
    )

    if verbose:
        size_mb = INDEX_FILE.stat().st_size / 1024 / 1024
        print(f"\n完成：{len(files)} 个文件 → {len(chunks)} 块，"
              f"{len(postings)} 个词条，索引 {size_mb:.1f} MB")
        print(f"位置：{INDEX_FILE.relative_to(ROOT)}")

    return index


# ============================================================
# 第四部分：搜索
# ============================================================

_cache = {"index": None, "mtime": None}


def load_index():
    """读索引（带内存缓存，同一个进程里只读一次）。"""
    if not INDEX_FILE.exists():
        return None

    mtime = INDEX_FILE.stat().st_mtime
    if _cache["index"] is not None and _cache["mtime"] == mtime:
        return _cache["index"]

    index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    _cache["index"] = index
    _cache["mtime"] = mtime
    return index


def search(query, top_k=None):
    """检索：给一句话，返回最相关的几块。

    返回 [{"source":..., "heading":..., "line":..., "text":..., "score":...}, ...]
    索引不存在时返回 None（调用方负责提示"请先建索引"）。
    """
    index = load_index()
    if index is None:
        return None

    top_k = top_k or DEFAULT_TOP_K
    chunks = index["chunks"]
    postings = index["postings"]
    doc_len = index["doc_len"]
    avgdl = index["avgdl"] or 1.0
    n_docs = len(chunks)

    # 查询词去重：同一个词重复出现不该重复加分
    q_terms = set(tokenize(query))

    scores = {}
    for term in q_terms:
        plist = postings.get(term)
        if not plist:
            continue

        df = len(plist)
        # IDF：越罕见的词越值钱。+0.5 那些是为了避免除零和极端值
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))

        for cid, tf in plist:
            dl = doc_len[cid]
            denom = tf + K1 * (1 - B + B * dl / avgdl)
            scores[cid] = scores.get(cid, 0.0) + idf * tf * (K1 + 1) / denom

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    results = []
    for cid, score in ranked:
        ch = chunks[cid]
        results.append({
            "source": ch["source"],
            "heading": ch["heading"],
            "line": ch["line"],
            "text": ch["text"],
            "score": round(score, 3),
        })

    return results


def stats():
    """看一眼索引的体检报告。"""
    index = load_index()
    if index is None:
        return None

    chunks = index["chunks"]
    by_source = {}
    for ch in chunks:
        by_source[ch["source"]] = by_source.get(ch["source"], 0) + 1

    return {
        "files": len(index["files"]),
        "chunks": len(chunks),
        "terms": len(index["postings"]),
        "avg_len": round(index["avgdl"], 1),
        "by_source": by_source,
    }


# ============================================================
# 命令行入口：python rag.py build / search 关键词 / stats
# ============================================================

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "build":
        print("正在扫描文档……\n")
        build()

    elif cmd == "search":
        query = " ".join(sys.argv[2:])
        if not query:
            print("用法：python rag.py search 空槽机制")
            sys.exit(1)

        hits = search(query)
        if hits is None:
            print("还没有索引，先运行：python rag.py build")
            sys.exit(1)
        if not hits:
            print(f"没有命中「{query}」")
            sys.exit(0)

        print(f"搜「{query}」，命中 {len(hits)} 块：\n")
        for i, h in enumerate(hits, 1):
            print(f"[{i}] {h['source']} · {h['heading']}")
            print(f"    第 {h['line']} 行起，相关度 {h['score']}")
            preview = h["text"][:150].replace("\n", " ")
            print(f"    {preview}…\n")

    elif cmd == "stats":
        info = stats()
        if info is None:
            print("还没有索引，先运行：python rag.py build")
            sys.exit(1)
        print(f"文件 {info['files']} 个，切出 {info['chunks']} 块，"
              f"词条 {info['terms']} 个，平均每块 {info['avg_len']} 个词")
        print("\n各来源的块数：")
        for src, n in sorted(info["by_source"].items(), key=lambda x: -x[1]):
            print(f"  {n:>4}  {src}")

    else:
        print("用法：python rag.py build | search <关键词> | stats")
