# 超星学习通自动刷课脚本（Playwright 版）

用学习通自己的播放器把未完成的【视频】任务点真实播完（默认 2 倍速），不伪造学习接口。只做视频，测验/讨论/文档/学习检测请手动完成。

## 功能
- 自动遍历章节树，已完成的任务点自动跳过
- 2 倍速 + 硬锁倍速；关掉后台节流、最小化照常播；第一次进视频自动起播
- 弹题自动作答：答错换下一个选项直到答对、整组答对才点"继续学习"；平台把视频拉回开头会自动拉回原位
- 一页多个视频（情景剧）逐段播，每段单独核对任务点
- 播完核对 `ans-job-finished`：先看 2 秒，没标上就直接从头重播一遍（仍用原倍速）；重播还标不上才记录；播放中任务点已完成就切下一个视频
- 遇 9010 风控验证页立刻停手等你手动过；登录一次记住登录态（`.chaoxing_profile`）
- 只依赖 playwright

## 环境要求
- Python 3.9+，`pip install -r requirements.txt`（只有 `playwright`）
- 浏览器：自带 Chromium（`playwright install chromium`），或用系统 Chrome/Edge（`--channel chrome|msedge`，免下载）
- Windows / macOS / Linux；Windows 可直接双击 `run.bat`（默认用 Edge）

## 快速开始
```bash
pip install -r requirements.txt
playwright install chromium          # 用 --channel chrome/msedge 可跳过
python chaoxing_auto_course.py --url "你的课程章节页网址"
```
课程网址三选一：`--url` / 脚本同目录 `course_url.txt`（推荐，已 gitignore）/ 改脚本顶部 `COURSE_URL`（默认留空）。
首次运行在弹出的窗口里登录学习通，之后自动开始。

## 常用参数
| 参数 | 说明 |
| --- | --- |
| `--url` | 课程章节页网址；不传就读 `course_url.txt` |
| `--rate` | 播放倍速，默认 2.0（更高风险自负） |
| `--channel` | `chromium`(默认) / `chrome` / `msedge` |
| `--headless` | 无头模式（需先登录过一次） |
| `--dump` | 只导出页面结构，排错用 |
| `--quiz` | 弹题处理：`auto`(默认) / `bypass` / `answer` / `off` |

更多开关见脚本顶部配置区（都有中文注释）。加参数直接写在 `run.bat` 后面，如 `run.bat --channel chrome`。

## 注意事项
- 超星有行为检测，倍速建议不超过 2，尽量别切走或最小化窗口。
- 学习检测、章节测验、讨论、文档等非视频任务点不在处理范围内。
- 启动就报 `TargetClosedError`：配置目录 `.chaoxing_profile` 已经有浏览器在跑了（上次没关干净，或双击了两次 `run.bat`）。关掉所有 Edge/Chrome 窗口，或删掉 `.chaoxing_profile` 再跑。
- 运行日志与 `调试-*.txt` 已加入 `.gitignore`，排错时看它们。
- 仅供学习交流，请勿商用；后果自负。

完整排错手册（倍速、风控、弹题、任务点未完成等）见 `详细说明.md`。
