"""
toolbox.py —— 工具箱（本项目第五块积木）

它管三件事：
  1. 【注册表】所有"工具"的登记处
  2. 【调度员】执行模型要求的工具调用
  3. 【自动加载】启动时把 tools/ 文件夹里的每个 .py 都执行一遍

★ 先说清楚"工具"到底是什么：
  大模型自己不会读文件、也不会写文件。它唯一的能力是"输出文字"——
  所以它只能通过"说话"来请求干活，比如：
      "帮我调用 read_file，参数是 {"path": "README.md"}"
  真正动手的是我们写好的 Python 函数。
  所以一个"工具"由两部分组成：
      ① 说明书（name/description/parameters）—— 发给模型看，帮它判断什么时候该用
      ② 执行函数（run）                      —— 模型点名时，我们真正去执行

★ 为什么要有注册表？（这就是给未来留的"空缺"）
  不管是今天本地写的工具，还是将来从 MCP 服务器拉来的工具、
  从 Skill 带来的工具——只要按同样的格式"登记"进来，
  引擎调用它们时就一视同仁、代码一行都不用改。
"""

import importlib
import pkgutil
import threading
from pathlib import Path

import hooks

# 注册表：{ 工具名: {"description": 说明书字典, "run": 执行函数, "source": 来源} }
_registry = {}

# 项目根目录（toolbox.py 所在的文件夹）。
# "纯名字沙箱"（用户只输入一个名字时）会建在它的 sandboxes/ 子目录下。
PROJECT_ROOT = Path(__file__).resolve().parent


# ============================================================
# 工作区管理（agent 能在哪个目录里干活）
# ------------------------------------------------------------
# 每个"对话"可以有一个自己的工作区（由用户在界面上设定）：
#   - 没设定过 → 默认在项目目录里干活
#   - 设定过   → 所有文件工具只能在这个目录里干活（路径锁锁在它上面）
#
# ★ 为什么用"线程本地"（threading.local）来记？
#   服务器是多线程的（每个请求一个线程），两个对话可能同时进行——
#   如果用普通全局变量记录"当前工作区"，两个请求会串台。
#   threading.local 相当于给每个线程发了一个自己的小本本，互不干扰。
# ============================================================

_local = threading.local()


def set_workspace(root):
    """记录"本线程当前对话用哪个工作区"（engine 每次对话开始时调用）。

    参数 root 是目录路径字符串；传 None 表示"恢复默认"（项目目录）。
    """
    _local.root = root


def current_workspace():
    """读取本线程的当前工作区；没设置过返回 None（工具会回退到项目目录）。"""
    return getattr(_local, "root", None)


def dangerous_reason(path):
    """检查一个目录是不是"太宽或太敏感"、不适合当工作区。

    这是"防手滑"的兜底：用户万一输入了 D:\\ 或整个用户主目录，
    工具们不应该真的在那里放开手脚。
    返回一句人话的拒绝理由；没问题返回 None。
    """
    p = Path(path).resolve()

    # 1) 盘符根目录（比如 D:\，它的父目录就是它自己）
    if p.parent == p:
        return "这是盘符根目录，范围太大了"

    # 2) 用户主目录本身（子目录没问题，整个主目录太宽）
    if p == Path.home():
        return "这是你的用户主目录，范围太大了"

    # 3) 系统目录
    for system_dir in (Path("C:/Windows"), Path("C:/Program Files"), Path("C:/Program Files (x86)")):
        if p == system_dir:
            return "这是系统目录，不允许作为工作区"

    return None


def prepare_workspace(path):
    """准备一个工作区目录：先检查合规，不存在就创建。

    由 server.py 的 /api/workspace 接口调用（用户界面上设定工作区时）。
    返回 (真实路径, 是否新建)；不合规会抛 ValueError（消息直接给用户看）。
    """
    if not path or not path.strip():
        raise ValueError("路径不能为空")

    raw = path.strip()

    # 小贴心：只输入一个"名字"（没有路径分隔符）→ 当作"在项目下新建沙箱"，
    # 自动补全成 <项目目录>/sandboxes/<名字>
    if not any(ch in raw for ch in ("/", "\\", ":")):
        raw = str(PROJECT_ROOT / "sandboxes" / raw)

    p = Path(raw).resolve()

    # 合规检查（防手滑）
    reason = dangerous_reason(p)
    if reason:
        raise ValueError(f"这个目录不能作为工作区：{reason}")

    if p.exists() and not p.is_dir():
        raise ValueError("这个路径已经存在，但它不是目录（是个文件）")

    created = False
    if not p.exists():
        try:
            p.mkdir(parents=True)   # 连父目录一起创建（比如新建 sandboxes/xxx）
            created = True
        except OSError as e:
            raise ValueError(f"目录创建失败：{e}")

    return str(p), created


# ============================================================
# 工具禁用名单（子代理防"套娃"用）
# ------------------------------------------------------------
# 子代理干活期间，它的工具箱里不该再出现 spawn_agent——
# 不然它可能自己再派子代理、层层嵌套（烧钱无底洞）。
# 和"工作区"一样用线程本地：只影响正在跑子代理的那一个线程，
# 其他对话（其他线程）不受任何影响。
# ============================================================

