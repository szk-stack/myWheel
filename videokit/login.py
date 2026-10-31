"""
videokit/login.py —— B站扫码登录，把 cookie 存到本地

为什么要它：
  未登录时，B站对这些接口的风控很紧——UP 主作品列表虽然靠设备指纹能过，
  但限流严重（每页 7 秒、时不时 412），AI 字幕还根本看不到。
  扫码登录一次，拿到 SESSDATA，这些全都顺了。

为什么用扫码而不是让用户手抄 SESSDATA：
  手抄要开 F12 翻 Cookie、容易复制错、还要解释"只要值不要整行"。
  扫码只需要手机点一下，体验差一个量级。

存哪：项目根目录的 bili_cookie.json（已加进 .gitignore）。
  里面是登录凭据，等同密码——不要提交、不要发人。
"""

import json
import time
from pathlib import Path

import requests

PASSPORT = "https://passport.bilibili.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 登录后真正用得上的几个，其余不存（少存一个，泄一个的损失）
KEEP = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
    "b_nut",
)


def _project_root():
    return Path(__file__).resolve().parent.parent


def cookie_path(root=None):
    return Path(root or _project_root()) / "bili_cookie.json"


def load_cookie(root=None):
    """读本地登录凭据；没有就返回 {}。"""
    p = cookie_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in KEEP and v}
    except Exception:
        return {}


def save_cookie(cookies, root=None, extra=None):
    p = cookie_path(root)
    payload = {k: v for k, v in cookies.items() if k in KEEP and v}
    if extra:
        payload.update(extra)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _session():
    s = requests.Session()
    s.trust_env = False          # 本机/直连，别走系统代理
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    return s


def _print_qr(url):
    """在终端里画二维码。没有 qrcode 库就退回打印链接。"""
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        print("（没装 qrcode 库，画不出二维码）请把下面这段贴到浏览器打开：")
        print(url)


def whoami(session):
    """确认登录态：返回 (是否登录, 昵称, mid)。"""
    try:
        d = session.get(
            "https://api.bilibili.com/x/web-interface/nav", timeout=15
        ).json()
        data = d.get("data") or {}
        return bool(data.get("isLogin")), data.get("uname", ""), data.get("mid", 0)
    except Exception:
        return False, "", 0


def qr_login(root=None, timeout=180, on_qr=None):
    """走一遍扫码登录。成功返回 cookie 字典，失败抛异常。

    三种状态码（B站约定）：
      86101 未扫码 → 继续等
      86090 已扫码，等手机点确认 → 继续等
      0     成功 → Set-Cookie 里就带着 SESSDATA
      86038 二维码过期 → 只能重来
    """
    s = _session()
    g = s.get(PASSPORT + "/x/passport-login/web/qrcode/generate", timeout=15).json()
    if g.get("code") != 0:
        raise RuntimeError(f"拿二维码失败：{g.get('message')}")

    url = (g.get("data") or {}).get("url", "")
    key = (g.get("data") or {}).get("qrcode_key", "")
    if not url or not key:
        raise RuntimeError("二维码数据不完整")

    (on_qr or _print_qr)(url)

    started = time.time()
    scanned = False
    while time.time() - started < timeout:
        time.sleep(2)
        p = s.get(
            PASSPORT + "/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": key},
            timeout=15,
        ).json()
        data = p.get("data") or {}
        code = data.get("code")

        if code == 0:
            cookies = {c.name: c.value for c in s.cookies if "bilibili" in (c.domain or "")}
            path = save_cookie(cookies, root, extra={"saved_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            ok, uname, mid = whoami(s)
            return {
                "ok": ok,
                "uname": uname,
                "mid": mid,
                "path": str(path),
                "cookies": sorted(k for k in cookies if k in KEEP),
            }
        if code == 86038:
            raise RuntimeError("二维码过期了，重新运行一次")
        if code == 86090 and not scanned:
            scanned = True
            print("\n已扫码，请在手机上点「确认登录」…")

    raise TimeoutError(f"等了 {timeout} 秒还没确认")


def main():
    print("=== B站扫码登录 ===")
    print("用手机 B站 App 扫下面的二维码（不要用微信）：\n")
    try:
        result = qr_login()
    except Exception as e:
        print(f"\n失败：{e}")
        return 1

    if result["ok"]:
        print(f"\n登录成功：{result['uname']}（mid={result['mid']}）")
        print(f"凭据已保存到：{result['path']}")
        print(f"包含字段：{', '.join(result['cookies'])}")
        print("\n现在重启 agent 服务即可生效（cookie 是启动时读的）。")
        return 0

    print(f"\n拿到 cookie 了，但校验没通过；文件在 {result['path']}，可以稍后再试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
