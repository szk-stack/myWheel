"""
cli.py —— 命令行模式（学 Pi 的 -p / --json）

    python cli.py "把这个视频转成文字"          # 边跑边看（工具过程打到 stderr）
    python cli.py -p "..."                      # 只出最终答案（适合管道）
    python cli.py --json "..."                  # 每行一个事件 JSON（适合脚本）
    echo "问题" | python cli.py -              # 从 stdin 读
    python cli.py -w D:/some/dir "..."          # 指定工作区
    python cli.py --full-access "..."           # 允许跑出工作区

为什么单独写一个入口而不是给 server.py 加参数：
  引擎本来就是**生成器**（chat_stream 吐事件），网页只是它的一个消费端。
  命令行是第二个消费端——同一套引擎、同一套工具、同一套插件，
  只是把事件打到终端而不是 SSE。这样"能不能被脚本调用"这件事
  就不需要改引擎一行代码。

★ 三条约定，和 Pi 一致：
  1. 正文走 stdout，**过程信息走 stderr** —— 这样 `| grep` / `> file`
     只会拿到干净的答案，不会被"正在调用工具…"污染。
  2. `--json` 时 stdout 每行一个 JSON 对象，方便 jq 之类处理。
  3. 退出码：0 成功，1 出错（可以被 shell 判断）。
"""

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

import config
import engine
import keyring
import toolbox


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="mywheel",
        description="myWheel 命令行模式：一句话交给 agent，它自己调工具干完。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python cli.py \"把 B站上这个视频转成文字 BV1xx411c7mD\"\n"
            "  python cli.py -p \"总结 knowledge/ 里所有文档\" > 摘要.md\n"
            "  echo \"现在几点了\" | python cli.py -\n"
        ),
    )
    p.add_argument("prompt", nargs="*", help="要做的事；传 - 表示从 stdin 读")
    p.add_argument("-p", "--print", dest="quiet", action="store_true",
                   help="只输出最终答案（不显示工具调用过程）")
    p.add_argument("--json", action="store_true",
                   help="每行输出一个事件 JSON（text / tool_call / tool_result）")
    p.add_argument("-w", "--workspace", default=None,
                   help="工作区目录（默认：当前目录）")
    p.add_argument("--project", action="store_true",
                   help="用项目目录当工作区（而不是当前目录）")
    p.add_argument("--full-access", action="store_true",
                   help="允许读写工作区以外的文件（同网页上的开关）")
    p.add_argument("--max-rounds", type=int, default=None,
                   help="最多执行几轮工具（默认取 config，0 = 不限）")
    return p.parse_args(argv)


def resolve_prompt(args, stdin_text=None):
    """把 prompt 参数拼成一句话；"-" 或空则从 stdin 读。"""
    raw = " ".join(args.prompt).strip()
    if raw and raw != "-":
        return raw
    text = stdin_text if stdin_text is not None else sys.stdin.read()
    return (text or "").strip()


def _workspace_of(args):
    """工作区：默认用**当前目录**（命令行工具的直觉），--project 才回项目根。"""
    if args.project:
        return str(Path(__file__).resolve().parent)   # cli.py 就在项目根
    return str(Path(args.workspace).resolve()) if args.workspace else str(Path.cwd())


def _setup_stdout():
    """Windows 控制台默认 GBK，中文/符号会炸；统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    _setup_stdout()
    # 和网页端用同一份 Key：界面里保存过的，优先于 config.py 里的默认值
    keyring.load_into_config()
    args = parse_args(argv)
    prompt = resolve_prompt(args)

    if not prompt:
        print("错误：没有给要做的事（可以传一个字符串，或用 - 从 stdin 读）", file=sys.stderr)
        return 2

    if not (config.API_KEY or "").strip():
        print("错误：还没填 API_KEY（可在网页左下角「API Key」里配置，或填进 config.py）", file=sys.stderr)
        return 2

    if args.max_rounds is not None:
        # 命令行临时覆盖，不动 config 文件
        config.MAX_TOOL_ROUNDS = args.max_rounds

    # ★ 必须显式加载工具：server.py 是在启动时加载的，
    #   CLI 没有那个启动流程——漏了这行，模型会拿到**空工具列表**，
    #   然后很客气地告诉你"我没法访问文件"。实测踩过一次。
    #
    # ★ 加载时工具箱会打印"[工具] 已加载：xxx"。这些是诊断信息，必须赶到
    #   stderr 去——否则 `> file` / `| grep` 会把它们当成答案的一部分。
    with contextlib.redirect_stdout(sys.stderr):
        toolbox.load_tools()

    messages = [{"role": "user", "content": prompt}]
    workspace = _workspace_of(args)
    conv_id = "cli-" + time.strftime("%Y%m%d-%H%M%S")

    if not args.json and not args.quiet:
        print(f"工作区：{workspace}", file=sys.stderr)

    final_text = []
    had_error = False

    try:
        stream = engine.chat_stream(
            messages,
            workspace=workspace,
            conversation_id=conv_id,
            trace_source="cli",
            full_access=args.full_access,
        )

        for event in stream:
            kind = event.get("type")

            if args.json:
                # 每行一个事件；text 增量原样给出去，脚本自己拼
                print(json.dumps(event, ensure_ascii=False), flush=True)

            if kind == "text":
                final_text.append(event.get("text", ""))
                if not args.quiet and not args.json:
                    sys.stdout.write(event.get("text", ""))
                    sys.stdout.flush()

            elif kind == "tool_call" and not args.quiet and not args.json:
                name = event.get("name", "?")
                print(f"\n  → {name}", file=sys.stderr, flush=True)

            elif kind == "tool_result" and not args.quiet and not args.json:
                ok = event.get("ok", True)
                summary = str(event.get("result", "")).splitlines()[0][:70]
                mark = "✓" if ok else "✗"
                print(f"  {mark} {summary}", file=sys.stderr, flush=True)

            elif kind == "compacted" and not args.quiet and not args.json:
                print("\n  （上下文已自动压缩）", file=sys.stderr, flush=True)

            elif kind == "error":
                had_error = True
                print(f"\n错误：{event.get('message')}", file=sys.stderr)

    except KeyboardInterrupt:
        print("\n（已中断）", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    if args.quiet:
        # -p 模式：stdout 只留最终答案
        print("".join(final_text))
    elif not args.json:
        print()      # 收尾换行

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
