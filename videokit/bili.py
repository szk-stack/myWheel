"""
videokit/bili.py —— B 站接口的最小实现

只用 requests，不引 bilibili-api：
  那个库（4.2k star）和它的接口文档库（bilibili-API-collect，20k star）
  都已经归档，引进来等于给自己埋一个没人修的依赖。
  这里只实现我们真正用到的 6 个接口，签名逻辑集中在本文件一处——
  哪天 WBI 变了，只改这里。

用到的接口：
  /x/web-interface/view          视频元信息（标题、时长、cid、UP主）
  /x/web-interface/search/type   关键词搜索
  /x/space/wbi/arc/search        UP 主的视频列表
  /x/player/v2                   字幕列表（需要登录 cookie 才看得到 AI 字幕）
  /x/player/playurl             音频/视频流地址（免登录可用）
  /x/web-interface/nav           取 WBI 签名用的两个 key

★ 三个实测结论（2026-10-30 验证，写在这里免得以后重复踩）：
  1. 搜索接口**不带 WBI 也能返回结果**，但带上更稳；这里先带、失败再退回不带。
  2. nav 未登录时返回 code=-101，但 data.wbi_img 仍然有值——不能把它当失败。
  3. 音频流不需要登录，但要带 Referer，否则 CDN 返回 403。
"""

import base64
import hashlib
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path

import requests

API = "https://api.bilibili.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# WBI 签名用的混淆表（B站前端写死的那张，顺序不能动）
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.I)

# 空间接口要的"软指纹"：B站只检查有没有，不验真伪（固定值即可）
_B64_WEBGL = base64.b64encode(b"WebGL 1.0 (OpenGL ES 2.0 Chromium)").decode()
_B64_GPU = base64.b64encode(
    b"ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU Direct3D11 vs_5_0 ps_5_0), or similar"
).decode()


class BiliError(RuntimeError):
    """接口返回了非 0 的 code。带上 code，方便判断是风控还是没权限。"""

    def __init__(self, code, message, path=""):
        self.code = code
        self.message = message
        super().__init__(f"B站接口出错（code={code}）：{message}")


# ------------------------------------------------------------
# 会话与签名
# ------------------------------------------------------------

_session = None
_wbi_cache = {"img": "", "sub": "", "at": 0.0}


def _cookie():
    """取 cookie：配置里手填的优先，其次用扫码登录存下来的。

    ★ 登录态能解决三件免登录做不到的事：
      1. AI 字幕（未登录时字幕列表永远是空的）；
      2. 高画质 / 高码率流；
      3. UP 主作品列表的限流会宽松很多（免登录每页要等 7 秒）。
    """
    try:
        import config

        ck = (getattr(config, "VIDEO_BILI_COOKIE", "") or "").strip()
        if ck:
            return ck
    except Exception:
        pass

    try:
        from . import login

        jar = login.load_cookie()
        if jar:
            return "; ".join(f"{k}={v}" for k, v in jar.items())
    except Exception:
        pass
    return ""


