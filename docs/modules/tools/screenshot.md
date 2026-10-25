# 工具：screenshot（网页截图）

| 状态 | 最后更新 | 关联代码 | 代码基线 |
|---|---|---|---|
| Stable | 2026-10-24 | `tools/screenshot.py` | `a647e30` |

> 所属：[工具系统总览](README.md)

## 1. 职责

用 **Edge 无头模式（headless）** 给一个本地 HTML 文件或网址截图（PNG），
存到工作区的 `.screenshots/` 目录。Windows 自带 Edge，**零额外依赖、
不弹浏览器窗口**。

配合 [view_image](view_image.md) 构成自检闭环：
`screenshot` 截 → `view_image` 看 → 模型发现渲染问题 → 改代码 → 再截。

## 2. 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `target` | string | 是 | 工作区内的 `.html` 路径，或 `http(s)://` 网址 |
| `width` / `height` | integer | 否 | 视口尺寸（默认 1200 × 800） |
| `wait_ms` | integer | 否 | 页面渲染等待的**虚拟毫秒**（默认 1500；动画/游戏可调大） |

## 3. 行为与输出

- 本地文件经 `safe_path` 校验（工作区内）→ 转 `file://` URL（中文路径自动编码）；
- 调用 `msedge --headless=new --screenshot=… --window-size=… --virtual-time-budget=…`；
- **临时 profile**：每次用独立临时目录（避免和用户正在用的 Edge 冲突、不污染），
  截完即删；
- 输出：`.screenshots/shot-20261025-183258.png`（文件名带时间戳）；
- 返回文字：`截图已保存：.screenshots/xxx.png（1200x800，6 KB）。想看效果请调用 view_image 查看这张图。`

## 4. 错误路径

| 情况 | 返回 |
|---|---|
| 找不到 Edge | `错误：没找到 Microsoft Edge（截图需要它）` |
| 页面不存在 / 路径越界 | `错误：…` |
| 截图超时（90 秒） | `错误：截图超时——页面可能卡住了` |
| 截图文件没生成 | `错误：截图没有生成（Edge 输出：…）` |

## 5. 实现要点与坑

- **`--virtual-time-budget`**：让页面在"虚拟时间"下快速推进 N 毫秒再截——
  对 `requestAnimationFrame` 的动画/游戏特别有用（不用真的等 N 毫秒）；
- **`.screenshots/` 是隐藏目录**：`list_dir` 跳过 `.` 开头的目录，
  不会干扰模型浏览工作区；截图文件不自动清理（小文件，手动删即可）；
- **只截"静态一帧"**：截图是某个时刻的画面，不能验证交互行为
  （点击、键盘操作需要更重的方案，如 Playwright——超出零依赖范围）；
- Edge 路径按常见安装位置探测（`Program Files (x86)` / `Program Files` /
  PATH 里的 `msedge`）；换浏览器需改 `EDGE_CANDIDATES`。