def set_disabled(names):
    """设置"本线程要禁用哪些工具"，返回修改前的名单（用于之后恢复）。

    传一个集合（如 {"spawn_agent"}）；传空集合或 None 表示"取消禁用"。
    典型用法（照抄 spawn_agent 工具里的写法）：
        old = set_disabled({"spawn_agent"})
        try:
            ...干活...
        finally:
            set_disabled(old)      # 干完必须恢复（用 finally 保证）
    """
    prev = getattr(_local, "disabled", None)
    _local.disabled = set(names) if names else set()
    return prev


def disabled_names():
    """读取本线程当前的禁用名单（返回一个集合，没设置过就是空集合）。"""
    return getattr(_local, "disabled", None) or set()


# ============================================================
# 工具"附件"（随结果捎带给上层的数据）
# ------------------------------------------------------------
# 工具平时只返回一个字符串（给模型看的结果）；但有时还想捎带一些
# "结构化数据"给界面和日志用——比如 spawn_agent 想把子代理的完整
# 对话（几十条记录）带回去，前端才能"展开看过程"。
# 全塞进那个字符串里太丑，所以设计了这个小机制：
#   ① 工具执行期间调 attach(data) 挂一份数据；
#   ② engine 执行完这个工具后调 take_attachments() 取走；
#   ③ 数据跟着 tool_result 事件流向对前端、写进事件日志。
# 同样是线程本地（一个线程同一时刻只跑一个工具，不会串台）。
# ============================================================

def attach(data):
    """给"当前这一次工具执行"捎带一份结构化数据（见上方说明）。"""
    _local.attachments = data


def take_attachments():
    """取走本线程的附件并清空（engine 每次工具执行后取一次）。"""
    data = getattr(_local, "attachments", None)
    _local.attachments = None
    return data


def register(description, run, source="builtin"):
    """登记一个工具（每个工具文件在末尾调用它）。

    参数：
      description —— 工具说明书，字典格式：
                     {"name": "...", "description": "...", "parameters": {...}}
      run         —— 真正干活的函数：接收一个参数字典，返回一个字符串。
                     约定：成功返回结果文字；失败返回以"错误"开头的说明
                     （不要抛异常——让模型看到错误、自己调整）
      source      —— 工具来源："builtin"（本地自带）；
                     将来接 MCP 时会是 "mcp:服务器名"，接 Skill 时是 "skill:技能名"
    """
    _registry[description["name"]] = {
        "description": description,
        "run": run,
        "source": source,
    }


def all_descriptions():
    """取出所有工具的"说明书"，转换成 API 要求的格式，发给模型。

    ★ 注意这里做了一次"包装"（第一次测试时踩过坑）：
      API 要求的每条工具长这样——
          {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
      而工具文件里写的是里面那一层（更简洁、更好写）。
      这个"内部格式 → API 格式"的转换层就固定放在这里——
      将来接 MCP 时也一样：远端工具的格式先转换成本地格式，
      到这里再统一包一层，引擎不用关心工具从哪来。

    ★ 禁用名单里的工具不会出现在这里——子代理"看不到"被禁的工具
      （看不见就不会用，这是防套娃的第一道门）。
    """
    disabled = disabled_names()
    return [
        {"type": "function", "function": item["description"]}
        for name, item in _registry.items()
        if name not in disabled
    ]


def has(name):
    """注册表里有没有叫这个名字的工具？"""
    return name in _registry


def call(name, arguments):
    """执行一个工具调用，返回结果字符串。

    arguments 是模型给出的参数字典（从它输出的 JSON 字符串解析而来）。
    这里的任何异常都会被转成"给模型看的错误文本"——
    让模型自己看到错误、自己调整策略，而不是让整个对话崩掉。

    注：工作区（agent 在哪个目录干活）由 engine 在对话开始时通过
    set_workspace() 记录，文件工具里的 safe_path() 会自动读取。
    """
    if name not in _registry:
        return f"错误：没有叫「{name}」的工具"

    # 禁用名单再拦一道：即使模型"硬要调"被禁的工具，也不给执行
    # （防套娃的第二道门——第一道是它的说明书根本不发给模型）
    if name in disabled_names():
        return f"错误：工具「{name}」当前不可用"

    # 路过预留的"工具执行前"空槽（将来的权限确认、审计插件挂在这里）
    ctx = {"name": name, "arguments": arguments, "blocked": None}
    hooks.run(hooks.before_tool, ctx)
    if ctx["blocked"]:
        return f"工具调用被拦截：{ctx['blocked']}"

    try:
        return _registry[name]["run"](arguments)
    except Exception as e:
        return f"错误：工具执行失败：{e}"


def load_tools():
    """自动加载 tools/ 文件夹里的所有工具。

    原理和 plugins/ 一模一样：把每个 .py 文件"执行"一遍，
    文件末尾那行 register(...) 就会把工具登记进注册表。
    所以以后加工具 = 往 tools/ 扔一个文件，别的都不用管。
    """
    import tools as tools_package

    for info in pkgutil.iter_modules(tools_package.__path__):
        if info.name.startswith("_"):   # 跳过 __init__ 和 _common 这类内部文件
            continue
        importlib.import_module(f"tools.{info.name}")
        print(f"[工具] 已加载：{info.name}")
