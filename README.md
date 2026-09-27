# 超星学习通自动刷课脚本（Playwright 版）

用学习通自己的播放器把未完成的【视频】任务点真实播完（默认 2 倍速），不伪造接口；另外可以自动做【学习检测】。章节测验、讨论、文档仍需手动完成。

## 功能
- `chaoxing_auto_course.py`：自动遍历章节树，已完成的任务点跳过；2 倍速 + 硬锁倍速、关掉后台节流、第一次进视频自动起播；弹题答错换下一个选项直到答对，答完点"继续学习"，视频被拉回开头会自动拉回原位；一页多个视频逐段播并逐段核对任务点；播完只等 2 秒，没标上就从头重播一遍；播放中任务点已完成就切下一个视频；遇 9010 风控页停手等你手动过。
- `刷学习检测.py`：不依赖题库，靠平台自己的判分反馈（对 / 半对 / 错）把答案试出来，最多 `max(选项数)+1` 次提交；默认连"做过但没满分"的也重做一遍；平台提示"还有未完成的题目"时点取消，绝不交空卷。

## 环境要求
- Python 3.9+，`pip install -r requirements.txt`（只有 `playwright`）
- 浏览器：自带 Chromium（`playwright install chromium`），或用系统 Chrome / Edge（`--channel chrome|msedge`，免下载）
- Windows 可直接双击 `run.bat`（刷视频）/ `刷学习检测.bat`（做测验），两个都默认用 Edge

## 快速开始
```bash
pip install -r requirements.txt
playwright install chromium                  # 用 --channel chrome/msedge 可跳过

python chaoxing_auto_course.py --url "你的课程章节页网址"   # 刷视频
python 刷学习检测.py --limit 5                             # 做 5 节学习检测
```

课程网址三选一：`--url` / 同目录 `course_url.txt`（推荐，已 gitignore）/ 改脚本顶部 `COURSE_URL`。
首次运行在弹出的窗口里登录学习通——**必须登在这个窗口里**，脚本用的是独立的 `.chaoxing_profile`。两个脚本共用登录态，刷完视频做测验不用再登。

## 常用参数
主脚本：`--rate` 倍速(默认 2.0) / `--channel` `chromium|chrome|msedge` / `--headless` / `--dump` / `--quiz` `auto|bypass|answer|off`。
学习检测：`--limit N` 最多做几节(默认 1，`0` 不限) / `--all` 每章都翻(默认只翻有未完成任务的) / `--chapter 关键字` / `--only-new` 只做没做过的 / `--dry-run` 只读题不提交。
更多开关见脚本顶部配置区（都有中文注释），加参数直接写在 bat 后面，如 `run.bat --channel chrome`。

## 注意事项
- 超星有行为检测，倍速建议不超过 2，尽量别切走或最小化窗口。
- 学习检测枚举答案会留下几次低分提交记录（只取最后一次成绩），介意就别用。
- 启动报 `TargetClosedError`：`.chaoxing_profile` 里已经有浏览器在跑（上次没关干净，或双击了两次 bat）。关掉 Edge/Chrome 窗口或删掉 `.chaoxing_profile` 再跑。
- 卡在"等待登录中 / 没解析到课程章节列表"：前者是登录没登在脚本弹出的窗口里，后者多半是网址不是课程章节页（或已过期）。卡满 3 分钟会自动导出 `调试-页面结构.txt`。
- 运行日志与 `调试-*.txt` 已 gitignore，排错时看它们。仅供学习交流，请勿商用；后果自负。

完整排错手册（倍速、风控、弹题、任务点未完成、学习检测原理等）见 `详细说明.md`。