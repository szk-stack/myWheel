"""
kb_eval.py —— 检索效果评估

【为什么要有这个文件】
"我加了向量检索，效果明显变好了"——这句话在面试里一文不值，因为没有数字。
这个脚本把 30 道题各跑一遍三种检索，算出命中率，让"变好了多少"变成可比较的数。

【两个指标】
Recall@5
    前 5 条里有没有正确答案。有就算过，不管排第几。
    这是检索层最常用的指标——因为后面还有个模型，它看到 5 条里有一条对的就能答对。

MRR（Mean Reciprocal Rank，平均倒数排名）
    正确答案排第 1 得 1 分，排第 2 得 1/2 分，排第 3 得 1/3 分，没命中得 0 分。
    它衡量的是"排得靠不靠前"。Recall 只看有没有，MRR 还看位置。

【怎么算"命中"】
questions.jsonl 里每道题都标了期望命中的位置：
    {"src": "docs/xxx.md", "h": "触发条件"}
只要返回的某一条同时满足"文件对得上"和"标题里含有关键词"，就算命中。

【怎么用】
    python kb_eval.py              跑三种方法并对比
    python kb_eval.py bm25         只跑某一种
    python kb_eval.py --bad        额外列出混合检索没命中的题（默认就列）
"""

import json
import sys
import time
from pathlib import Path

import rag

ROOT = Path(__file__).resolve().parent
QUESTIONS = ROOT / "eval" / "questions.jsonl"

TOP_K = 5


def load_questions():
    items = []
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def is_hit(chunk, expects):
    """这一块是否满足任意一条期望标注。"""
    for e in expects:
        src = e.get("src", "")
        head = e.get("h", "")
        if src and src not in chunk["source"]:
            continue
        if head and head not in chunk["heading"]:
            continue
        return True
    return False


def retrieve(query, method, top_k):
    """按指定方法检索。返回 (结果列表, 是否可用)。"""
    if method == "bm25":
        return (rag.search(query, top_k) or []), True

    import kb_vector

    if method == "vector":
        hits = kb_vector.search_vector(query, top_k)
        return (hits or []), hits is not None

    # hybrid
    hits, mode = kb_vector.hybrid_search(query, top_k)
    return hits, mode == "hybrid"


def evaluate(method, questions, top_k=TOP_K):
    hit_count = 0
    rr_sum = 0.0
    bad = []
    missed = []

    for q in questions:
        results, ok = retrieve(q["q"], method, top_k)
        if not ok:
            return None

        rank = None
        for i, h in enumerate(results, 1):
            if is_hit(h, q["expect"]):
                rank = i
                break

        if rank:
            hit_count += 1
            rr_sum += 1.0 / rank
            if rank > 1:
                missed.append((q, rank, results))
        else:
            bad.append((q, results))

    n = len(questions)
    return {
        "recall": hit_count / n if n else 0.0,
        "mrr": rr_sum / n if n else 0.0,
        "hits": hit_count,
        "total": n,
        "bad": bad,
        "missed": missed,
    }


def main():
    questions = load_questions()
    print(f"评估集：{len(questions)} 道题   top_k = {TOP_K}\n")

    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    methods = wanted or ["bm25", "vector", "hybrid"]
    labels = {"bm25": "BM25 关键词", "vector": "向量语义", "hybrid": "混合（RRF）"}

    rows = []
    for m in methods:
        t0 = time.time()
        r = evaluate(m, questions)
        if r is None:
            print(f"{labels.get(m, m):<12} 跳过（向量索引没建，先跑 python kb_vector.py build）")
            continue
        r["time"] = time.time() - t0
        r["method"] = m
        rows.append(r)

    print(f"{'方法':<14}{'Recall@5':>10}{'MRR':>10}{'命中':>8}{'耗时':>9}")
    print("-" * 52)
    for r in rows:
        print(f"{labels.get(r['method'], r['method']):<14}"
              f"{r['recall']:>10.3f}{r['mrr']:>10.3f}"
              f"{r['hits']:>6}/{r['total']:<3}"
              f"{r['time']:>7.1f}s")

    # 相对第一个方法涨了多少
    if len(rows) >= 2:
        base = rows[0]
        print()
        for r in rows[1:]:
            d = r["hits"] - base["hits"]
            sign = "+" if d > 0 else ""
            print(f"  {labels.get(r['method'], r['method'])} 比 "
                  f"{labels.get(base['method'], base['method'])}："
                  f"多命中 {sign}{d} 题，Recall {sign}{r['recall'] - base['recall']:.3f}")

    # ---- 没命中的题（这是最值钱的部分）----
    worst = rows[-1] if rows else None
    if worst and worst["bad"]:
        print(f"\n—— {labels.get(worst['method'], worst['method'])} 没命中的 "
              f"{len(worst['bad'])} 道题 ——")
        for q, results in worst["bad"]:
            exp = " / ".join(f"{e['src'].split('/')[-1]}:{e['h']}" for e in q["expect"])
            top = results[0]["heading"][-26:] if results else "（无结果）"
            print(f"\n  {q['id']} {q['q']}")
            print(f"      期望：{exp}")
            print(f"      实际第 1：{top}")

    # ---- 命中了但没排第一的 ----
    if worst and worst["missed"]:
        print(f"\n—— 命中了但不在第 1 位的有 {len(worst['missed'])} 道 ——")
        for q, rank, _ in worst["missed"]:
            print(f"  {q['id']} 排第 {rank}：{q['q']}")

    print("\n（改完切分 / 检索参数后重跑这个脚本，对比数字有没有变）")


if __name__ == "__main__":
    main()
