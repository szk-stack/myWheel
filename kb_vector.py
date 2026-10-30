"""
kb_vector.py —— 向量检索（RAG 的第二路召回）

【为什么需要它】
rag.py 的关键词检索有个硬伤：**不懂同义词**。
    搜「沙箱」  → 找不到写着「工作区」的段落
    搜「报错」  → 找不到写着「异常」的段落
    搜「拆分」  → 找不到写着「切分」的段落
向量检索能解决，因为 embedding 模型把意思相近的文字放到了相近的位置。

【怎么把文字变成向量】
调阿里云百炼的 text-embedding-v4：
    输入  ["空槽机制", "hooks.py 里那三个列表"]
    输出  [[0.012, -0.034, ...], [...]]     每组 1024 个浮点数

这 1024 个数字单看没有任何意义，但有三个性质（理解这三条就够了）：

  1. 意思相近 → 向量夹角小。所以"相似度"可以算出来。
  2. 可以做算术：vec(国王) - vec(男人) + vec(女人) ≈ vec(女王)
  3. 不可逆：拿到向量推不回原文（它是有损压缩，不是加密）

【相似度怎么算：余弦相似度】
两个向量的夹角越小越像。数学上就是：

    余弦相似度 = (A · B) / (|A| × |B|)

关键技巧：如果提前把每个向量"归一化"（长度缩成 1），分母就恒等于 1，
公式退化成**点积**：

    相似度 = A · B

而"一堆向量和一个查询向量分别做点积"就是一次矩阵乘法：

    scores = matrix @ query_vector

——这就是为什么你现在可以完全不要向量数据库。
264 块 × 1024 维的矩阵乘，numpy 跑一次不到 1 毫秒。
（等数据涨到几万块以上，才需要 faiss 的近似检索。现在上它是负优化。）

【为什么要和 BM25 融合，而不是二选一】
两路的盲区正好互补：

    BM25 强在：专有名词、函数名、变量名、错误码、精确短语
               （搜 "ENABLE_KB" 只有它能命中）
    向量强在：换个说法也能找到（"沙箱"和"工作区"在向量空间里很近）

融合用 RRF（倒数排序融合），只看名次不看分数——原因见下面 rrf_fuse 的注释。
"""

import hashlib
import json
import time
import urllib.error
import urllib.request

import numpy as np

import config
import rag

# 索引产物和关键词索引放在一起
INDEX_DIR = rag.INDEX_DIR
VEC_FILE = INDEX_DIR / "vectors.npy"
META_FILE = INDEX_DIR / "vectors.meta.json"


# ============================================================
# 第一部分：调接口，把文字变成向量
# ============================================================

def _api_key():
    """取出百炼的 Key，没填就报一个"能照着做"的错。"""
    key = getattr(config, "DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "还没填 DASHSCOPE_API_KEY。三步：\n"
            "  1. 打开 https://bailian.console.aliyun.com 开通百炼（免费，送 100 万 token）\n"
            "  2. 右上角「API-KEY」→ 创建，复制 sk- 开头的串\n"
            "  3. 粘进 config.py 的 DASHSCOPE_API_KEY = \"...\"\n"
            "注意：要百炼的 API-KEY，不是阿里云主账号的 AccessKey。"
        )
    return key