def session():
    """全局复用一个 Session：省掉每次 TCP 握手，也让 cookie 自动带着走。"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(
            {
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
            }
        )
        ck = _cookie()
        if ck:
            _session.headers["Cookie"] = ck
    return _session


def _get(path, params=None, signed=False, ok_codes=(0,), timeout=20):
    """发一个 GET，检出 code，返回 data。

    ok_codes 用来放行"其实不算失败"的 code——目前只有 nav 的 -101。
    """
    params = dict(params or {})
    if signed:
        params = sign(params)

    resp = session().get(API + path, params=params, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()

    code = payload.get("code")
    if code not in ok_codes:
        raise BiliError(code, payload.get("message") or "未知错误", path)
    return payload.get("data")


def _wbi_keys():
    """取签名用的 img_key / sub_key，缓存一小时（B站每天轮换，不用每次拉）。"""
    if _wbi_cache["img"] and time.time() - _wbi_cache["at"] < 3600:
        return _wbi_cache["img"], _wbi_cache["sub"]

    data = _get("/x/web-interface/nav", ok_codes=(0, -101)) or {}
    wbi = data.get("wbi_img") or {}
    img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise BiliError(-1, "拿不到 WBI key（接口结构可能变了）", "/x/web-interface/nav")

    _wbi_cache.update({"img": img, "sub": sub, "at": time.time()})
    return img, sub


def sign(params):
    """给参数加上 wts 和 w_rid（WBI 签名）。"""
    img, sub = _wbi_keys()
    raw = img + sub
    mixin = "".join(raw[i] for i in MIXIN_TAB)[:32]

    # B站规定：值里的 !'()* 要先剔掉，再按 key 排序拼查询串
    clean = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    clean["wts"] = str(int(time.time()))
    query = urllib.parse.urlencode(sorted(clean.items()))
    clean["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return clean


_buvid_ok = False


def ensure_buvid():
    """拿一个 buvid3 设备标识，塞进会话 cookie。

    ★ 这是搜索接口的**硬门槛**，不加会直接吃 HTTP 412（Precondition Failed）。
      412 不是业务错误码，是 WAF 在 HTTP 层拦掉的——所以它不会返回
      {"code": -xxx}，而是 requests 抛 HTTPError，很容易被误当成网络问题。
      好消息是免登录就能领：/x/frontend/finger/spi 会给 b_3 / b_4。
    """
    global _buvid_ok
    s = session()
    # 已经从登录凭据里带了 buvid3，就不用再领
    existing = (s.headers.get("Cookie") or "") + ";" + ";".join(
        f"{c.name}={c.value}" for c in s.cookies
    )
    if _buvid_ok or "buvid3=" in existing:
        _buvid_ok = True
        return

    try:
        d = _get("/x/frontend/finger/spi", ok_codes=(0,)) or {}
        b3, b4 = d.get("b_3"), d.get("b_4")
        if b3:
            s.cookies.set("buvid3", b3, domain=".bilibili.com")
            if b4:
                s.cookies.set("buvid4", b4, domain=".bilibili.com")
            _buvid_ok = True
    except Exception:
        pass   # 领不到就照常走，失败时由调用方给出人话解释


def _is_412(exc):
    return "412" in str(exc)


def _is_risk(exc):
    """判断是不是"被风控挡了"，而不是真的没数据。

    两种形态都要认：
      - HTTP 412 Precondition Failed —— WAF 在 HTTP 层拦的，requests 抛异常
      - BiliError code=-352「风控校验失败」—— 接口层拦的
    """
    return _is_412(exc) or getattr(exc, "code", None) == -352


def _get_guarded(path, params, signed=True, retries=1):
    """带 buvid + 一次退避重试的 GET。

    ★ 实测结论（2026-10-30，反复试过才敢写）：
      - 缺 buvid3 时，`search/type` 必挂 412；补上就稳——这是**真 bug**，已修；
      - 但 `space/wbi/arc/search`（UP 主作品列表）是另一套更强的风控，
        补 buvid、换签名、换 Referer、退避 2/4 秒都只能偶尔过，
        连续打几次之后会长时间返回 412 / -352。
      → 所以这里只留**一次**退避重试（捞一下偶发抖动），
        再往上加次数只是白等，真正的解法是配 `VIDEO_BILI_COOKIE`。
    """
    global _buvid_ok
    ensure_buvid()

    last = None
    for attempt in range(retries + 1):
        try:
            return _get(path, params, signed=signed)
        except Exception as e:
            if not _is_risk(e):
                raise
            last = e
            if attempt < retries:
                time.sleep(2.0)
                _buvid_ok = False          # 顺手换一个设备标识
                ensure_buvid()
    raise last


def _search_api(search_type, keyword, page, order=None):
    """搜索接口的统一入口：管签名、管 buvid、失败自动降级。

    B站的搜索对签名其实不敏感（带不带都能返回），但对 buvid3 极敏感。
    所以策略是：带签名试 → 失败退不签名 → 仍被风控就重新领 buvid 再来。
    """
    params = {"search_type": search_type, "keyword": keyword, "page": page}
    if order:
        params["order"] = order

    try:
        return _get_guarded("/x/web-interface/search/type", params, signed=True)
    except BiliError:
        return _get_guarded("/x/web-interface/search/type", params, signed=False)


# ------------------------------------------------------------
# 地址解析与元信息
# ------------------------------------------------------------


def resolve(text):
    """把用户给的东西统一成 {bvid} 或 {aid}。

    认：BV 号、av 号、完整链接、b23.tv 短链、分享文案里夹的链接。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("链接或 BV 号不能为空")

    m = _BV_RE.search(text)
    if m:
        return {"bvid": m.group(1)}

    m = _AV_RE.search(text)
    if m:
        return {"aid": int(m.group(1))}

    if "http" in text:
        # 短链要跟一次跳转才能看到 BV 号
        url = text[text.index("http") :].split()[0]
        try:
            resp = session().get(url, allow_redirects=True, timeout=15)
        except Exception as e:
            raise ValueError(f"打不开这个链接：{e}") from e
        m = _BV_RE.search(resp.url)
        if m:
            return {"bvid": m.group(1)}

    raise ValueError(f"认不出这个输入里的视频：{text[:60]}")


