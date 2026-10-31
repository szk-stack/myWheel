"""
工具：bili_login —— 让用户扫码登录 B站

用法（agent 视角）：
  bili_login()                 → 返回一个链接 + 二维码，把它原样发给用户
  bili_login(action="status")  → 问一下"登上了没"

为什么要做成"给链接"而不是"工具自己等"：
  扫码是**人的动作**，可能隔一分钟才做。工具调用不能卡在那里等
  （会阻塞整个对话，而且用户看不到二维码）。
  所以工具只负责生成二维码并把链接交出去，登录过程由那个页面推进，
  成功之后服务端把凭据写进 bili_cookie.json，后续调用自动就是登录态。
"""

import config
import toolbox
from toolbox import register
from videokit import login as bili_login

DESCRIPTION = {
    "name": "bili_login",
    "description": (
        "让用户登录 B站（扫码）。用户说「登录B站」「登一下B站」「要 AI 字幕」时调用。"
        "调用后**必须把返回的链接和二维码原样发给用户**，不要改写 URL。"
        "登录后 AI 字幕可用、UP 主作品列表不再频繁被限流。"
        "参数 action：start（默认，生成二维码给链接）/ status（查当前登录状态）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "status"],
                "description": "start 生成二维码；status 查询是否已登录",
            },
        },
    },
}


def _base_url():
    return f"http://127.0.0.1:{getattr(config, 'PORT', 8000)}"


def run(arguments):
    action = (arguments.get("action") or "start").lower()
    root = toolbox.current_workspace() or str(toolbox.PROJECT_ROOT)

    if action == "status":
        info = bili_login.status(root)
        state = info.get("state")
        if state == "ok":
            return (
                f"已登录：{info.get('uname') or '（昵称未知）'}"
                f"（mid={info.get('mid')}）。凭据已保存，可以直接用了。"
            )
        if state == "scanned":
            return "用户已扫码，但还没在手机上点「确认登录」。再等一下。"
        if state == "waiting":
            return "还没扫码。把登录链接再发一次给用户：\n" + _base_url() + "/bili/login"
        if state == "expired":
            return "二维码过期了。再调一次 bili_login（action=start）生成新的。"
        return "还没有开始登录。调 bili_login（action=start）拿二维码。"

    # 已经在登录态就不用再折腾用户
    jar = bili_login.load_cookie()
    if jar.get("SESSDATA"):
        cur = bili_login.current()
        who = f"：{cur['uname']}" if cur.get("uname") else ""
        return (
            f"已经登录过了{who}（凭据在 bili_cookie.json）。"
            "如果要用别的账号，删掉那个文件再调一次本工具。"
        )

    try:
        bili_login.start(root)
    except Exception as e:
        return f"错误：生成二维码失败：{e}"

    base = _base_url()
    return (
        "把下面这段**原样**发给用户（链接一个字都别改）：\n\n"
        "---\n"
        "### 需要你扫码登录一下 B站\n\n"
        f"**登录页**：{base}/bili/login\n\n"
        f"![登录二维码]({base}/bili/login/qr.svg)\n\n"
        "用手机「B站」App 扫码，扫完在手机上点「确认登录」。"
        "登录成功后凭据会自动存下来，回来跟我说一声就行。\n\n"
        "（二维码 3 分钟内有效，过期就刷新那个链接）\n"
        "---\n\n"
        "用户说登好了之后，调 bili_login(action=\"status\") 确认一下。"
    )


if getattr(config, "ENABLE_VIDEO", True):
    register(DESCRIPTION, run)