def _post_json(url, payload, key, timeout=60):
    """发一个 JSON POST 请求（用标准库 urllib，不装 requests）。

    这里故意不用 SDK：让你能直接看见请求体和返回长什么样。
    看懂了这一层，换任何厂商都只是改 URL 和模型名。
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        hint = {
            400: "请求参数有问题（多半是模型名或 dimensions 不被接受）",
            401: "Key 不对，或者不是百炼的 API-KEY",
            403: "没开通百炼服务，或者这个 Key 没有该模型权限",
            429: "被限流了，或者账户余额不足",
        }.get(e.code, "")
        raise RuntimeError(f"HTTP {e.code} {hint}\n{detail}") from e

    except urllib.error.URLError as e:
        raise RuntimeError(f"网络连不上：{e.reason}") from e


def embed_texts(texts, verbose=False):
    """把一批文本变成向量，返回 (N, dim) 的 numpy 数组，**已经归一化**。

    归一化是重点：让每个向量的长度都是 1，之后点积就直接等于余弦相似度，
    不用每次再除模长。
    """
    if not texts:
        return np.zeros((0, getattr(config, "EMBED_DIM", 1024)), dtype=np.float32)

    key = _api_key()
    url = config.EMBED_URL
    model = config.EMBED_MODEL
    dim = getattr(config, "EMBED_DIM", 1024)
    batch = getattr(config, "EMBED_BATCH", 10)

    vectors = []
    for start in range(0, len(texts), batch):
        part = texts[start:start + batch]
        payload = {
            "model": model,
            "input": part,
            "dimensions": dim,
            "encoding_format": "float",
        }
        resp = _post_json(url, payload, key)

        # 服务端不保证按顺序返回，用 index 字段排一下
        items = sorted(resp["data"], key=lambda x: x.get("index", 0))
        vectors.extend(item["embedding"] for item in items)

        if verbose:
            done = min(start + batch, len(texts))
            print(f"    已向量化 {done}/{len(texts)}")

    matrix = np.asarray(vectors, dtype=np.float32)

    # L2 归一化：每行除以它自己的长度
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


# ============================================================
# 第二部分：建向量索引
# ============================================================

def _fingerprint(chunks):
    """给所有块算一个指纹，用来判断"内容变没变"。"""
    h = hashlib.md5()
    for c in chunks:
        h.update(c["source"].encode("utf-8"))
        h.update(str(c["line"]).encode("utf-8"))
        h.update(c["heading"].encode("utf-8"))
        h.update(c["text"].encode("utf-8"))
    return h.hexdigest()


def _load_meta():
    if not META_FILE.exists():
        return None
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build(force=False, verbose=True):
    """给所有 chunk 算向量并落盘。

    花钱的地方：这一步会调接口。7.8 万字大约 8 万 token，
    text-embedding-v4 是 0.0005 元/千 token，**一次约 4 分钱**。
    所以加了指纹判断——内容没变就直接跳过，别重复花钱。
    """
    index = rag.load_index()
    if index is None:
        raise RuntimeError("还没有关键词索引，先运行：python rag.py build")

    chunks = index["chunks"]
    fingerprint = _fingerprint(chunks)
    meta = _load_meta()

    if (not force and meta and VEC_FILE.exists()
            and meta.get("fingerprint") == fingerprint):
        if verbose:
            print(f"向量索引已是最新（{meta['count']} 块 / {meta['dim']} 维），"
                  f"跳过。想强制重建加 --force")
        return

    # 送进模型的是"标题 + 正文"：标题提供了上下文，
    # 一个写着「第 2 节 触发条件」的段落，加上标题才知道是什么的触发条件。
    texts = [f"{c['heading']}\n\n{c['text']}" for c in chunks]

    if verbose:
        print(f"开始向量化：{len(chunks)} 块，模型 {config.EMBED_MODEL}\n")

    started = time.time()
    matrix = embed_texts(texts, verbose=verbose)

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(VEC_FILE, matrix)
    META_FILE.write_text(
        json.dumps({
            "model": config.EMBED_MODEL,
            "dim": int(matrix.shape[1]),
            "count": int(matrix.shape[0]),
            "fingerprint": fingerprint,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if verbose:
        cost = len(chunks) * 800 / 1000 * 0.0005 / 1000  # 粗估
        print(f"\n完成：{matrix.shape[0]} 块 × {matrix.shape[1]} 维，"
              f"耗时 {time.time() - started:.1f} 秒")
        print(f"落盘：{VEC_FILE.name}（{VEC_FILE.stat().st_size / 1024 / 1024:.1f} MB）")
        print(f"本次大约花费 {cost:.4f} 元（免费额度内不扣）")


# ============================================================
# 第三部分：向量检索
# ============================================================

_matrix_cache = {"data": None, "mtime": None}


def _load_matrix():
    """读向量矩阵（带缓存，同一个进程只读一次盘）。"""
    if not VEC_FILE.exists():
        return None
    mtime = VEC_FILE.stat().st_mtime
    if _matrix_cache["data"] is not None and _matrix_cache["mtime"] == mtime:
        return _matrix_cache["data"]

    matrix = np.load(VEC_FILE)
    _matrix_cache["data"] = matrix
    _matrix_cache["mtime"] = mtime
    return matrix


def search_vector(query, top_k=None):
    """纯向量检索。返回格式和 rag.search() 完全一样，方便融合。

    返回 None 表示"向量索引还没建"。
    """
    index = rag.load_index()
    matrix = _load_matrix()
    if index is None or matrix is None:
        return None

    top_k = top_k or getattr(config, "KB_TOP_K", 5)
    chunks = index["chunks"]

    query_vec = embed_texts([query])[0]

    # 一行矩阵乘 = 264 次点积 = 264 个余弦相似度
    scores = matrix @ query_vec

    # 数量不大，直接全排序；几万条时再换成 argpartition
    order = np.argsort(-scores)[:top_k]

    results = []
    for cid in order:
        cid = int(cid)
        ch = chunks[cid]
        results.append({
            "source": ch["source"],
            "heading": ch["heading"],
            "line": ch["line"],
            "text": ch["text"],
            "score": round(float(scores[cid]), 4),
        })
    return results


# ============================================================
# 第四部分：融合（RRF）
# ============================================================

def rrf_fuse(runs, top_k=5, k=60):
    """RRF —— 倒数排序融合（Reciprocal Rank Fusion）。

    公式：某块的总分 = Σ 1 / (k + 它在第 i 路里的名次)

    【为什么不用分数加权求和】
    因为两路的分数量纲根本不一样：
        BM25 的分数没有上界，可能是 3 分，也可能 30 分
        余弦相似度被限制在 -1 ~ 1 之间
    直接相加，等于让 BM25 单方面决定结果，哪怕向量那路判得更准。

    【为什么 RRF 好用】
    它只看**名次**，不看分数。第 1 名就是第 1 名，不管你打 30 分还是 0.9 分。
    于是两路天然可比，还不需要调权重——这是它成为业界默认做法的原因。

    k=60 是个经验值：k 越大，名次之间的差距被压得越平。
    """
    scores = {}
    origin = {}     # 记住第一次出现时的原始结果（拿来取正文）

    for run in runs:
        for rank, item in enumerate(run, 1):
            key = (item["source"], item["line"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            origin.setdefault(key, item)

    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]

    results = []
    for key, score in ranked:
        item = dict(origin[key])
        item["score"] = round(score, 5)
        results.append(item)
    return results


def hybrid_search(query, top_k=None):
    """混合检索：BM25 + 向量 → RRF 融合。

    返回 (结果列表, 实际用了哪几路)
    """
    top_k = top_k or getattr(config, "KB_TOP_K", 5)
    recall = getattr(config, "RECALL_TOP", 30)

    bm25 = rag.search(query, recall) or []
    vector = search_vector(query, recall)

    # 向量索引还没建 → 悄悄退回纯关键词检索，不影响正常使用
    if vector is None:
        return bm25[:top_k], "bm25"

    return rrf_fuse([bm25, vector], top_k), "hybrid"


# ============================================================
# 命令行：python kb_vector.py build / search / compare
# ============================================================

def _show(title, hits):
    print(f"— {title} —")
    if not hits:
        print("   （没有结果）\n")
        return
    for i, h in enumerate(hits, 1):
        print(f"  {i}. {h['source']} · {h['heading'][:40]}")
        print(f"     {h['score']}  {h['text'][:60].replace(chr(10), ' ')}…")
    print()


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"

    if cmd == "build":
        force = "--force" in sys.argv
        try:
            build(force=force)
        except RuntimeError as e:
            print(f"\n出错了：\n{e}")
            sys.exit(1)

    elif cmd == "search":
        query = " ".join(a for a in sys.argv[2:] if not a.startswith("--"))
        if not query:
            print("用法：python kb_vector.py search 沙箱")
            sys.exit(1)
        try:
            hits = search_vector(query, 5)
        except RuntimeError as e:
            print(f"\n出错了：\n{e}")
            sys.exit(1)
        if hits is None:
            print("向量索引还没建，先运行：python kb_vector.py build")
            sys.exit(1)
        _show(f"向量检索「{query}」", hits)

    elif cmd == "compare":
        query = " ".join(a for a in sys.argv[2:] if not a.startswith("--"))
        if not query:
            print("用法：python kb_vector.py compare 沙箱")
            sys.exit(1)

        try:
            t0 = time.time()
            bm = rag.search(query, 5) or []
            t1 = time.time()
            vec = search_vector(query, 5) or []
            t2 = time.time()
            fused, mode = hybrid_search(query, 5)
            t3 = time.time()
        except RuntimeError as e:
            print(f"\n出错了：\n{e}")
            sys.exit(1)

        print(f"\n查询：{query}\n")
        _show(f"BM25 关键词（{(t1-t0)*1000:.0f} ms）", bm)
        _show(f"向量语义（{(t2-t1)*1000:.0f} ms）", vec)
        _show(f"融合结果（{(t3-t2)*1000:.0f} ms · {mode}）", fused)

        if mode == "hybrid":
            keys_bm = {(h["source"], h["line"]) for h in bm}
            keys_vec = {(h["source"], h["line"]) for h in vec}
            only_one = keys_bm ^ keys_vec
            print(f"两路各自命中的块里，有 {len(only_one)} 块是对方没找到的"
                  f"——这就是融合的价值。")

    else:
        print("用法：python kb_vector.py build | search <词> | compare <词>")