def video_info(bvid=None, aid=None):
    """视频元信息。cid 是"这一 P"的 id，取字幕和音频流都要它。"""
    params = {"bvid": bvid} if bvid else {"aid": aid}
    d = _get("/x/web-interface/view", params) or {}
    owner = d.get("owner") or {}
    stat = d.get("stat") or {}
    return {
        "bvid": d.get("bvid", ""),
        "aid": d.get("aid", 0),
        "cid": d.get("cid", 0),
        "title": d.get("title", ""),
        "desc": (d.get("desc") or "").strip(),
        "duration": d.get("duration", 0),
        "owner": owner.get("name", ""),
        "mid": owner.get("mid", 0),
        "pubdate": d.get("pubdate", 0),
        "pages": len(d.get("pages") or []),
        "view": stat.get("view", 0),
        "like": stat.get("like", 0),
    }


def parse_duration(text):
    """搜索接口给的时长是 "17:2" / "1:02:03" 这种字符串，转成秒。"""
    parts = [p for p in str(text or "").split(":") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return nums[0] if nums else 0


def _clean_title(text):
    """搜索结果的标题带 <em class="keyword"> 高亮标签，去掉。"""
    return re.sub(r"<[^>]+>", "", text or "").strip()


# ------------------------------------------------------------
# 搜索 / UP主列表
# ------------------------------------------------------------


def _search_page(keyword, page, order):
    return _search_api("video", keyword, page, order)


def search(keyword, limit=15, order="totalrank", min_minutes=0, days=0):
    """关键词搜视频，返回候选列表（不下载）。

    order: totalrank（综合）/ click（播放）/ pubdate（最新）
    min_minutes: 只要这个时长以上的——"系统学点什么"通常 10 分钟起步
    days: 只要最近 N 天发布的（0 = 不限）——"这两天的视频"就靠它
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("关键词不能为空")

    min_seconds = int(min_minutes or 0) * 60
    cutoff = (time.time() - int(days) * 86400) if days else 0
    out, page = [], 1
    retried_risk = False

    while len(out) < limit and page <= 3:
        try:
            data = _search_page(keyword, page, order) or {}
        except Exception as e:
            # ★ 这条经验抄自 dsh-bilibili（moxingovo 的 B站插件）：
            #   B站会**定向风控默认的综合排序**，而 pubdate 排序通常放行。
            #   所以撞风控时换排序重试一次，比干等或原样重试有效。
            #   只在调用方没显式指定排序时降级，免得改掉用户的选择。
            if not retried_risk and order == "totalrank" and _is_risk(e):
                retried_risk = True
                order = "pubdate"
                continue
            raise

        items = data.get("result") or []
        if not items:
            break
        for it in items:
            if not it.get("bvid"):
                continue          # 搜索里会混进没有 BV 号的广告位，取不了
            seconds = parse_duration(it.get("duration"))
            if min_seconds and seconds < min_seconds:
                continue
            if cutoff and (it.get("pubdate") or 0) < cutoff:
                continue
            out.append(
                {
                    "bvid": it.get("bvid", ""),
                    "title": _clean_title(it.get("title")),
                    "author": it.get("author", ""),
                    "duration": seconds,
                    "view": it.get("play", 0),
                    "pubdate": it.get("pubdate", 0),
                    "desc": _clean_title(it.get("description"))[:90],
                }
            )
            if len(out) >= limit:
                break
        page += 1

    return out[:limit]


def search_user(keyword, limit=5):
    """按名字搜 UP 主，把"影视飓风"这种名字换成 mid。"""
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("UP 主名不能为空")

    data = _search_api("bili_user", keyword, 1) or {}
    items = data.get("result") or []
    return [
        {
            "mid": it.get("mid"),
            "uname": _clean_title(it.get("uname")),
            "fans": it.get("fans", 0),
            "videos": it.get("videos", 0),
        }
        for it in items[:limit]
    ]


def user_videos(mid, limit=15, order="pubdate", cache_dir=None, ps=30, days=0):
    """某个 UP 主的视频列表（★ 批量看一个作者就靠它）。

    ★ 这个接口比搜索难对付得多，三条经验都是实测出来的：
      1. 不带**设备指纹参数**必挂 -352「风控校验失败」——补 dm_img_str /
         dm_cover_img_str / dm_img_list / platform / web_location 才行。
      2. 指纹参数必须**一起参与 WBI 签名**（只加不签仍然 -352）。
      3. 连打会被限流（实测连打 4 次只成 1 次），拉开 8 秒能到 2/3。
         所以这里自带节流：两次请求至少隔 _SPACE_MIN_INTERVAL 秒。
    代价是慢：一个 142 个视频的 UP 主要翻 5 页，约 40 秒。所以结果会缓存。

    cache_dir 给了就落盘缓存（同一天内不重复爬）。
    """
    mid = str(mid).strip()
    if not mid.isdigit():
        raise ValueError(f"mid 应该是一串数字：{mid!r}")

    cached = _read_author_cache(cache_dir, mid)
    items = list(cached["items"]) if cached else []
    total = cached.get("total", 0) if cached else 0

    # 缓存够用就直接返回（够用 = 条数满足 limit，或者已经取全了）
    if items and (len(items) >= limit or (total and len(items) >= total)):
        fresh = items
        if days:
            cutoff = time.time() - int(days) * 86400
            fresh = [x for x in items if (x.get("pubdate") or 0) >= cutoff]
        return {
            "items": fresh[:limit],
            "total": total,
            "mid": mid,
            "cached": True,
            "partial": False,
            "recent": len(fresh),
        }

    # 不够就**续取**：从上次停下的那一页接着翻，按 bvid 去重。
    # ★ 为什么不整段重爬：一个 142 个视频的号要翻 5 页、每页 7 秒，
    #   为了多要 10 条重爬 35 秒很蠢。
    seen = {x["bvid"] for x in items}
    page = (len(items) // ps + 1) if items else 1
    partial = False

    while len(items) < limit and page <= 20:
        try:
            data = _space_page(mid, page, ps, order)
        except Exception as e:
            # ★ 关键：已经拿到一部分时，别因为后面某页被拦就整体失败。
            #   这个接口本来就时好时坏，把 30 条丢掉、只回一个错误，
            #   等于让用户白等 20 秒。给多少算多少，并标出来。
            if _is_risk(e) and items:
                partial = True
                break
            raise

        vlist = ((data.get("list") or {}).get("vlist")) or []
        total = (data.get("page") or {}).get("count") or total
        if not vlist:
            break
        for it in vlist:
            bv = it.get("bvid", "")
            if not bv or bv in seen:
                continue
            seen.add(bv)
            items.append(
                {
                    "bvid": bv,
                    "title": it.get("title", ""),
                    "author": it.get("author", ""),
                    "duration": parse_duration(it.get("length")),
                    "view": it.get("play", 0),
                    "pubdate": it.get("created", 0),
                    "desc": (it.get("description") or "")[:90],
                }
            )
            if len(items) >= limit:
                break
        page += 1

    _write_author_cache(cache_dir, mid, items, total)

    # 「这两天的视频」：按发布时间过滤（缓存里存全量，过滤在返回时做，
    # 这样一个缓存能给不同 days 用）
    fresh = items
    if days:
        cutoff = time.time() - int(days) * 86400
        fresh = [x for x in items if (x.get("pubdate") or 0) >= cutoff]

    return {
        "items": fresh[:limit],
        "total": total,
        "mid": mid,
        "cached": bool(cached),
        "partial": partial,
        "recent": len(fresh),
    }


# 空间接口的节流：两次请求之间至少隔这么久（秒）
_SPACE_MIN_INTERVAL = 7.0
_space_last = 0.0
_space_lock = threading.Lock()


def _space_params(mid, page, ps, order):
    """空间接口的设备指纹参数。

    ★ 这几个是 2024 年后加的"软指纹"：B站不校验真伪，只要求**有**
      （伪造固定的值就能过）。少一个就是 -352。
    """
    return {
        "mid": mid,
        "ps": ps,
        "pn": page,
        "order": order,
        "platform": "web",
        "web_location": 1550101,
        "dm_img_list": "[]",
        "dm_img_str": _B64_WEBGL,
        "dm_cover_img_str": _B64_GPU,
    }


def _space_page(mid, page, ps, order, retries=2):
    """取一页，失败按节流间隔重试。"""
    global _space_last
    params = _space_params(mid, page, ps, order)

    last = None
    for attempt in range(retries + 1):
        with _space_lock:
            wait = _SPACE_MIN_INTERVAL - (time.time() - _space_last)
            if wait > 0:
                time.sleep(wait)
            _space_last = time.time()
        try:
            return _get("/x/space/wbi/arc/search", params, signed=True)
        except Exception as e:
            if not _is_risk(e):
                raise
            last = e
    raise last


def _author_cache_path(cache_dir, mid):
    return Path(cache_dir) / f"author-{mid}.json" if cache_dir else None


def _read_author_cache(cache_dir, mid):
    p = _author_cache_path(cache_dir, mid)
    if not p or not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - d.get("fetched_at", 0) > 86400:   # 一天后过期
            return None
        return d
    except Exception:
        return None


def _write_author_cache(cache_dir, mid, items, total):
    p = _author_cache_path(cache_dir, mid)
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"mid": mid, "items": items, "total": total, "fetched_at": time.time()},
                ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception:
        pass   # 缓存写不进去不影响主流程


# ------------------------------------------------------------
# 字幕与音频
# ------------------------------------------------------------


def subtitles(bvid, cid):
    """取这一 P 的字幕轨列表。

    ★ 未登录时通常返回空数组——不是接口坏了，是 AI 字幕只对登录用户开放。
      遇到空要先往这个方向怀疑，别急着以为视频没有字幕。
    """
    data = _get("/x/player/v2", {"bvid": bvid, "cid": cid}) or {}
    subs = ((data.get("subtitle") or {}).get("subtitles")) or []
    if subs:
        return subs
    # 再试一次带签名的 wbi 版本（有的视频只在它下面给）
    try:
        data = _get("/x/player/wbi/v2", {"bvid": bvid, "cid": cid}, signed=True) or {}
        return ((data.get("subtitle") or {}).get("subtitles")) or []
    except BiliError:
        return []


def subtitle_segments(url, timeout=20):
    """下载字幕 JSON，转成 [(t0, t1, 文本)]。

    字幕文件长这样：{"body": [{"from": 1.2, "to": 3.4, "content": "..."}]}
    """
    if url.startswith("//"):
        url = "https:" + url
    resp = session().get(url, timeout=timeout)
    resp.raise_for_status()
    body = (resp.json() or {}).get("body") or []
    return [
        {
            "t0": float(b.get("from", 0)),
            "t1": float(b.get("to", 0)),
            "text": (b.get("content") or "").strip(),
        }
        for b in body
        if (b.get("content") or "").strip()
    ]


def audio_url(bvid, cid):
    """取这一 P 的最佳音轨地址。

    fnval=16 表示要 DASH 格式：音视频分离，音轨是独立的 m4a——
    正好，转写只要音频，不用把整个视频拖下来。
    """
    data = _get(
        "/x/player/playurl", {"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1}
    ) or {}
    dash = data.get("dash") or {}
    audios = dash.get("audio") or []
    if audios:
        best = max(audios, key=lambda a: a.get("bandwidth", 0))
        return best.get("baseUrl") or best.get("base_url", "")

    # 老格式兜底（部分视频没有 DASH）
    durl = data.get("durl") or []
    if durl:
        return durl[0].get("url", "")
    raise BiliError(-1, "这个视频没有可用的音频流（可能需要大会员或已下架）")


def download(url, dest, chunk=1 << 18, on_progress=None):
    """下载到本地文件。

    ★ 必须带 Referer：B站 CDN 会校验，不带就 403。
    """
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    total = 0
    with session().get(url, headers=headers, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        size = int(resp.headers.get("Content-Length") or 0)
        with open(dest, "wb") as f:
            for block in resp.iter_content(chunk):
                if not block:
                    continue
                f.write(block)
                total += len(block)
                if on_progress:
                    on_progress(total, size)
    return total
