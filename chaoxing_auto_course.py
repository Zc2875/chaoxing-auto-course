#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
超星学习通网页自动刷课脚本（Playwright 版）

功能: 打开课程章节页, 自动把本课程所有未完成的【视频】任务点看完(可调倍速)。
特点: 视频由平台自己的播放器真实播放并上报进度, 不伪造任何学习接口;
      登录一次后登录态保存在脚本目录下的 .chaoxing_profile 中, 下次免登录。

环境: Python 3.9+ 和 playwright(唯一的第三方依赖, 见 requirements.txt);
      系统装了 Chrome/Edge 可以加 --channel chrome/msedge, 省掉 Chromium 下载。

用法:
    pip install -r requirements.txt
    playwright install chromium        # 用 --channel chrome/msedge 时可跳过
    python chaoxing_auto_course.py

常用参数:
    --url      目标课程网址(不传就读脚本同目录的 course_url.txt)
    --rate     视频播放倍速(默认 2.0, 可以 --rate 3.0 但风险自负)
    --headless 无头模式(仅完成过一次登录后可用)

注意事项:
    1. 仅供学习交流, 请勿商用; 使用后果由使用者自行承担。
    2. 超星有学习行为检测, 建议倍速不超过 2, 且不要切走/最小化浏览器窗口。
    3. 章节测验、讨论、文档、"学习检测"等非视频任务点不在处理范围内, 请手动完成。
    4. 一章里可能有好几张卡片(安全知识 / 学习检测 / ...), 脚本会逐张卡片找视频。
    5. 脚本内置防切屏补丁 + 播放期间自动锁倍速, 但浏览器窗口放前台最稳妥。
'''

import argparse
import logging
import random
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# ============================================================== 配置区
# 课程网址从哪来(优先级从高到低):
#   1) 运行时 --url "https://mooc1.chaoxing.com/mycourse/studentstudy?..."
#   2) 脚本同目录的 course_url.txt(整个文件就一行网址, 已被 .gitignore 排除)
#   3) 直接改下面这一行 COURSE_URL
# 这里故意留空: 公开仓库里不带任何真实课程参数(网址里的 cpi/enc/openc 虽然
# 是会过期的一次性参数, 也没必要放到网上)。
COURSE_URL = ''
COURSE_URL_FILE = Path(__file__).resolve().parent / 'course_url.txt'

PLAYBACK_RATE = 2.0           # 视频播放倍速
DWELL_BETWEEN_UNITS = (2, 6)  # 两个任务点之间的随机停留(秒)
DWELL_AFTER_OPEN = (3, 8)     # 打开任务点后开始操作前的随机等待(秒)
STALL_SECONDS = 25            # 视频进度连续停滞多久视为异常(秒)
MAX_TASK_MINUTES = 30         # 单个任务点的最长处理时间(分钟)
HARD_LOCK_RATE = True         # 硬锁倍速: 连播放器自己把倍速改回来也拦得住
RATE_LOCK_INTERVAL = 5        # 播放过程中每隔几秒重新锁一次倍速
KEEP_ALIVE = True             # 反切屏: 屏蔽失焦/隐藏检测, 自动点掉"继续学习"遮罩
KEEP_ALIVE_INTERVAL = 2       # 播放过程中每隔几秒做一次保活
LOST_GRACE = 12               # 播放器元素消失多久算"这一段播完了"(秒)
HANDLE_ALL_CARDS = True       # 一章有多张卡片时逐张处理(视频常在非第一张卡片里)
CARD_TITLES_TO_SKIP = ('学习检测',)   # 名字含这些字的卡片直接跳过(按要求不动学习检测)

# 视频弹题: 超星会在视频播到中途弹出随堂提问, 不处理就会一直卡在那里
VIDEO_QUIZ = 'auto'           # auto=先尝试绕开, 绕不开就作答 | bypass=只绕开 | answer=直接作答 | off=不处理
QUIZ_CHOICE = 1               # 作答时点第几个选项(从 1 开始)
QUIZ_COOLDOWN = 6             # 判定为弹题后多少秒内不重复处理
QUIZ_PAUSE_WAIT = 5           # 视频被暂停持续这么久才怀疑是弹题, 免得白扫 DOM
QUIZ_KINDS = ('videoquiz', 'quiz', 'question', 'tiwen', 'exercise', 'marking',
              'interact', 'hudong', 'popup-quiz', 'quizbox')
QUIZ_SCAN_INTERVAL = 6         # 播放过程中每隔几秒主动扫一次(互动测验有时不暂停视频)
QUIZ_VERIFY_WAIT = 2.5         # 作答提交后等几秒, 再读平台反馈(回答正确/错误)
QUIZ_MAX_TRIES = 8             # 一道题最多试几个选项(答错就换下一个, 直到答对)
RESUME_AFTER_QUIZ = True       # 答完题后平台常把视频拉回开头, 自动拉回原位接着播
QUIZ_SEEK_MAX = 3              # 一段视频最多这样拉回几次, 免得跟平台来回对拉
QUIZ_SEEK_HOLD = 20            # 拉回是"粘"的: 在页面里持续压住进度这么多秒, 平台抹一次就再压一次
QUIZ_SEEK_VERIFY = 5           # 拉回/推到结尾之后, 最多等几秒确认真的到位
QUIZ_SEEK_MARGIN = 8.0         # 同一道题又被弹回来时, 拉回的位置往后让一点, 免得刚拉回去又把它勾出来
QUIZ_PAUSE_VIDEO = True        # 扫到弹题先把视频停住: 边答题边播, 2 倍速下几秒就冲到结尾
QUIZ_END_RATIO = 0.9           # 弹题落在视频最后 10% 时, 答完直接推到结尾, 不拉回原位
QUIZ_END_GIVEUP = 2            # 末尾弹题被平台反复拉回超过这个次数就收手, 不再重播这一段
QUIZ_GROUP_MAX = 5             # 一组互动测验最多按这个题数去答(整组答完才敢点"继续学习")
QUIZ_SETTLE = 15              # 刚答完题/刚拉回进度后多少秒内, 不把"进度不动"当成卡住
QUIZ_DUMP_ALWAYS = True        # 一检测到弹题就把现场写到 调试-弹题.txt

# 任务点完成核对: "视频播完"和"任务点完成"是两件事。
# 超星的做法是视频 iframe 播完给父页面发一条 postMessage(JOB_FINISH_INFO),
# 父页面收到才给任务点加上 ans-job-finished —— 这条消息一旦丢了, 就成了
# "视频播完了、弹题也过了, 但任务点显示未完成"。所以播完不能马上走:
#   1) 播完只等 JOB_FINISH_WAIT_FIRST 秒, 看 ans-job-finished 出来没有;
#   2) 没出来就不折腾了, 直接从头把这一段重播一遍(重播也是真实播放);
#   3) 重播完还是没有, 才如实报出来, 并把现场写到 调试-任务点未完成.txt
JOB_FINISH_CHECK = True       # 播完后核对任务点到底有没有被标成完成
JOB_FINISH_WAIT_FIRST = 2     # 播完只等这几秒, 平台没标上就直接从头重播, 不干等
JOB_TAIL_WAIT = 8             # 停在最后一秒时, 最多等多少秒等它发出"看完"通知

# 播放中提前收尾: 超星有时在这段视频还没播到结尾时, 就已经把它的任务点标成"已完成"
# (比如这一段之前已经看够, 或者补播那一次的上报刚到)。这时候没必要继续等它演完 ——
# 直接收尾去下一个视频, 省时间, 也少播一段免得被行为检测盯上。
JOB_EARLY_EXIT = True         # 播放中检测到这个视频"任务点已完成"就自动切下一个视频
JOB_EARLY_INTERVAL = 5        # 播放中每隔几秒核对一次当前这个视频的任务点

# 重播倍速: 第一遍已经正常倍速看完了, 平台却没把任务点标成"已完成"时, 重播这一段
# 用多少倍速 —— 设成 0 就跟着视频本身的倍速走(默认 x2.0), 不做额外加速。
# 不加速更稳: 补播也是真实观看行为, 倍速越高越容易被行为检测盯上。
RETRY_RATE = 0                # "任务点没标上"时重播这一段用的倍速(0 = 跟随原倍速, 默认 x2.0)

# 后台播放: 关掉 Chromium 的"窗口被遮挡/最小化就降级"逻辑
# 少了这几个开关, 只要浏览器被别的窗口盖住或最小化, 计时器会被降频、视频会被暂停
BACKGROUND_PLAYBACK = True

# 自动起播: 往页面里装一个常驻补丁, 谁暂停就给谁补一次 play()
# 专治"第一次点进视频停在播放按钮上"和"切走再回来视频就停了"
AUTOPLAY_POKE = True
POKE_INTERVAL_MS = 800         # 补播检查间隔(毫秒), 只在有 video 暂停时才真的动手

# 一个页面可能排了多个视频(情景剧/合辑): 首页的卡片里往往是一整集拆成好几段,
# 必须按顺序一段一段播完, 跳着播会被平台拦(会报"学习顺序"类错误)
MULTI_VIDEO_PER_PAGE = True
MULTI_PAGE_VIDEO_LIMIT = 40      # 单张卡片里最多连续播几个视频, 防止死循环
MULTI_PAGE_VIDEO_MISSES = 4      # 页面说还有任务点没做完但暂时找不到视频时, 再等几轮
MULTI_PAGE_VIDEO_PENDING = 2     # 某一段"播完了平台却没标任务点"时, 最多回头补播几遍

# 超星风控: 触发后整页会跳到验证码页(错误码 9010 操作异常), 脚本必须停手等人工过验证
ANTISPIDER_WAIT_MINUTES = 10

# 判定“当前页是学习通登录页”的特征(命中任意一个就算登录页)
LOGIN_URL_MARKS = ('passport', '/login', 'login?', 'login.', 'tologin')
FINISHED_MARKS = ('已完成', '完成')

# 页面选择器: 超星页面改版后只需调整这里
# 章节树容器候选, 按顺序尝试
UNIT_CONTAINER_CANDIDATES = [
    "#coursetree",
    ".periodList",
    ".chapter_item",
]
# 章节树"真的渲染好了"才认的信号(超星先给一个空容器, 再异步填内容)
READY_SELECTORS = [
    "#coursetree div.posCatalog_select",
    ".periodList li",
    ".chapter_item li",
]
# 树里的"任务点节点"候选(在容器内查找), 按顺序尝试
UNIT_NODE_CANDIDATES = [
    "div.posCatalog_select",
    "li a.articlename",
    "li span.articlename",
    "li",
]
# 右侧内容区 iframe 的候选选择器
CONTENT_FRAME_CANDIDATES = [
    "iframe#iframe",
    "#frame_content",
    "div.course_main iframe",
    "iframe[info='card']",
]
# 章节内的卡片列表(安全知识 / 学习检测 / ...), 每个 li 就是一张卡片
CARD_LIST_SELECTOR = "#prev_tab .prev_ul li"
# 只关心视频: 走错卡片时切回第一张卡片
CONTENT_TAB_CANDIDATES = ["#dct1"]
# 内容区 iframe 的网址里应含有的特征
CONTENT_FRAME_MARKS = ("knowledge/cards", "frame_content")
SELECTORS = {
    "unit_name": "span.posCatalog_name, a.articlename, span.articlename",
    "unit_state": ".icon_Completed, .state, .unit_status, .status",
    "unfinish_count": ".jobUnfinishCount",
    "video": "video",
}


PROFILE_DIR = Path(__file__).resolve().parent / '.chaoxing_profile'
LOG_FILE = Path(__file__).resolve().parent / '运行日志-超星刷课.txt'

log = logging.getLogger('chaoxing')


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
        ],
    )


def _looks_like_antispider(url: str) -> bool:
    '''判断某个网址是不是超星的风控验证页'''
    text = (url or '').lower()
    for mark in ('antispider', 'processverify', 'showverify'):
        if mark in text:
            return True
    return False


def _fill_js(template: str, **kwargs) -> str:
    '''把 JS 模板里的 __XXX__ 占位符替换成 Python repr 后的值'''
    for name, value in kwargs.items():
        template = template.replace('__%s__' % name.upper(), repr(value))
    return template

# 防切屏补丁: 让页面一直以为自己"可见 + 有焦点", 并挡掉所有失焦事件
# 超星的 switchwindow 检测就是靠 blur / visibilitychange 暂停视频的
_KEEP_ALIVE_BODY = """
  try { window.onblur = null; window.onpagehide = null; } catch (e) {}
  if (window.__cx_alive_done) return 2;
  window.__cx_alive_done = 1;
  const block = (e) => { try { e.stopImmediatePropagation(); } catch (err) {} };
  const evts = ["blur", "visibilitychange", "pagehide", "freeze", "mouseleave"];
  for (const name of evts) {
    try { window.addEventListener(name, block, true); } catch (e) {}
    try { document.addEventListener(name, block, true); } catch (e) {}
  }
  try {
    Object.defineProperty(document, "hidden", {configurable: true, get: () => false});
    Object.defineProperty(document, "visibilityState", {configurable: true, get: () => "visible"});
    Object.defineProperty(document, "webkitHidden", {configurable: true, get: () => false});
    Object.defineProperty(document, "webkitVisibilityState", {configurable: true, get: () => "visible"});
  } catch (e) {}
  try { document.hasFocus = () => true; } catch (e) {}
  try {
    Object.defineProperty(navigator, "webdriver", {configurable: true, get: () => undefined});
  } catch (e) {}
  return 1;
"""

# evaluate() 版本: Playwright 的 evaluate 传函数表达式最稳
_KEEP_ALIVE_JS = "() => {" + _KEEP_ALIVE_BODY + "}"
# add_init_script() 版本: 那里是当普通脚本执行的, 必须写成 IIFE
_KEEP_ALIVE_BOOT = "(() => {" + _KEEP_ALIVE_BODY + "})()"

# 自动起播补丁: 首次进视频时播放器会停在"点击播放", 后台/被遮挡时它也会自己暂停
# 这里的做法是常驻一个扫描: 发现 <video> 被暂停且没播完, 就补一次 play(),
# 并挂上 canplay / pause / stalled 等事件, 事件一到就再补一次
# 弹题浮层在前台时不补播, 免得把随堂提问顶掉(那个交给 handle_video_quiz)
_POKE_BODY = """
  try { if (window.__cx_poke_timer) return 3; } catch (e) {}
  const quizUp = () => {
    // 弹题浮层在前台时不补播: 抢着 play() 会把随堂提问顶掉,
    // 结果是这段视频"看完了"但互动测验没答, 任务点根本不算完成
    let vr = null;
    try {
      const v0 = document.querySelector("video");
      if (v0) vr = v0.getBoundingClientRect();
    } catch (e) {}
    const hit = (el) => {
      let r = {width: 0, height: 0};
      try { r = el.getBoundingClientRect(); } catch (e) { return false; }
      if (r.width <= 120 || r.height <= 60) return false;
      if (!vr) return true;
      // 离播放器很远的面板(藏在角落的模板之类)不算, 免得把播放一直挡着
      const ox = Math.min(r.right, vr.right) - Math.max(r.left, vr.left);
      const oy = Math.min(r.bottom, vr.bottom) - Math.max(r.top, vr.top);
      return ox > 40 && oy > 30;
    };
    try {
      const sel = "[class*=videoquiz], [class*=video-quiz], [class*=quiz]," +
        " [class*=question], [class*=tiwen], [class*=exercise]," +
        " [class*=marking], [class*=interact], [class*=hudong]," +
        " [class*=tkTopic], [class*=tkScroll], [id*=quiz]";
      for (const el of document.querySelectorAll(sel)) {
        if (hit(el)) return true;
      }
    } catch (e) {}
    return false;
  };
  const poke = () => {
    // 没被显式打开就什么都不做: 一页多播放器时绝不能一起起播
    if (!window.__cx_poke_on) return;
    if (quizUp()) return;
    let target = null;
    for (const v of document.querySelectorAll("video")) {
      if (v.ended) continue;
      // 已经播到结尾的不要碰, 否则会把整段视频重新播一遍
      if (v.duration && v.currentTime >= v.duration - 0.5) continue;
      // 一页多个视频(情景剧)时只动第一个: 后面的被平台按顺序锁着, 抢跑会报错
      target = v;
      break;
    }
    if (!target) return;
    try { target.muted = true; } catch (e) {}
    // 没有"暂停中的视频"时不要点大播放按钮, 免得播完的视频被重新拉起来
    if (!target.paused) return;
    try {
      const pr = target.play();
      if (pr && pr.catch) pr.catch(() => {});
    } catch (e) {}
    const btn = document.querySelector(".vjs-big-play-button");
    if (btn) {
      try {
        const r = btn.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) btn.click();
      } catch (e) {}
    }
  };
  const hook = (v) => {
    if (!v || v.__cxPokeHooked) return;
    v.__cxPokeHooked = true;
    const again = () => setTimeout(poke, 200);
    const evts = ["loadedmetadata", "canplay", "canplaythrough", "pause",
                  "stalled", "waiting", "suspend", "abort", "emptied"];
    for (const name of evts) {
      try { v.addEventListener(name, again); } catch (e) {}
    }
  };
  const scan = () => {
    const list = document.querySelectorAll("video");
    for (const v of list) hook(v);
    return list.length;
  };
  scan();
  poke();
  try {
    window.__cx_poke_timer = setInterval(() => { scan(); poke(); }, __POKE_MS__);
  } catch (e) {}
  try {
    if (!window.__cx_poke_mo) {
      window.__cx_poke_mo = new MutationObserver(() => { scan(); poke(); });
      window.__cx_poke_mo.observe(document.documentElement,
                                  {childList: true, subtree: true});
    }
  } catch (e) {}
  return 1;
"""

# evaluate() 版本: Playwright 的 evaluate 传函数表达式最稳
_POKE_JS = "() => {" + _POKE_BODY + "}"
# add_init_script() 版本: 那里是当普通脚本执行的, 必须写成 IIFE
_POKE_BOOT_TEMPLATE = "(() => {" + _POKE_BODY + "})()"
# 把间隔真正填进去; 补丁是幂等的(靠 __cx_poke_timer 挡重复安装)
_POKE_BOOT = _fill_js(_POKE_BOOT_TEMPLATE, poke_ms=POKE_INTERVAL_MS)
_POKE_JS = _fill_js(_POKE_JS, poke_ms=POKE_INTERVAL_MS)

# 每个 iframe 都会装一份补丁, 但默认全部关着: 只给"正在看的那个播放器"单独打开
# 否则一页 7 个播放器会被同时 play(), 平台立刻判操作异常(9010 验证码)
_POKE_ON_JS = '() => { window.__cx_poke_on = true; return 1; }'
_POKE_OFF_JS = '() => { window.__cx_poke_on = false; return 0; }'
# 反查: 一个 <video> 自己的 window 上挂着哪个序号, 就说明它属于哪个 frame
_FRAME_TAG_READ_JS = 'v => (v.ownerDocument.defaultView || {}).__cx_frame_tag'

# 遮罩清理: 超星检测到切屏/异常时会弹"继续学习"之类的浮层, 这里自动点掉
_DISMISS_JS = """
() => {
  let n = 0;
  const vis = (el) => {
    if (!el) return false;
    try {
      const r = el.getBoundingClientRect();
      const st = window.getComputedStyle(el);
      return r.width > 0 && r.height > 0 &&
             st.display !== "none" && st.visibility !== "hidden";
    } catch (e) { return false; }
  };
  const fp = document.querySelector("#freezePage");
  if (vis(fp)) {
    const btn = fp.querySelector(".keepLearning") || fp.querySelector(".bluebtn02");
    if (vis(btn)) { try { btn.click(); n++; } catch (e) {} }
    try { if (typeof continue_learning === "function") { continue_learning(); n++; } } catch (e) {}
  }
  // 只点"盖在播放器上的浮层"里的继续按钮: 页面上别处的同名文字不碰
  // 特别注意: 绝不能点"重新播放" —— 那会把整段视频从头再播一遍(末尾弹题时会死循环)
  const words = ["继续学习", "继续播放", "继续观看", "继续看", "继续听课",
                 "继续播放视频"];
  const BOX = "[class*=tkTopic], [class*=Topic], [class*=topic], [class*=quiz]," +
    " [class*=question], [class*=tiwen], [class*=exercise], [class*=interact]," +
    " [class*=hudong], [class*=dialog], [class*=layer], [class*=mask]," +
    " [class*=popup], [class*=freeze], [class*=marking]";
  let vr = null;
  try {
    const v0 = document.querySelector("video");
    if (v0) vr = v0.getBoundingClientRect();
  } catch (e) {}
  const onVideo = (el) => {
    if (!vr) return true;
    let r = null;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    const ox = Math.min(r.right, vr.right) - Math.max(r.left, vr.left);
    const oy = Math.min(r.bottom, vr.bottom) - Math.max(r.top, vr.top);
    return ox > 40 && oy > 30;
  };
  const boxes = [];
  try { for (const b of document.querySelectorAll(BOX)) boxes.push(b); } catch (e) {}
  if (vis(fp) && boxes.indexOf(fp) < 0) boxes.push(fp);
  for (const box of boxes) {
    if (!vis(box) || !onVideo(box)) continue;
    let cands = [];
    try { cands = box.querySelectorAll("a, button, div, span, i, li, p, .vjs-button, [class*=btn]"); } catch (e) { continue; }
    let seen = 0;
    for (const el of cands) {
      if (seen++ > 800) break;
      const txt = (el.textContent || "").trim();
      if (!txt || txt.length > 8 || words.indexOf(txt) < 0) continue;
      if (!vis(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 320 || r.height > 90) continue;
      try { el.click(); n++; } catch (e) {}
    }
  }
  return n;
}
"""


# 倍速锁定: 遍历所有 video(含嵌套子 frame), 用 defineProperty 把 playbackRate 硬锁住
# 软锁(直接赋值)会被播放器改回来; 硬锁之后播放器再赋值也是白搭, 读出来永远是目标倍速
_RATE_LOCK_JS = """
() => {
  window.__cx_rate = __RATE__;
  window.__cx_hard = __HARD__;
  const hard = !!window.__cx_hard;
  const rate = window.__cx_rate;
  const force = (v) => {
    if (hard && !v.__cxHard) {
      try {
        const d = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "playbackRate");
        Object.defineProperty(v, "playbackRate", {
          configurable: true,
          get: () => window.__cx_rate,
          set: () => { try { d.set.call(v, window.__cx_rate); } catch (e) {} },
        });
        d.set.call(v, rate);
        v.__cxHard = true;
      } catch (e) {}
    }
    if (!v.__cxHard) {
      try {
        v.defaultPlaybackRate = rate;
        if (v.playbackRate !== rate) v.playbackRate = rate;
      } catch (e) {}
    }
    if (!v.__cxHooked) {
      v.__cxHooked = true;
      try {
        v.addEventListener("ratechange", () => {
          try { v.playbackRate = window.__cx_rate; } catch (e) {}
        });
      } catch (e) {}
    }
  };
  const list = document.querySelectorAll(__VIDEO__);
  for (const v of list) force(v);
  if (!window.__cx_timer) {
    window.__cx_timer = setInterval(() => {
      for (const v of document.querySelectorAll(__VIDEO__)) force(v);
    }, 1000);
  }
  return list.length;
}
"""


# 粘性拉回进度: 答完题/点完"继续学习"之后, 超星常把播放器整个重载一遍并从 0 重播,
# 这时候单次 v.currentTime = X 是压不住的 —— 赋值会被它的初始化抹掉(日志里就是
# "想拉回 2分35秒, 但播放器还停在 0分07秒")。所以往页面里装一个小定时器: 它盯着
# 这个播放器, 只要进度又退回去就再压一次, 直到真的压到位(或 __HOLD__ 毫秒用光)。
# __TARGET__ 是目标位置; 传 0 表示"推到结尾", 按 时长 - __NEAREND__ 算(末尾弹题收尾用)。
# __VIDEO__ 是找播放器的选择器, 播放器被重载成新元素时靠它重新抓住。
_SEEK_GUARD_JS = """
(v) => {
  const doc = (v && v.ownerDocument) || document;
  const win = doc.defaultView || window;
  const st = win.__cx_seek || (win.__cx_seek = {timer: 0, until: 0, target: 0,
                                                v: null, tries: 0, cur: 0});
  st.v = v;
  st.target = __TARGET__;
  st.until = Date.now() + __HOLD__;
  st.tries = 0;
  const aim = () => {
    if (st.target > 0) return st.target;
    const d = (st.v && st.v.duration) || 0;
    return Math.max(0, d - __NEAREND__);
  };
  const pick = () => {
    let el = st.v;
    if (el && el.isConnected === false) el = null;
    if (!el) {
      for (const cand of doc.querySelectorAll(__VIDEO__)) {
        const d = cand.duration || 0;
        if (d > 1 && (!st.target || d > st.target)) { el = cand; break; }
      }
      st.v = el;
    }
    return el;
  };
  const step = () => {
    const el = pick();
    if (!el) return true;
    const dur = el.duration || 0;
    if (dur <= 1) return true;
    const cur = el.currentTime || 0;
    st.cur = cur;
    const target = aim();
    if (!(target > 0) || cur >= target - 1.0) return false;
    st.tries += 1;
    try { el.currentTime = Math.min(target, Math.max(0, dur - 0.6)); } catch (e) {}
    try { el.muted = true; const p = el.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
    return st.tries < 80;
  };
  if (!st.timer) {
    st.timer = setInterval(() => {
      let keep = false;
      try { keep = step(); } catch (e) { keep = false; }
      if (!keep || Date.now() > st.until) { clearInterval(st.timer); st.timer = 0; st.v = null; }
    }, 400);
  }
  step();
  return 1;
}
"""

# 读一下粘性拉回当前把进度压到了哪里(页面里还没有记录时返回 -1)
_SEEK_READ_JS = """
(v) => {
  const win = ((v && v.ownerDocument) || document).defaultView || {};
  const st = win.__cx_seek;
  return (st && st.cur > 0) ? st.cur : -1;
}
"""

# 视频弹题/互动测验处理: probe=快速探一下, scan=完整扫, bypass=点掉浮层, answer=选第 n 个选项交卷
# 互动测验必须答完, 否则这个视频的任务点不会被判完成 -> 所以每次只动"当前这一题"
# 的所有元素都写宽了: class/id 关键字 + 选项个数 + "视频上压着谁" 三种途径一起判断
_QUIZ_JS = """
(arg) => {
  const mode = (arg && arg.mode) || "scan";
  const pick = (arg && arg.n) || 1;
  const res = {boxes: [], over: [], options: 0, submits: 0, closed: 0, cont: 0,
               answered: false, clicked: 0, optText: [], fb: "", fbText: "",
               qTotal: 0, qRight: 0};
  const vis = (el) => {
    if (!el) return false;
    try {
      const r = el.getBoundingClientRect();
      const st = window.getComputedStyle(el);
      return r.width > 30 && r.height > 16 &&
             st.display !== "none" && st.visibility !== "hidden";
    } catch (e) { return false; }
  };
  const clsOf = (el) => {
    try {
      if (el.className && el.className.baseVal !== undefined) {
        return el.className.baseVal;
      }
      return el.className || "";
    } catch (e) { return ""; }
  };
  const keyOf = (el) => String(clsOf(el) + " " + (el.id || "")).toLowerCase();
  const KINDS = ["videoquiz", "video-quiz", "video_quiz", "quiz", "question",
                 "tiwen", "answercard", "popup", "layui-layer", "exercise",
                 "marking", "interact", "hudong", "tktopic", "quizbox"];
  const kindOf = (el) => {
    const key = keyOf(el);
    for (const k of KINDS) { if (key.indexOf(k) >= 0) return k; }
    return "";
  };
  const QWORDS = ["提交", "回答", "正确", "错误", "选项", "选择", "下一题",
                  "答题", "作答", "题"];
  const qword = (t) => {
    for (const w of QWORDS) { if (t.indexOf(w) >= 0) return w; }
    return "";
  };
  // shadow DOM 里的元素也算上(新版互动测验有可能挂在 shadow root 里)
  const deep = (sel) => {
    const acc = [];
    const walk = (root, depth) => {
      if (!root || depth > 3) return;
      let list = [];
      try { list = root.querySelectorAll(sel); } catch (e) { list = []; }
      for (const el of list) acc.push(el);
      let all = [];
      try { all = root.querySelectorAll("*"); } catch (e) { all = []; }
      for (const el of all) { if (el.shadowRoot) walk(el.shadowRoot, depth + 1); }
    };
    walk(document, 0);
    return acc;
  };
  const OPT_SEL = "li, label, .option, [class*=opt], input[type=radio]," +
                  " input[type=checkbox]";
  const CAP = 12;
  // 只收"像选项"的节点: 有尺寸、文字不长、不是一大坨
  const optNodes = (root) => {
    const out = [];
    if (!root) return out;
    let list = [];
    try { list = root.querySelectorAll(OPT_SEL); } catch (e) { return out; }
    for (const el of list) {
      if (out.length >= CAP + 1) break;
      if (!vis(el)) continue;
      const t = String(el.textContent || "").trim();
      if (!t || t.length > 240) continue;
      let r = {height: 0, width: 0};
      try { r = el.getBoundingClientRect(); } catch (e) {}
      if (r.height > 140) continue;
      out.push(el);
    }
    return out;
  };
  const SEL = "[class*=quiz], [class*=Quiz], [class*=question], [class*=tiwen]," +
    " [class*=popup], [class*=layer], [class*=answer], [class*=exercise]," +
    " [class*=marking], [class*=interact], [class*=hudong]," +
    " [id*=quiz], [id*=popup], [id*=interact], [id*=dialog]";
  const full = (el) => {
    let r = {width: 0, height: 0};
    try { r = el.getBoundingClientRect(); } catch (e) {}
    return r.width > window.innerWidth * 0.96 &&
           r.height > window.innerHeight * 0.96;
  };
  const vids = deep("video");
  let vr = null;
  if (vids.length) {
    try { vr = vids[0].getBoundingClientRect(); } catch (e) {}
  }
  const overlap = (el) => {
    if (!vr) return 0;
    let r = null;
    try { r = el.getBoundingClientRect(); } catch (e) { return 0; }
    const ox = Math.min(r.right, vr.right) - Math.max(r.left, vr.left);
    const oy = Math.min(r.bottom, vr.bottom) - Math.max(r.top, vr.top);
    return (ox > 40 && oy > 30) ? 1 : 0;
  };
  // 1) 视频上压着谁: 浮层盖住播放器时 elementFromPoint 会直接指到它
  const over = [];
  let overEl = null;
  if (vr && vr.width > 100 && vr.height > 60) {
    const pts = [[vr.left + vr.width / 2, vr.top + vr.height / 2],
                 [vr.left + vr.width / 2, vr.top + vr.height * 0.3],
                 [vr.left + vr.width / 2, vr.top + vr.height * 0.7],
                 [vr.left + vr.width * 0.2, vr.top + vr.height / 2],
                 [vr.left + vr.width * 0.8, vr.top + vr.height / 2]];
    for (const p of pts) {
      let el = null;
      try { el = document.elementFromPoint(p[0], p[1]); } catch (e) {}
      if (!el) continue;
      const tag = String(el.tagName || "").toLowerCase();
      if (tag === "video" || tag === "canvas") continue;
      let node = el;
      let depth = 0;
      let hit = "";
      while (node && depth < 6 && !hit) {
        if (!full(node)) {
          const k = kindOf(node);
          const n = optNodes(node).length;
          const w = qword(String(node.textContent || "").trim().slice(0, 300));
          if (k) hit = "class:" + k;
          else if (w && n >= 2 && n <= CAP) hit = "text:" + w + "/opts:" + n;
          else if (n >= 2 && n <= CAP) hit = "opts:" + n;
          if (hit) overEl = node;
        }
        if (!hit) { node = node.parentElement; depth += 1; }
      }
      if (hit) {
        over.push({tag: tag, cls: String(clsOf(el)).slice(0, 80),
                   id: String(el.id || "").slice(0, 40), hit: hit,
                   text: String(el.textContent || "").trim().slice(0, 60)});
        break;
      }
    }
  }
  res.over = over.slice(0, 3);
  // 2) 按 class/id 关键字 + 选项个数找候选容器
  const cands = [];
  let seen = 0;
  for (const el of deep(SEL)) {
    if (seen++ > 3000) break;
    if (!vis(el)) continue;
    let r = {width: 0, height: 0};
    try { r = el.getBoundingClientRect(); } catch (e) {}
    if (r.width < 120 || r.height < 60) continue;
    if (full(el)) continue;
    const k = kindOf(el);
    const n = optNodes(el).length;
    if (!k && !(n >= 2 && n <= CAP)) continue;
    cands.push({el: el, kind: k, n: n, area: r.width * r.height,
                over: overlap(el)});
  }
  // 选项最多的优先: 外层弹层和内层题干会同时命中, 得挑真正装着选项的那个;
  // 然后才比"压着视频"和面积(面积小的更贴身)
  cands.sort((a, b) => {
    if (a.n !== b.n) return b.n - a.n;
    if (a.over !== b.over) return b.over - a.over;
    return a.area - b.area;
  });
  // elementFromPoint 指到的常常是"某个选项"这种叶子节点, 要往上找到装着选项的那层
  const boxWithOpts = (el) => {
    let n = el;
    for (let i = 0; i < 5 && n; i++) {
      if (optNodes(n).length >= 2) return n;
      n = n.parentElement;
    }
    return el;
  };
  let box = cands.length ? cands[0].el : null;
  if (overEl && (!box || !box.contains(overEl))) box = boxWithOpts(overEl);
  if (mode === "probe") {
    res.boxes = cands.slice(0, 2).map((c) => ({kind: c.kind, w: 0, h: 0,
                                              text: ""}));
    return res;
  }
  res.boxes = cands.slice(0, 4).map((c) => {
    let r = {width: 0, height: 0};
    try { r = c.el.getBoundingClientRect(); } catch (e) {}
    return {kind: c.kind || "容器", w: Math.round(r.width),
            h: Math.round(r.height),
            text: String(c.el.textContent || "").trim().slice(0, 80)};
  });
  const opts = box ? optNodes(box).slice(0, CAP) : [];
  res.options = opts.length;
  res.optText = opts.slice(0, 6).map((el) =>
    String(el.textContent || "").trim().slice(0, 30));
  // 2.5) 判分反馈: 提交之后浮层不会自己消失, 而是显示对错
  // 只认"结果/提示"这类小元素 + 不可能出现在题干里的说法;
  // 直接拿题干猜对错会翻车 —— 题干里常常写着"下列说法错误的是"
  const FB_WRONG = ["回答错误", "答错了", "回答不正确", "再试一次", "重新作答",
                    "再想想", "选错了"];
  const FB_RIGHT = ["回答正确", "回答对了", "答对了", "太棒了", "恭喜你答对",
                    "回答完全正确"];
  const FB_TIP = "[class*=tip], [class*=Tip], [class*=result], [class*=Result]," +
    " [class*=feedback], [class*=msg], [class*=status], [class*=right]," +
    " [class*=wrong], [class*=correct], [class*=error], [class*=Error]," +
    " [class*=checked], [class*=dui], [class*=cuo]";
  const hasAny = (arr, t) => {
    for (const w of arr) { if (t.indexOf(w) >= 0) return w; }
    return "";
  };
  const exactFb = (t) => {
    if (t === "正确" || t === "对" || t === "√" || t === "✓") return "right";
    if (t === "错误" || t === "错" || t === "×" || t === "✗") return "wrong";
    return "";
  };
  const fbOf = (scope) => {
    try {
      for (const el of scope.querySelectorAll(FB_TIP)) {
        if (!vis(el)) continue;
        const t = String(el.textContent || "").trim();
        if (!t || t.length > 24) continue;
        const w = hasAny(FB_WRONG, t);
        if (w) return {fb: "wrong", fbText: t.slice(0, 30)};
        const r = hasAny(FB_RIGHT, t);
        if (r) return {fb: "right", fbText: t.slice(0, 30)};
        const x = exactFb(t);
        if (x) return {fb: x, fbText: t.slice(0, 30)};
      }
    } catch (e) {}
    // 兜底只在"已经没得选了"的时候用: 有选项说明题还在, 别去猜题干
    if (!res.options) {
      const all = String(scope.textContent || "");
      const w = hasAny(FB_WRONG, all);
      if (w) return {fb: "wrong", fbText: w};
      const r = hasAny(FB_RIGHT, all);
      if (r) return {fb: "right", fbText: r};
    }
    return {fb: "", fbText: ""};
  };
  if (box && vis(box)) {
    const fb = fbOf(box);
    res.fb = fb.fb;
    res.fbText = fb.fbText;
  }
  // 2.55) 互动测验常常是一整组题: 浮层上写着"共 2 题, 已答对 1 题"。
  // 只要还有题没答对, 点"继续学习"平台就会把视频退回这个知识点重播 —— 题又弹
  // 出来, 来回就是"答完题视频被拉回开头"的死循环。所以先把这两个数读出来。
  // 这里只挑数字, 不用正则(脚本里不允许出现反斜杠)。
  const numIn = (s) => {
    let out = "";
    const t = String(s || "");
    for (let i = 0; i < t.length; i++) {
      const c = t.charAt(i);
      if (c >= "0" && c <= "9") out += c;
    }
    return out ? parseInt(out, 10) : 0;
  };
  const qCounter = (scope) => {
    if (!scope) return null;
    let el = null;
    try { el = scope.querySelector(".tkTopic_numbar"); } catch (e) { el = null; }
    if (!el) {
      try { el = scope.querySelector("[class*=numbar]"); } catch (e) { el = null; }
    }
    if (!el) return null;
    const t = String(el.textContent || "");
    if (!t) return null;
    const parts = t.split("已答对");
    const right = numIn(parts.length > 1 ? parts[1] : t);
    let total = parts.length > 1 ? numIn(parts[0]) : 0;
    if (!total) total = right;
    if (!total) return null;
    return {total: total, right: right};
  };
  let qc = qCounter(box);
  if (!qc) {
    for (const c of cands) { qc = qCounter(c.el); if (qc) break; }
  }
  if (!qc) qc = qCounter(overEl);
  if (qc) { res.qTotal = qc.total; res.qRight = qc.right; }
  // 2.6) "继续学习"按钮: 答对之后必须点它, 视频才会接着播
  const CONT = ["继续学习", "继续播放", "继续观看", "继续听课", "继续播放视频",
                "继续", "播放视频", "下一题"];
  const contBtns = () => {
    const out = [];
    const ok = (el) => {
      if (!vis(el)) return false;
      if (out.indexOf(el) >= 0) return false;
      let r = {width: 0, height: 0};
      try { r = el.getBoundingClientRect(); } catch (e) { return false; }
      if (r.width > 320 || r.height > 90) return false;
      out.push(el);
      return true;
    };
    const look = (root) => {
      let list = [];
      try { list = root.querySelectorAll("a, button, span, div, input"); }
      catch (e) { return; }
      for (const el of list) {
        if (out.length >= 3) return;
        const t = String(el.textContent || el.value || "").trim();
        if (!t || t.length > 10 || CONT.indexOf(t) < 0) continue;
        ok(el);
      }
    };
    if (box) {
      let p = box;
      for (let i = 0; i < 3 && p && !out.length; i++) {
        look(p);
        p = p.parentElement;
      }
    }
    if (!out.length) look(document);
    return out;
  };
  res.cont = contBtns().length;
  // 3) 提交/确定按钮: 只在容器和它的父节点里找, 免得点到别处去
  const SUBWORDS = ["提交", "确定", "确认", "提交答案", "下一题", "继续", "保存",
                    "重新作答", "再试一次", "我知道了"];
  const subs = [];
  const scopes = [];
  if (box) {
    let p = box;
    for (let i = 0; i < 3 && p; i++) { scopes.push(p); p = p.parentElement; }
  }
  for (const scope of scopes) {
    let list = [];
    try { list = scope.querySelectorAll("a, button, input, div, span"); }
    catch (e) { list = []; }
    for (const el of list) {
      if (subs.length >= 6) break;
      if (!vis(el)) continue;
      const t = String(el.textContent || el.value || "").trim();
      if (SUBWORDS.indexOf(t) < 0) continue;
      let r = {width: 0, height: 0};
      try { r = el.getBoundingClientRect(); } catch (e) {}
      if (r.width > 360 || r.height > 100) continue;
      subs.push(el);
    }
    if (subs.length) break;
  }
  res.submits = subs.length;
  if (mode === "scan") return res;
  if (mode === "continue") {
    let n = 0;
    for (const el of contBtns()) {
      if (n >= 2) break;
      try { el.click(); n += 1; } catch (e) {}
    }
    res.cont = n;
    return res;
  }
  if (mode === "bypass") {
    let closed = 0;
    let n = 0;
    if (box) {
      let closers = [];
      try {
        closers = box.querySelectorAll(
          "[class*=close], [class*=Close], [class*=guanbi], a, i, em, span");
      } catch (e) { closers = []; }
      for (const el of closers) {
        if (n++ > 400) break;
        if (!vis(el)) continue;
        const t = String(el.textContent || "").trim();
        const key = keyOf(el);
        const looksClose = t === "x" || t === "X" ||
          t === "关闭" || t === "跳过" || t === "知道了" || t === "取消" ||
          key.indexOf("close") >= 0 || key.indexOf("guanbi") >= 0;
        if (!looksClose) continue;
        let r = {width: 0, height: 0};
        try { r = el.getBoundingClientRect(); } catch (e) {}
        if (r.width > 90 || r.height > 90) continue;
        try { el.click(); closed += 1; } catch (e) {}
      }
    }
    res.closed = closed;
    return res;
  }
  if (mode === "answer") {
    if (!opts.length) return res;
    const target = opts[(pick - 1 + opts.length) % opts.length];
    try {
      const inner = target.querySelector("input, label, a, span") || target;
      inner.click();
    } catch (e) {}
    try { target.click(); } catch (e) {}
    if (subs.length) {
      try { subs[0].click(); res.clicked = 1; } catch (e) {}
    }
    res.answered = true;
    return res;
  }
  return res;
}
"""

# 弹题现场取证: 抓"视频中心点上压着的元素链" + 所有可疑浮层 + shadow DOM
# 不靠关键字也能定位: 元素链里的 class 就是我们下次要补的选择器
_QUIZ_DUMP_JS = """
() => {
  const out = [];
  const NL = String.fromCharCode(10);
  const clsOf = (el) => {
    try {
      if (el.className && el.className.baseVal !== undefined) {
        return el.className.baseVal;
      }
      return el.className || "";
    } catch (e) { return ""; }
  };
  const vis = (el) => {
    if (!el) return false;
    try {
      const r = el.getBoundingClientRect();
      const st = window.getComputedStyle(el);
      return r.width > 20 && r.height > 10 &&
             st.display !== "none" && st.visibility !== "hidden";
    } catch (e) { return false; }
  };
  const head = (el, label) => label + " <" +
    String(el.tagName || "").toLowerCase() + " id=" + String(el.id || "") +
    " class=" + String(clsOf(el)).slice(0, 140) + "> " +
    String(el.textContent || "").trim().slice(0, 80) + " || HTML: " +
    String(el.outerHTML || "").slice(0, 1200);
  out.push("网址: " + String(location.href));
  out.push("标题: " + String(document.title || ""));
  const vids = document.querySelectorAll("video");
  out.push("video 个数: " + vids.length);
  if (vids.length) {
    let r = null;
    try { r = vids[0].getBoundingClientRect(); } catch (e) {}
    if (r) {
      out.push("video 位置: " + Math.round(r.left) + "," + Math.round(r.top) +
               " " + Math.round(r.width) + "x" + Math.round(r.height));
      let el = null;
      try {
        el = document.elementFromPoint(r.left + r.width / 2,
                                      r.top + r.height / 2);
      } catch (e) {}
      let node = el;
      let depth = 0;
      while (node && depth < 7) {
        out.push("压中心点第 " + depth + " 层: <" +
                 String(node.tagName || "").toLowerCase() + " id=" +
                 String(node.id || "") + " class=" +
                 String(clsOf(node)).slice(0, 140) + "> " +
                 String(node.textContent || "").trim().slice(0, 60));
        node = node.parentElement;
        depth += 1;
      }
    }
  }
  const SEL = "[class*=quiz], [class*=question], [class*=tiwen], [class*=answer]," +
    " [class*=interact], [class*=hudong], [class*=popup], [class*=dialog]," +
    " [class*=layer], [class*=mask], [class*=overlay], [class*=exam]," +
    " [id*=quiz], [id*=popup], [id*=dialog], [id*=interact]";
  let n = 0;
  let hits = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (n++ > 2000) break;
    if (!vis(el)) continue;
    out.push(head(el, "候选浮层"));
    hits += 1;
    if (hits >= 6) break;
  }
  let shadows = 0;
  for (const el of document.querySelectorAll("*")) {
    if (el.shadowRoot) {
      shadows += 1;
      if (shadows <= 2) {
        out.push("shadow root: " +
                 String(el.shadowRoot.innerHTML || "").slice(0, 1500));
      }
    }
  }
  out.push("shadow root 个数: " + shadows);
  const ifr = [];
  for (const f of document.querySelectorAll("iframe")) {
    let r = null;
    try { r = f.getBoundingClientRect(); } catch (e) {}
    ifr.push(String(f.getAttribute("src") || "") + " (" +
             (r ? Math.round(r.width) + "x" + Math.round(r.height) : "?") + ")");
  }
  out.push("iframe: " + ifr.join(" ; "));
  if (!hits) {
    out.push("没找到关键字容器, body HTML 前 6000 字符: " +
             String(document.body ? document.body.innerHTML : "").slice(0, 6000));
  }
  return out.join(NL);
}
"""

# 播放器消失时用的兜底取证: 不管 class 叫什么名字, 只要是有尺寸的悬浮层,
# 而且压在视频所在区域上, 就把它整个 outerHTML 抓出来 —— 互动测验就是这种形态
_OVERLAY_DUMP_JS = """
() => {
  const vids = document.querySelectorAll("video");
  const vr = vids.length ? vids[0].getBoundingClientRect() : null;
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll("div, section, ul, form, iframe")) {
    if (n++ > 8000) break;
    let st;
    try { st = window.getComputedStyle(el); } catch (e) { continue; }
    if (st.display === "none" || st.visibility === "hidden") continue;
    if (st.position !== "fixed" && st.position !== "absolute") continue;
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 60) continue;
    if (vr) {
      const ox = Math.min(r.right, vr.right) - Math.max(r.left, vr.left);
      const oy = Math.min(r.bottom, vr.bottom) - Math.max(r.top, vr.top);
      if (ox < 60 || oy < 40) continue;
    }
    let cls = "";
    try {
      cls = (el.className && el.className.baseVal !== undefined)
        ? el.className.baseVal : (el.className || "");
    } catch (e) { cls = ""; }
    out.push("悬浮层 " + st.position + " " + Math.round(r.width) + "x" +
             Math.round(r.height) + " class=" + String(cls).slice(0, 80) +
             " :: " + el.outerHTML.slice(0, 1500));
    if (out.length >= 6) break;
  }
  return out.join("  ||  ");
}
"""

# 枚举当前 frame 里所有有尺寸的 <video>, 顺带算出每个是不是已经播完
# 一页多个视频(情景剧)时会用: 先看成没完的, 看完了再找下一个
_VIDEO_LIST_JS = """
() => {
  const out = [];
  const vids = Array.from(document.querySelectorAll("video"));
  // 顺着父节点(必要时跨 iframe)往上摸到任务点容器 div.ans-attach-ct, 看平台把它
  // 标成"完成"没有。播放器放到结尾 != 任务点完成: 一页多个视频时只有靠这个才
  // 分得清"这一段已经真做完"和"播完了但平台没给标"。
  const jobPending = (el) => {
    let node = el;
    let hops = 0;
    while (node && hops < 20) {
      hops += 1;
      let cls = "";
      try {
        cls = (node.className && node.className.baseVal !== undefined)
          ? node.className.baseVal : (node.className || "");
      } catch (e) { cls = ""; }
      cls = String(cls);
      if (cls.indexOf("ans-attach-ct") >= 0) {
        return cls.indexOf("ans-job-finished") < 0;
      }
      if (node.parentElement) { node = node.parentElement; continue; }
      let fe = null;
      try {
        const win = node.ownerDocument ? node.ownerDocument.defaultView : null;
        fe = win ? win.frameElement : null;
      } catch (e) { fe = null; }
      node = fe;
    }
    return null;
  };
  for (let i = 0; i < vids.length; i++) {
    const v = vids[i];
    let r = { width: 0, height: 0 };
    try { r = v.getBoundingClientRect(); } catch (e) {}
    if (r.width < 40 || r.height < 30) continue;
    let dur = 0, cur = 0, ended = false, paused = true;
    try { dur = v.duration || 0; } catch (e) {}
    try { cur = v.currentTime || 0; } catch (e) {}
    try { ended = !!v.ended; } catch (e) {}
    try { paused = v.paused; } catch (e) {}
    const done = ended || (dur > 1 && cur >= dur - 1);
    out.push({idx: i, dur: dur, cur: cur, ended: ended,
              paused: paused, done: done, jobPending: jobPending(v)});
  }
  return out;
}
"""
# 超星的任务点容器是 div.ans-attach-ct, 做完会加 ans-job-finished
# 用它来判断"这一页到底还有没有没做完的任务点", 比瞎猜准得多
_ATTACH_STATE_JS = """
() => {
  let total = 0;
  let done = 0;
  const list = document.querySelectorAll(".ans-attach-ct, [class*=ans-attach-ct]");
  for (const el of list) {
    total += 1;
    let key = "";
    try {
      key = (el.className && el.className.baseVal !== undefined)
        ? el.className.baseVal : (el.className || "");
    } catch (e) { key = ""; }
    if (String(key).indexOf("ans-job-finished") >= 0) done += 1;
  }
  return {total: total, done: done,
          videos: document.querySelectorAll("video").length};
}
"""
# 任务点状态: 超星把每个附件包在 div.ans-attach-ct 里, 做完才加 ans-job-finished。
# "视频播完"只是播放器放完了, "任务点完成"是父页面收到报告后加的这个 class。
# 靠它就能知道这一段到底算不算完成。
_JOB_STATE_JS = """
() => {
  const out = [];
  const list = document.querySelectorAll(".ans-attach-ct, [class*=ans-attach-ct]");
  for (let i = 0; i < list.length; i++) {
    const el = list[i];
    let cls = "";
    try {
      cls = (el.className && el.className.baseVal !== undefined)
        ? el.className.baseVal : (el.className || "");
    } catch (e) { cls = ""; }
    cls = String(cls);
    let label = "";
    try {
      const ic = el.querySelector(".ans-job-icon");
      if (ic) label = ic.getAttribute("aria-label") || "";
    } catch (e) { label = ""; }
    out.push({i: i, label: label,
              done: cls.indexOf("ans-job-finished") >= 0});
  }
  return out;
}
"""

# 反查"这个播放器属于哪一张任务点卡片": 播放器在子 iframe 里, 顺着
# window.frameElement 往上走就能摸到父页面里包着它的 div.ans-attach-ct,
# 再用序号跟 _JOB_STATE_JS 的结果对上号(两边都按同一个顺序数)。
_ATTACH_KEY_JS = """
() => {
  const miss = {idx: -1, found: false, done: false, cls: ""};
  try {
    if (!window.frameElement) return miss;
  } catch (e) { return miss; }
  let el = window.frameElement;
  let hops = 0;
  while (el && hops < 12) {
    hops += 1;
    let cls = "";
    try {
      cls = (el.className && el.className.baseVal !== undefined)
        ? el.className.baseVal : (el.className || "");
    } catch (e) { cls = ""; }
    cls = String(cls);
    if (cls.indexOf("ans-attach-ct") >= 0) {
      let idx = -1;
      try {
        const all = el.ownerDocument.querySelectorAll(
          ".ans-attach-ct, [class*=ans-attach-ct]");
        for (let i = 0; i < all.length; i++) {
          if (all[i] === el) { idx = i; break; }
        }
      } catch (e) { idx = -1; }
      return {idx: idx, found: true,
              done: cls.indexOf("ans-job-finished") >= 0,
              cls: cls.slice(0, 120)};
    }
    el = el.parentElement;
  }
  return miss;
}
"""

# 平台自己的限制提示: 命中说明"任务点标不上"不怪脚本, 是平台按天/按时长卡住了
_LIMIT_TIP_JS = """
() => {
  const out = [];
  const names = ["jobLimitTip", "videoLimitTip", "jobFinishTip"];
  for (const name of names) {
    let list = [];
    try {
      list = document.querySelectorAll("[id*=" + name + "], [class*=" + name + "]");
    } catch (e) { list = []; }
    for (const el of list) {
      let vis = false;
      let txt = "";
      try {
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        vis = r.width > 0 && r.height > 0 &&
              st.display !== "none" && st.visibility !== "hidden";
        txt = String(el.textContent || "").trim().slice(0, 100);
      } catch (e) { vis = false; }
      if (!vis || !txt) continue;
      out.push(name + " :: " + txt);
      break;
    }
  }
  return out;
}
"""

# 任务点上报钩子: 视频 iframe 播完会发一条 JOB_FINISH_INFO 给父页面, 父页面收到才
# 给任务点加 ans-job-finished。这里把进出本窗口的这条消息都记下来, 用来判断到底
# 是"根本没上报"还是"上报了没人处理"。
_JOB_HOOK_BODY = """
  try { if (window.__cxJobHook) return 2; } catch (e) {}
  try { window.__cxJobHook = 1; } catch (e) {}
  try { if (!window.__cxMsgs) window.__cxMsgs = []; } catch (e) {}
  const isJob = (d) => {
    try {
      if (!d || typeof d !== "object") return false;
      const t = String(d.type || "") + String(d.module || "");
      return t.indexOf("JOB_FINISH") >= 0 || t.indexOf("JOBFINISH") >= 0;
    } catch (e) { return false; }
  };
  const keep = (dir, d) => {
    try {
      if (!isJob(d)) return;
      const box = window.__cxMsgs;
      if (!box) return;
      let peek = "";
      try { peek = JSON.stringify(d).slice(0, 160); } catch (e2) { peek = ""; }
      box.push({dir: dir, type: String(d.type || ""), peek: peek,
                at: Date.now()});
      while (box.length > 20) box.shift();
    } catch (e) {}
  };
  try {
    const raw = window.postMessage.bind(window);
    window.postMessage = function (msg, target, transfer) {
      keep("out", msg);
      return raw(msg, target, transfer);
    };
  } catch (e) {}
  try {
    window.addEventListener("message", (ev) => keep("in", ev && ev.data), true);
  } catch (e) {}
  return 1;
"""

# evaluate() 版本: Playwright 的 evaluate 传函数表达式最稳
_JOB_HOOK_JS = "() => {" + _JOB_HOOK_BODY + "}"
# add_init_script() 版本: 那里是当普通脚本执行的, 必须写成 IIFE
_JOB_HOOK_BOOT = "(() => {" + _JOB_HOOK_BODY + "})()"
# 读回上面记的那些消息(诊断"任务点为什么没标上"用)
_JOB_MSG_JS = "() => (window.__cxMsgs || []).slice(-6)"

_FORCE_LOGIN = threading.Event()


def _watch_enter_key() -> None:
    """后台阻塞等一次回车; 用户按下就把 _FORCE_LOGIN 置位"""
    try:
        input()
    except Exception:
        return
    _FORCE_LOGIN.set()


class ChaoxingRunner:
    def __init__(self, page, rate: float):
        self.page = page
        self.rate = rate
        self.rate_now = rate      # 当前实际用的倍速(重播那一段会临时调高)
        self.unit_box = ''
        self.unit_node = ''
        self.content_dumped = False
        self.stopped = False          # 弹出风控验证页后置位: 停手等人工过验证
        self.antispider_seen = False  # 导航事件里提前记下的风控信号
        self.antispider_dumped = False
        self.quiz_miss_dumps = 0     # 卡住但没识别出弹题的取证次数上限
        self.quiz_video = None       # 正在处理弹题的那段视频: 答题期间把它停住
        self.job_missed = 0           # 播完了但平台没把任务点标成的次数
        try:
            self.page.on('framenavigated', self._on_frame_navigated)
        except Exception:
            pass

    # -------------------------------------------------------- 登录等待
    def _alive_pages(self) -> list:
        '''当前浏览器里所有还开着的标签页'''
        try:
            pages = list(self.page.context.pages)
        except Exception:
            pages = [self.page]
        return [p for p in pages if not p.is_closed()]

    @staticmethod
    def _is_login_url(url: str) -> bool:
        '''判断某个网址是不是学习通的登录页'''
        low = (url or "").strip().lower()
        if not low.startswith("http"):
            return True
        return any(mark in low for mark in LOGIN_URL_MARKS)

    def _follow_other_tab(self) -> None:
        '''登录常常发生在新标签页里, 这里把跟踪目标切过去'''
        if not self._is_login_url(self.page.url):
            return
        for page in reversed(self._alive_pages()):
            if not self._is_login_url(page.url):
                log.info("登录页开在新标签页了, 改为跟踪: %s", page.url)
                self.page = page
                return

    def _has_course_tree(self, page) -> bool:
        '''看看这个标签页里的章节树是不是真的渲染好了'''
        ''''''
        '''只判断容器在不在是不够的: 超星会先给一个空的 #coursetree,'''
        '''过一会儿才把章节异步填进去'''
        for sel in READY_SELECTORS:
            try:
                count = page.evaluate(
                    _fill_js("() => document.querySelectorAll(__SEL__).length",
                             sel=sel)
                )
                if count:
                    return True
            except Exception:
                pass
        return False

    def _start_enter_watcher(self) -> None:
        '''后台等一次回车, 用户按下就强制跳过登录检测'''
        if getattr(self, "_enter_thread", None) is not None:
            return
        thread = threading.Thread(target=_watch_enter_key, daemon=True)
        thread.start()
        self._enter_thread = thread

    def wait_until_logged_in(self, headless: bool) -> None:
        started = time.monotonic()
        self._start_enter_watcher()
        last_note = None
        last_note_at = 0.0
        while True:
            for page in self._alive_pages():
                if self._is_login_url(page.url):
                    continue
                if self._has_course_tree(page):
                    if page is not self.page:
                        log.info("已切换到课程标签页: %s", page.url)
                        self.page = page
                    log.info("登录成功, 课程章节列表已就绪")
                    if KEEP_ALIVE:
                        self.install_keep_alive()
                    return

            if _FORCE_LOGIN.is_set():
                log.warning("收到回车信号, 跳过登录检测继续往下跑")
                return

            self._follow_other_tab()
            now = time.monotonic()
            url = self.page.url
            if self._is_login_url(url):
                note = ("等待登录中, 请在弹出的浏览器窗口里登录学习通(扫码或密码)。"
                        " 当前网址: %s" % url)
            else:
                note = ("已经不在登录页了, 但没解析到课程章节列表。"
                        " 当前网址: %s" % url)
            if note != last_note or now - last_note_at > 30:
                log.info(note)
                last_note = note
                last_note_at = now

            elapsed = now - started
            if headless and elapsed > 120:
                raise SystemExit("无头模式下 120 秒内未完成登录。"
                                 "请先不带 --headless 运行一次完成登录。")
            if not headless and elapsed > 900:
                raise SystemExit("等待登录超时(15 分钟)。请检查网址是否正确、"
                                 "网络是否通畅。")
            time.sleep(2)

    def dump_page_diagnostics(self) -> None:
        '''把主页面和所有 iframe 的结构导出成文本, 方便定位选择器'''
        path = Path(__file__).resolve().parent / "调试-页面结构.txt"
        lines = ["主页面网址: %s" % self.page.url, ""]

        lines.append("===== 章节树选择器命中情况 =====")
        for box in UNIT_CONTAINER_CANDIDATES:
            lines.append("容器 %-30s 命中 %s 个"
                         % (box, self._count(box)))
        for node_sel in UNIT_NODE_CANDIDATES:
            js = _fill_js(
                "() => { const b = document.querySelector(__BOX__);"
                " return b ? b.querySelectorAll(__NODE__).length : -1; }",
                box=UNIT_CONTAINER_CANDIDATES[0],
                node=node_sel,
            )
            try:
                count = self.page.evaluate(js)
            except Exception as exc:
                count = "查询失败: %s" % exc
            lines.append("节点 %-30s 命中 %s 个" % (node_sel, count))
        for sel in READY_SELECTORS:
            lines.append("就绪信号 %-24s 命中 %s 个"
                         % (sel, self._count(sel)))
        for sel in CONTENT_FRAME_CANDIDATES:
            lines.append("内容区 %-28s 命中 %s 个"
                         % (sel, self._count(sel)))
        lines.append("")

        lines.append("===== 页面上出现过的 class(去重后前 500 个) =====")
        try:
            names = self.page.evaluate(
                "() => { const out = [];"
                " for (const n of document.querySelectorAll('[class]')) {"
                " if (typeof n.className === 'string') out.push(n.className); }"
                " return out.slice(0, 500); }"
            )
            lines.extend(sorted(set(names)))
        except Exception as exc:
            lines.append("class 统计失败: %s" % exc)
        lines.append("")

        frames = list(self.page.frames)
        lines.append("===== 共 %d 个 frame =====" % len(frames))
        for i, fr in enumerate(frames):
            lines.append("[%d] %s" % (i, fr.url))
        lines.append("")

        for i, fr in enumerate(frames):
            lines.append("=" * 70)
            lines.append("frame[%d] %s" % (i, fr.url))
            lines.append("=" * 70)
            try:
                lines.append(fr.content()[:200000])
            except Exception as exc:
                lines.append("导出该 frame 的 HTML 失败: %s" % exc)
            lines.append("")

        try:
            path.write_text(chr(10).join(lines), encoding="utf-8")
            log.info("页面结构已写入: %s", path)
        except Exception as exc:
            log.warning("写调试文件失败: %s", exc)

    def _count(self, sel: str):
        '''数一数某个选择器命中了多少个元素'''
        try:
            return self.page.evaluate(
                _fill_js("() => document.querySelectorAll(__SEL__).length",
                         sel=sel)
            )
        except Exception as exc:
            return "查询失败: %s" % exc

    # -------------------------------------------------------- 章节任务点
    def collect_units(self) -> list:
        '''收集章节树里的任务点, 返回 [{uid,text,state,unfinish,visible}, ...]'''
        for box in UNIT_CONTAINER_CANDIDATES:
            for node_sel in UNIT_NODE_CANDIDATES:
                js = _fill_js(
                    '''
                    () => {
                      const items = [];
                      const root = document.querySelector(__BOX__);
                      if (!root) return items;
                      const nodes = root.querySelectorAll(__NODE__);
                      for (const node of nodes) {
                        const nameEl = node.querySelector(__NAME_SEL__);
                        if (!nameEl) continue;
                        // 章节头会包住整棵子树, 它的 querySelector 会命中最里层,
                        // 所以只要节点内部还嵌着别的任务点节点, 就说明它不是叶子
                        if (node.querySelector("div.posCatalog_select")) continue;
                        if (!node.id) node.id = "__cx_" + items.length;
                        const stateEl = node.querySelector(__STATE_SEL__);
                        const cntEl = node.querySelector(__CNT_SEL__);
                        const rect = node.getBoundingClientRect();
                        items.push({
                          uid: node.id,
                          text: (nameEl.textContent || "").trim(),
                          state: stateEl ? stateEl.textContent.trim() : "",
                          unfinish: cntEl ? (parseInt(cntEl.value, 10) || 0) : 0,
                          visible: rect.width > 0 && rect.height > 0,
                        });
                      }
                      return items;
                    }
                    ''',
                    box=box,
                    node=node_sel,
                    name_sel=SELECTORS["unit_name"],
                    state_sel=SELECTORS["unit_state"],
                    cnt_sel=SELECTORS["unfinish_count"],
                )
                try:
                    data = self.page.evaluate(js)
                except Exception:
                    data = []
                if data:
                    self.unit_box = box
                    self.unit_node = node_sel
                    return data
        return []

    def collect_units_wait(self, timeout: float = 30.0) -> list:
        '''等章节树渲染出来再收集; 空容器不算'''
        deadline = time.monotonic() + timeout
        while True:
            units = self.collect_units()
            if units or time.monotonic() >= deadline:
                return units
            time.sleep(1)

    def click_unit(self, unit: dict) -> bool:
        js = _fill_js(
            '''
            (uid) => {
              const node = document.getElementById(uid);
              if (!node) return false;
              const nameEl = node.querySelector(__NAME_SEL__);
              const target = nameEl || node;
              try { target.scrollIntoView({block: "center"}); } catch (e) {}
              target.click();
              return true;
            }
            ''',
            name_sel=SELECTORS["unit_name"],
        )
        try:
            return bool(self.page.evaluate(js, unit["uid"]))
        except Exception:
            return False

    def unit_state(self, unit: dict) -> str:
        js = _fill_js(
            '''
            (uid) => {
              const node = document.getElementById(uid);
              if (!node) return "";
              const el = node.querySelector(__STATE_SEL__);
              return el ? el.textContent.trim() : "";
            }
            ''',
            state_sel=SELECTORS["unit_state"],
        )
        try:
            return self.page.evaluate(js, unit["uid"]) or ""
        except Exception:
            return ""

    @staticmethod
    def _knowledge_id(uid: str) -> str:
        '''从 cur1248565493 这样的节点 id 里取出 knowledgeId'''
        return uid[3:] if uid.startswith("cur") else ""

    def _wait_chapter_loaded(self, unit: dict, timeout: float = 25.0) -> None:
        '''等右侧内容区切到刚点的这一章, 免得把上一章的视频当成这一章的'''
        kid = self._knowledge_id(unit.get("uid", ""))
        if not kid:
            time.sleep(1.5)
            return
        js = _fill_js(
            "() => { const f = document.querySelector(__SEL__);"
            " return f ? (f.src || '') : ''; }",
            sel=CONTENT_FRAME_CANDIDATES[0],
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                src = self.page.evaluate(js) or ""
            except Exception:
                src = ""
            if kid in src or kid in (self.page.url or ""):
                return
            time.sleep(0.5)
        log.warning("等右侧内容切到本章超时(章节 %s), 按当前内容继续", kid)

    @staticmethod
    def _is_finished(state: str) -> bool:
        return any(mark in state for mark in FINISHED_MARKS)

    # -------------------------------------------------------- 内容区处理
    def find_content_frame(self):
        '''找到右侧内容区的 Frame 对象'''

        '''超星 mooc1 章节学习页里它是 div.course_main 下的 iframe#iframe,'''
        '''网址形如 /mooc-ans/knowledge/cards?...'''
        ''''''
        timeout = time.monotonic() + 30
        while time.monotonic() < timeout:
            for fr in self.page.frames:
                if fr == self.page.main_frame:
                    continue
                url = fr.url or ""
                if fr.name in ("frame_content", "iframe"):
                    return fr
                if any(mark in url for mark in CONTENT_FRAME_MARKS):
                    return fr
            for sel in CONTENT_FRAME_CANDIDATES:
                try:
                    el = self.page.query_selector(sel)
                except Exception:
                    el = None
                if not el:
                    continue
                try:
                    cf = el.content_frame()
                except Exception:
                    cf = None
                if cf:
                    return cf
            time.sleep(0.5)
        return None

    def _video_list(self, frame) -> list:
        '''列出当前 frame 里所有有尺寸的 <video> 及其状态; 异常时返回 []'''
        try:
            items = frame.evaluate(_VIDEO_LIST_JS)
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [i for i in items if isinstance(i, dict) and i.get("idx") is not None]

    def find_visible_video(self, frame):
        '''在当前 frame 及其子 frame 中找一个还没播完的可见 <video>, 返回 Locator'''
        items = self._video_list(frame)
        # 一页多个视频(情景剧)时优先找还没播完的, 免得抓到已经看完的那段
        for item in items:
            if not item.get('done'):
                try:
                    return frame.locator('video').nth(int(item['idx']))
                except Exception:
                    pass
        if items:
            try:
                return frame.locator('video').nth(int(items[0]['idx']))
            except Exception:
                pass
        for child in getattr(frame, "child_frames", []) or []:
            try:
                found = self.find_visible_video(child)
                if found:
                    return found
            except Exception:
                pass
        return None

    def _attach_pending(self, frame) -> int:
        """这一页还有几个任务点没做完; 数不出来时返回 -1"""
        for fr in [frame] + list(getattr(frame, "child_frames", []) or []):
            try:
                state = fr.evaluate(_ATTACH_STATE_JS)
            except Exception:
                continue
            if not isinstance(state, dict):
                continue
            total = int(state.get("total") or 0)
            if total <= 0:
                continue
            return max(0, total - int(state.get("done") or 0))
        return -1

    # ------------------------------------------------ 任务点完成核对
    def install_job_hook(self) -> int:
        '''给所有 frame(含嵌套)装任务点上报钩子; 幂等, 新 frame 会自动重装'''
        if not JOB_FINISH_CHECK:
            return 0
        done = 0
        for fr in self._page_frames():
            try:
                fr.evaluate(_JOB_HOOK_JS)
                done += 1
            except Exception:
                pass
        return done

    def _job_snapshot(self, frame):
        '''读出这一页每个任务点的完成状态, 返回 {序号: {...}}; 读不出来返回 None'''
        if frame is None:
            return None
        for fr in [frame] + list(getattr(frame, 'child_frames', []) or []):
            try:
                rows = fr.evaluate(_JOB_STATE_JS)
            except Exception:
                continue
            if not isinstance(rows, list) or not rows:
                continue
            out = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    idx = int(row.get('i'))
                except Exception:
                    continue
                out[idx] = {'done': bool(row.get('done')),
                            'label': str(row.get('label') or '')}
            if out:
                return out
        return None

    def _job_frame(self, video):
        '''找到放着任务点卡片(div.ans-attach-ct)的那个 frame

        播放器在很深的子 iframe 里, 任务点卡片在外面一层, 得顺着
        frame.parent_frame 往上找; 找得到才谈得上核对完成状态。
        '''
        try:
            fr = self._frame_of_video(video)
        except Exception:
            fr = None
        hops = 0
        while fr is not None and hops < 6:
            hops += 1
            if self._job_snapshot(fr):
                return fr
            try:
                fr = fr.parent_frame
            except Exception:
                fr = None
        # 往上摸不到也不放弃: 退回内容区那一层(任务点卡片就在里面)
        try:
            cand = self.find_content_frame()
        except Exception:
            cand = None
        if cand is not None and self._job_snapshot(cand):
            return cand
        return None

    @staticmethod
    def _video_job(video):
        '''看这个播放器属于哪张任务点卡片, 以及那张卡片现在完成没有'''
        try:
            got = video.evaluate(_ATTACH_KEY_JS)
        except Exception:
            return None
        return got if isinstance(got, dict) else None

    def _job_done_now(self, video) -> bool:
        '''这一段的任务点现在是不是已经被平台标成"已完成"了

        播放中每隔几秒核对一次: 一旦标上就没必要等它播到结尾, 直接切下一个视频。
        认不出这个播放器属于哪张任务点卡片时返回 False(不硬判)。
        '''
        job = self._video_job(video)
        if isinstance(job, dict) and job.get('found') and job.get('done'):
            return True
        return False

    @staticmethod
    def _job_marked(snap, idx, before):
        '''这份快照里, 这一段的任务点算完成了吗; 认不出来返回 None'''
        if snap is None:
            return None
        if idx is not None and idx in snap:
            return bool(snap[idx]['done'])
        if before is None:
            return None
        for key, item in snap.items():
            if item['done'] and not (before.get(key) or {}).get('done'):
                return True
        return None

    def _wait_job_finished(self, job_frame, before, job_now,
                           timeout=None) -> tuple:
        '''盯一会儿, 看平台有没有把这一段的任务点标成完成; 返回 (好了没, 说明)'''
        if job_frame is None:
            return True, ''
        idx = None
        if isinstance(job_now, dict):
            try:
                raw = int(job_now.get('idx', -1))
            except Exception:
                raw = -1
            if raw >= 0:
                idx = raw
        limit = JOB_FINISH_WAIT_FIRST if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.5, limit)
        waited = False
        while True:
            marked = self._job_marked(self._job_snapshot(job_frame), idx, before)
            if marked is None:
                return True, ''
            if marked:
                return True, '任务点已标记完成'
            if time.monotonic() >= deadline:
                return False, ''
            if not waited:
                waited = True
                log.info('视频播完了, 正在等平台把任务点标成完成(最多 %.0f 秒)……',
                         limit)
            time.sleep(1.5)

    def _limit_tips(self) -> list:
        '''平台自己的"今日上限"类提示; 命中说明这一段不是脚本能解决的'''
        tips = []
        for fr in self._page_frames():
            try:
                got = fr.evaluate(_LIMIT_TIP_JS)
            except Exception:
                continue
            if isinstance(got, list):
                tips.extend(str(x) for x in got)
        return tips[:4]

    def _dump_job_diagnostics(self, video, job_frame, before, job_now) -> None:
        '''"视频播完了但任务点没完成"时把现场导出来, 定位到底是哪种原因'''
        path = Path(__file__).resolve().parent / '调试-任务点未完成.txt'
        lines = ['视频播完了(弹题也过了), 但平台没把任务点标成完成', '']
        lines.append('播放器状态: %s' % (self._video_info(video) or '读不到'))
        lines.append('这一段对应的任务点: %s' % (job_now or '认不出来'))
        lines.append('播放前的任务点: %s' % (before if before else '读不到'))
        lines.append('现在的任务点: %s' % (self._job_snapshot(job_frame) or '读不到'))
        tips = self._limit_tips()
        lines.append('平台限制提示: %s' % (' / '.join(tips) if tips else '(没有)'))
        lines.append('')
        lines.append('===== 各 frame 记到的"任务点完成"消息 =====')
        for fr in self._page_frames():
            try:
                msgs = fr.evaluate(_JOB_MSG_JS)
            except Exception as exc:
                msgs = '读取失败: %s' % exc
            lines.append('%s -> %s' % (getattr(fr, 'url', ''), msgs))
        try:
            path.write_text(chr(10).join(lines), encoding='utf-8')
            log.info('任务点未完成的现场已写入: %s', path)
        except Exception as exc:
            log.warning('写任务点调试文件失败: %s', exc)

    def _ensure_job_finished(self, video, job_frame, before, job_now,
                             final: bool = False) -> bool:
        '''视频播完后, 确认平台真的把这一段的任务点标成了完成

        超星的链路是: 播放器播完 -> 给父页面发一条 JOB_FINISH_INFO ->
        父页面给任务点加 ans-job-finished。消息丢了或父页面没来得及处理,
        就成了"视频播完了、弹题也过了, 任务点却显示未完成"。

        这里不再自己想办法逼平台补一次上报(把结尾重放一遍那种做法平台并不
        认): 播完只等 JOB_FINISH_WAIT_FIRST 秒, 没标上就返回 False, 由上层
        把整段从头重播一遍 —— 那才是平台认的"又看了一遍"。

        final=True 表示这次已经是重播的那一遍了, 没有下一次: 再标不上就记一
        笔, 并把现场写到 调试-任务点未完成.txt。
        '''
        if not JOB_FINISH_CHECK or job_frame is None:
            return True
        ok, why = self._wait_job_finished(job_frame, before, job_now,
                                          timeout=JOB_FINISH_WAIT_FIRST)
        if ok:
            if why:
                log.info(why)
            return True
        tips = self._limit_tips()
        if tips:
            for tip in tips:
                log.warning('平台弹了限制提示, 这一段暂时没法完成(得等它解除): %s',
                            tip)
            self.job_missed += 1
            self._dump_job_diagnostics(video, job_frame, before, job_now)
            return True
        if final:
            log.warning('这一段重播完了, 平台还是没把任务点标成完成')
            self.job_missed += 1
            self._dump_job_diagnostics(video, job_frame, before, job_now)
            return False
        log.warning('这一段的任务点还没被标成完成, 直接从头重播一遍……')
        return False

    def _video_key(self, video):
        '''这一段对应哪张任务点卡片(拿它的序号当"这一页第几段"的编号)'''
        info = self._video_job(video)
        if isinstance(info, dict):
            try:
                idx = int(info.get('idx', -1))
            except Exception:
                idx = -1
            if idx >= 0:
                return idx
        return None

    def find_unfinished_video(self, frame, skip=None):
        '''只在"还没做完"的视频里找: 一页多个视频, 播完一个接着播下一个

        两个判据: 播放器放到结尾了没有, 以及它那张任务点卡片
        (div.ans-attach-ct) 被平台标成"完成"没有。只播到结尾不等于任务点就算完了 ——
        一页多个视频时, 平台没给标的那一段必须再挑出来补播, 否则这一页永远只
        标得上第一段。

        skip 里的任务点序号(已经补过几遍还是标不上的那几段)直接跳过,
        免得对着同一段反复重播。
        '''
        for item in self._video_list(frame):
            # 任务点已经被平台标成"完成"的那一段不用再播: 播放中"提前收尾"就是这样
            # 退出来的 —— 那时候视频还没播到结尾, 只看 done 会把它反复挑回来,
            # 同一段来回播个没完(日志里"这一页还有第 4/5/6 个视频"全是同一段)
            if item.get('jobPending') is False:
                continue
            if item.get('done') and item.get('jobPending') is not True:
                continue
            try:
                loc = frame.locator('video').nth(int(item['idx']))
            except Exception:
                continue
            # skip 里记的是"这一段补过几遍": 补到上限还没被平台标上才跳过它,
            # 没到上限的还要挑回来补(不然"回头再补"永远轮不到它)
            if skip and skip.get(self._video_key(loc), 0) >= MULTI_PAGE_VIDEO_PENDING:
                continue
            return loc
        for child in getattr(frame, "child_frames", []) or []:
            try:
                found = self.find_unfinished_video(child, skip)
                if found:
                    return found
            except Exception:
                pass
        return None

    def switch_to_course_tab(self) -> bool:
        '''超星章节页顶部有课程内容 / 学习检测两个页签,'''
        '''我们要的是视频, 所以停在"学习检测"时切回内容页签'''
        for sel in CONTENT_TAB_CANDIDATES:
            try:
                el = self.page.query_selector(sel)
            except Exception:
                el = None
            if not el:
                continue
            try:
                el.click(timeout=2000)
                return True
            except Exception:
                continue
        return False

    # -------------------------------------------------------- 章节内卡片
    def list_cards(self) -> list:
        '''列出当前章节的卡片(安全知识 / 学习检测 / ...)'''
        js = _fill_js(
            """
            () => {
              const out = [];
              const items = document.querySelectorAll(__SEL__);
              for (let i = 0; i < items.length; i++) {
                const el = items[i];
                const title = (el.getAttribute("title") || el.textContent || "").trim();
                out.push({
                  index: i + 1,
                  title: title,
                  cardid: el.getAttribute("cardid") || "",
                  active: el.classList.contains("active"),
                });
              }
              return out;
            }
            """,
            sel=CARD_LIST_SELECTOR,
        )
        try:
            return self.page.evaluate(js) or []
        except Exception:
            return []

    def open_card(self, index: int) -> bool:
        '''切到第 index 张卡片(走页面自己的切换逻辑)'''
        js = _fill_js(
            """
            (i) => {
              const items = document.querySelectorAll(__SEL__);
              if (!items.length) return false;
              let n = parseInt(i, 10);
              if (isNaN(n) || n < 1) n = 1;
              if (n > items.length) n = items.length;
              const el = items[n - 1];
              try { el.scrollIntoView({block: "center"}); } catch (e) {}
              try { el.click(); } catch (e) {}
              return true;
            }
            """,
            sel=CARD_LIST_SELECTOR,
        )
        try:
            return bool(self.page.evaluate(js, index))
        except Exception:
            return False

    def _card_signature(self, frame) -> str:
        '''用内容区里的附件 jobid 拼一个"当前是哪张卡片"的指纹'''
        try:
            return frame.evaluate(
                "() => Array.from(document.querySelectorAll('iframe[jobid]'))"
                ".map(f => f.getAttribute('jobid')).join(',')"
            ) or ""
        except Exception:
            return ""

    def switch_card(self, index: int) -> bool:
        '''切到第 index 张卡片, 并等内容区真的换成新卡片再返回'''
        before = None
        frame = self.find_content_frame()
        if frame:
            before = self._card_signature(frame)
        if not self.open_card(index):
            return False
        if not before:
            time.sleep(2.0)
            return True
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            time.sleep(0.6)
            frame = self.find_content_frame()
            if not frame:
                continue
            now = self._card_signature(frame)
            if now and now != before:
                time.sleep(0.8)
                return True
        log.warning("切到卡片 %d 后内容区没换过, 按当前内容继续", index)
        return True

    # -------------------------------------------------------- 反切屏保活
    def install_keep_alive(self) -> int:
        '''给所有 frame(含嵌套)装上防切屏补丁; 幂等, 新加载的 frame 会自动重装'''
        done = 0
        for fr in self._page_frames():
            try:
                fr.evaluate(_KEEP_ALIVE_JS)
                done += 1
            except Exception:
                pass
        return done

    def install_autoplay(self) -> int:
        '''给所有 frame(含嵌套)装自动起播补丁; 幂等, 新 frame 会自动重装

        注意: 补丁装上去是关着闸门的(见 _POKE_ON_JS), 必须再用 _focus_video
        单独打开"正在看的那个播放器"; 一页多个播放器一起起播会被平台
        判成操作异常(9010 验证码)。
        '''
        if not AUTOPLAY_POKE:
            return 0
        done = 0
        for fr in self._page_frames():
            try:
                fr.evaluate(_POKE_JS)
                done += 1
            except Exception:
                pass
        return done

    def _frame_of_video(self, video):
        '''找出这个 <video> 挂在哪个 frame 上

        做法: 先给每个 frame 的 window 打一个临时序号, 再看 video 自己的
        window 上挂着哪个序号 —— 一页排了好几个播放器时, 这个答案就是
        "只操作这一个"的关键(不区分的话它们会一起起播, 立刻触发 9010 风控)。
        '''
        tagged = []
        for idx, fr in enumerate(self._page_frames()):
            js = _fill_js('() => { window.__cx_frame_tag = __IDX__; return 1; }',
                          idx=idx)
            try:
                fr.evaluate(js)
            except Exception:
                continue
            tagged.append((idx, fr))
        if not tagged:
            return None
        try:
            tag = video.evaluate(_FRAME_TAG_READ_JS)
        except Exception:
            return None
        for idx, fr in tagged:
            if idx == tag:
                return fr
        return None

    def _focus_video(self, video) -> None:
        '''只给"正在看的这个播放器"开自动补播闸门, 同页其它播放器一律关掉'''
        if not AUTOPLAY_POKE:
            return
        target = self._frame_of_video(video)
        self.install_autoplay()
        for fr in self._page_frames():
            try:
                fr.evaluate(_POKE_OFF_JS)
            except Exception:
                pass
        if target is None:
            return
        try:
            target.evaluate(_POKE_ON_JS)
        except Exception:
            pass

    # -------------------------------------------------------- 超星风控
    def _on_frame_navigated(self, frame) -> None:
        '''页面一跳风控验证页就马上留个记号(重活留给 _check_antispider)'''
        try:
            url = frame.url or ''
        except Exception:
            return
        if _looks_like_antispider(url):
            self.antispider_seen = True
            log.error('检测到页面跳到风控验证页: %s', url)

    def _scan_antispider(self) -> str:
        '''扫一遍主页面和所有 frame, 返回风控验证页的网址(没有就返回空串)'''
        urls = []
        try:
            urls.append(self.page.url or '')
        except Exception:
            pass
        for fr in self._page_frames():
            try:
                urls.append(fr.url or '')
            except Exception:
                continue
        for url in urls:
            if _looks_like_antispider(url):
                return url
        return ''

    def _enter_antispider(self, url: str = '') -> None:
        '''进入"停手等人工过验证"状态(只生效一次)'''
        if self.stopped:
            return
        self.stopped = True
        log.error('=' * 60)
        log.error('超星弹出了风控验证页(操作异常 9010): 脚本立刻停手, 不再点任何东西')
        if url:
            log.error('验证页网址: %s', url)
        log.error('请在浏览器窗口里手动输入图片验证码(输入框 ucode)')
        log.error('验证通过后脚本会自动接着刷, 最多等 %.0f 分钟',
                  ANTISPIDER_WAIT_MINUTES)
        log.error('=' * 60)
        self.dump_antispider_diagnostics(url)

    def _check_antispider(self) -> bool:
        '''发现风控验证页就停手; 返回 True 表示现在处于"已停手"状态'''
        if self.stopped:
            return True
        url = self._scan_antispider()
        if not url:
            self.antispider_seen = False
            return False
        self._enter_antispider(url)
        return True

    def _wait_antispider_cleared(self, minutes=None) -> bool:
        '''等人工在浏览器里过验证; 过了返回 True, 一直没过返回 False'''
        if not self.stopped:
            return True
        limit = ANTISPIDER_WAIT_MINUTES if minutes is None else float(minutes)
        deadline = time.monotonic() + max(0.0, float(limit)) * 60
        while time.monotonic() < deadline:
            if not self._scan_antispider():
                self.stopped = False
                self.antispider_seen = False
                self.antispider_dumped = False
                log.info('风控验证已通过, 继续刷课')
                return True
            time.sleep(3)
        log.error('等了 %.0f 分钟还没过验证, 这一段先放弃', limit)
        return False

    def _guard_antispider(self) -> bool:
        '''风控守门员: 没被拦返回 True; 被拦就停手等人工, 过了也返回 True'''
        if not self._check_antispider():
            return True
        return self._wait_antispider_cleared()

    def dump_antispider_diagnostics(self, url: str = '') -> None:
        '''把风控验证页的样子导出来(只写一次), 方便确认是哪种拦截'''
        if self.antispider_dumped:
            return
        self.antispider_dumped = True
        path = Path(__file__).resolve().parent / '调试-风控.txt'
        lines = ['超星弹出了风控验证页, 下面是当时的页面结构', '']
        lines.append('命中网址: %s' % (url or '未知'))
        lines.append('')
        for fr in self._page_frames():
            try:
                furi = fr.url or ''
            except Exception:
                furi = ''
            lines.append('===== frame: %s =====' % furi)
            for label, js in (
                ('video 个数', "() => document.querySelectorAll('video').length"),
                ('body HTML', "() => (document.body ? document.body.innerHTML : '')"),
            ):
                try:
                    value = fr.evaluate(js)
                except Exception as exc:
                    lines.append('%s 读取失败: %s' % (label, exc))
                    continue
                if label == 'body HTML' and isinstance(value, str):
                    value = value[:40000]
                lines.append('%s: %s' % (label, value))
            lines.append('')
        try:
            path.write_text(chr(10).join(lines), encoding='utf-8')
            log.info('风控验证页结构已写入: %s', path)
        except Exception as exc:
            log.warning('写风控调试文件失败: %s', exc)


    def dismiss_blockers(self) -> None:
        '''自动点掉"继续学习"之类的遮罩'''
        for fr in self._page_frames():
            try:
                fr.evaluate(_DISMISS_JS)
            except Exception:
                pass

    def _wait_video_in_view(self, timeout: float = 20.0):
        '''等右侧内容区加载好并找到可见的视频, 返回 (frame, video)'''
        deadline = time.monotonic() + timeout
        frame = None
        tried_tab = False
        while time.monotonic() < deadline:
            frame = self.find_content_frame()
            if frame:
                video = self.find_visible_video(frame)
                if video:
                    return frame, video
            elif not tried_tab:
                tried_tab = True
                if self.switch_to_course_tab():
                    log.info("内容区还没出来, 先切回第一张卡片, 重新找视频……")
                    time.sleep(2)
                    continue
            time.sleep(0.5)
        return frame, None

    def _measure_rate(self, video, seconds: float = 3.0) -> float:
        '''用真实播放进度估算实际倍速, 用来确认倍速到底生效没有'''
        try:
            t0 = video.evaluate("v => v.currentTime || 0")
        except Exception:
            return 0.0
        time.sleep(seconds)
        try:
            t1 = video.evaluate("v => v.currentTime || 0")
        except Exception:
            return 0.0
        return (t1 - t0) / seconds

    def _retry_rate(self) -> float:
        '''重播那一段用的倍速; 没配 RETRY_RATE 就还是原倍速'''
        if RETRY_RATE and float(RETRY_RATE) > 0:
            return float(RETRY_RATE)
        return float(self.rate)

    @staticmethod
    def _segment_at_end(video) -> bool:
        '''这一段是不是已经放到结尾了(哪怕平台没给 ended)'''
        try:
            got = video.evaluate(
                "v => { const d = v.duration || 0;"
                " return !!d && (!!v.ended || v.currentTime >= d - 1); }")
        except Exception:
            return False
        return bool(got)

    def _play_segment(self, video, seen_to_end: bool = False) -> bool:
        '''播完一段视频; 没能完成就按 RETRY_RATE 重播一遍

        一页多个视频(情景剧/合辑)时**每一段**都要走这里 —— 不能只有第一段会重播,
        后面几段播完没标上任务点就放弃整页。

        重播直接拿同一个定位器再播一遍(它是按序号取的, 还指着同一段): 不切卡片、
        不重建 DOM —— 播放器还在原地, 从头再放一遍就能重新触发一次"看完了"上报,
        而且重播本身就是真实播放(默认仍按原倍速), 不做额外加速。

        seen_to_end=True 表示这一段之前就放到过结尾了(内容真看过, 只是平台没标
        任务点): 那第一遍就直接用 RETRY_RATE 补播, 不用再慢慢走一遍原倍速。
        '''
        if self._play_video(video, retry=seen_to_end):
            return True
        if seen_to_end:
            log.warning('这一段补播了一遍还是没能完成')
            return False
        log.warning('这一段没能完成, 改用 x%.1f 倍速重播一遍再试……',
                    self._retry_rate())
        if self._play_video(video, retry=True):
            return True
        log.warning('这一段重播了一遍还是没能完成')
        return False

    def _play_page_videos(self, frame, first) -> tuple:
        '''把这一页(卡片)的视频逐段播完, 每一段都核对任务点

        一页多个视频(情景剧/合辑)时, 后面每一段跟第一段走的是同一条路:
        播 -> 核对任务点 -> 没标上就按 RETRY_RATE 重播一遍。

        "播完了但平台没标任务点"的那一段不再直接丢掉: 先接着播后面几段,
        回头靠任务点状态再把它挑出来补播(最多 MULTI_PAGE_VIDEO_PENDING 遍)。
        平台是按学习顺序放行的, 前面几段没被标上时后面的可能压根没起来,
        所以这里必须按顺序往下走, 不能跳。

        返回 (播好的段数, 失败的段数)。
        '''
        done = 0
        failed = 0
        seen = 0
        misses = 0
        retries = {}          # 任务点序号 -> 已经为"平台没标上"补播过几遍
        video = first
        while seen < MULTI_PAGE_VIDEO_LIMIT and misses < MULTI_PAGE_VIDEO_MISSES:
            if video is None:
                time.sleep(2.0)
                video = self.find_unfinished_video(frame, retries)
                if video is None:
                    misses += 1
                    pending = self._attach_pending(frame)
                    if pending > 0 and misses < MULTI_PAGE_VIDEO_MISSES:
                        log.info('这一页还有 %d 个任务点没完成, 等播放器出来……',
                                 pending)
                        continue
                    break
            misses = 0
            seen += 1
            seg = video
            video = None
            # 这一段的视频任务点已经是"已完成"(通常就是刚"提前收尾"退出来的那段
            # 又被挑回来): 不用播, 也不再让它回到候选里, 直接看下一段
            if self._job_done_now(seg):
                key0 = self._video_key(seg)
                log.info('这一段的视频任务点已经标成"已完成", 不用再播, 直接看下一段')
                if key0 is not None:
                    retries[key0] = max(retries.get(key0, 0),
                                        MULTI_PAGE_VIDEO_PENDING)
                done += 1          # 任务点已经完成, 也算这一段做好了
                continue
            if seen > 1:
                log.info('这一页还有第 %d 个视频, 接着往下播……', seen)
            at_end = self._segment_at_end(seg)
            if at_end:
                log.info('这一段之前已经放到过结尾(只是平台没标任务点),'
                         ' 直接用 x%.1f 倍速补一遍……', self._retry_rate())
            if self._play_segment(seg, seen_to_end=at_end):
                done += 1
                continue
            failed += 1
            if not self._segment_at_end(seg):
                log.warning('这一段没能播完(可能被平台按学习顺序锁着),'
                            ' 这一页剩下的视频先不播了')
                break
            key = self._video_key(seg)
            if key is None:
                log.warning('这一段播完了, 但平台没把任务点标成完成')
                continue
            retries[key] = retries.get(key, 0) + 1
            if retries[key] >= MULTI_PAGE_VIDEO_PENDING:
                log.warning('这一段平台一直没把任务点标成完成(已经补播 %d 遍),'
                            ' 这一页先跳过它', retries[key])
            else:
                log.warning('这一段播完了, 但平台没把任务点标成完成;'
                            ' 先接着播后面的视频, 回头再补')
        return done, failed

    def _play_video(self, video, retry: bool = False) -> bool:
        '''把一个视频播到结束, 并确认平台把这一段的任务点标成了完成

        retry=True 表示这是"第一遍播完了但平台没标任务点"之后的重播: 内容已经
        真看过一遍, 重播只是为了逼平台补一次上报, 所以用 RETRY_RATE 快进刷完。
        '''
        if not self._guard_antispider():
            return False
        if retry:
            self.rate_now = self._retry_rate()
            if self.rate_now > float(self.rate):
                log.info('这一段改用 x%.1f 倍速重播(第一遍已经正常看过一遍了)',
                         self.rate_now)
        else:
            self.rate_now = float(self.rate)
        # 播之前先记下这一段的任务点长什么样, 播完好核对平台到底标了没有
        job_frame = None
        job_before = None
        job_now = None
        if JOB_FINISH_CHECK:
            job_frame = self._job_frame(video)
            if job_frame is not None:
                job_before = self._job_snapshot(job_frame)
                job_now = self._video_job(video)
        self._focus_video(video)
        if VIDEO_QUIZ != 'off':
            self.handle_video_quiz(True)
        duration = self._wait_video_ready(video)
        log.info("找到视频, 时长: %s",
                 self._fmt(duration) if duration else "未知时长")

        started = self._try_play(video)
        locked = self._lock_rate()
        time.sleep(1.5)
        actual = self._current_rate(video)
        if actual and abs(actual - self.rate_now) > 0.05:
            log.warning("播放器把倍速改回了 x%.2f, 重新锁定……", actual)
            self._lock_rate()
            time.sleep(1.0)
            actual = self._current_rate(video)
        measured = self._measure_rate(video)
        tries = 0
        while measured < 0.2 and tries < 3:
            tries += 1
            log.warning("播放还没真正跑起来(实测 x%.2f), 第 %d 次重新唤起播放器……",
                        measured, tries)
            if self.handle_video_quiz(True):
                log.info("起不来的原因就是互动测验/弹题, 已经处理掉了")
            else:
                self._nudge(video)
            time.sleep(1.5)
            actual = self._current_rate(video)
            measured = self._measure_rate(video)
        log.info("视频开始播放(目标 x%.1f, 属性 x%.2f, 实测 x%.2f, 锁定 %d 个播放器)%s",
                 self.rate_now, actual, measured, locked,
                 "" if started else ", 未能自动播放, 将自动重试")
        if measured < self.rate_now * 0.5:
            log.warning("实测速度只有 x%.2f, 可能被播放器限速或被反复暂停,"
                        " 运行期间请把浏览器窗口放在前台", measured)
        finished = self._watch_until_finished(video)
        if not finished:
            log.warning("这一段没播完(播放器被顶掉或卡死), 标记为未完成")
            return False
        if not self._ensure_job_finished(video, job_frame, job_before, job_now,
                                         final=retry):
            log.warning('这一段按"没播完"处理, 交给上层重来一次')
            return False
        return True

    def handle_current_task(self) -> str:
        '''处理当前章节: 逐张卡片找视频, 找到就播完'''
        cards = self.list_cards() if HANDLE_ALL_CARDS else []
        if cards:
            order = ([c for c in cards if c.get("active")]
                     + [c for c in cards if not c.get("active")])
            log.info("本章共 %d 张卡片: %s", len(cards),
                     " / ".join("%d.%s" % (c["index"], c["title"] or "未命名")
                                for c in order))
        else:
            order = [{"index": 1, "title": "", "active": True}]

        videos_done = 0
        videos_failed = 0
        last_frame = None
        for pos, card in enumerate(order):
            if not self._guard_antispider():
                return "fail"
            title = card.get("title") or ("卡片%d" % card["index"])
            if any(mark in title for mark in CARD_TITLES_TO_SKIP):
                log.info("卡片「%s」按设置跳过(不处理)", title)
                continue
            if pos and HANDLE_ALL_CARDS:
                if not self.switch_card(card["index"]):
                    log.warning("切到卡片「%s」失败, 跳过", title)
                    continue
            frame, video = self._wait_video_in_view()
            if frame:
                last_frame = frame
            if not frame:
                log.warning("没有找到内容区 iframe, 该章节可能加载失败")
                return "fail"
            if not video:
                log.info("卡片「%s」里没有视频(测验/讨论/文档等), 跳过", title)
                continue
            # 每一段都走同一条路: 播 -> 核对任务点 -> 没标上就按 RETRY_RATE 重播。
            # 一页多个视频(情景剧/合辑)时第一段没成也不能把整页丢掉: 后面几段
            # 照样往下播, 回头再补那些"播完了平台却没标上任务点"的。
            if MULTI_VIDEO_PER_PAGE:
                done, failed = self._play_page_videos(frame, video)
            else:
                done = 1 if self._play_segment(video) else 0
                failed = 0 if done else 1
            videos_done += done
            videos_failed += failed
            if MULTI_VIDEO_PER_PAGE:
                left = self._attach_pending(frame)
                if left > 0:
                    log.warning("这一页还有 %d 个任务点没被标成完成"
                                "(已经处理 %d 段视频)", left, done + failed)
            if not HANDLE_ALL_CARDS:
                break

        if videos_failed:
            return "fail"
        if videos_done:
            return "video_done"
        if last_frame is not None and not self.content_dumped:
            self.content_dumped = True
            self.dump_content_diagnostics(last_frame)
        log.info("这一章所有卡片里都没有视频(测验/讨论/文档等), 按约定跳过")
        return "no_video"

    def dump_player_diagnostics(self) -> None:
        '''播放器中途消失时, 把内容区当时的样子导出来(定位弹题/重建)'''
        path = Path(__file__).resolve().parent / '调试-播放器消失.txt'
        lines = ['播放器元素中途消失, 下面是当时的页面结构', '']
        base = self.find_content_frame()
        frames = []
        if base is not None:
            frames = [base] + list(getattr(base, 'child_frames', []) or [])
        if not frames:
            frames = self._page_frames()
        for fr in frames:
            lines.append('===== frame: %s =====' % getattr(fr, 'url', ''))
            for label, js in (
                ('video 个数', "() => document.querySelectorAll('video').length"),
                ('iframe 个数', "() => document.querySelectorAll('iframe').length"),
                ('iframe 网址', "() => Array.from(document.querySelectorAll('iframe'))"
                               ".map(f => f.getAttribute('src') || '').join(' ; ')"),
                ('候选浮层', _QUIZ_DUMP_JS),
                ('压视频的悬浮层', _OVERLAY_DUMP_JS),
                ('body HTML', "() => (document.body ? document.body.innerHTML : '')"),
            ):
                try:
                    value = fr.evaluate(js)
                except Exception as exc:
                    lines.append('%s 读取失败: %s' % (label, exc))
                    continue
                if label == 'body HTML' and isinstance(value, str):
                    value = value[:120000]
                lines.append('%s: %s' % (label, value))
            lines.append('')
        try:
            path.write_text(chr(10).join(lines), encoding='utf-8')
            log.info('播放器消失时的结构已写入: %s', path)
        except Exception as exc:
            log.warning('写播放器调试文件失败: %s', exc)
    def dump_content_diagnostics(self, frame) -> None:
        '''找不到视频时, 把内容区(含它的子 frame)结构导出来'''
        path = Path(__file__).resolve().parent / "调试-内容区.txt"
        lines = ["内容区网址: %s" % getattr(frame, "url", ""), ""]
        for label, js in (
            ("video 元素", "() => document.querySelectorAll('video').length"),
            ("iframe 元素", "() => document.querySelectorAll('iframe').length"),
        ):
            try:
                lines.append("%s 个数: %s" % (label, frame.evaluate(js)))
            except Exception as exc:
                lines.append("数 %s 失败: %s" % (label, exc))
        lines.append("")
        lines.append("===== 子 frame 列表 =====")
        children = []
        try:
            children = list(frame.child_frames)
        except Exception as exc:
            lines.append("列子 frame 失败: %s" % exc)
        for i, fr in enumerate(children):
            lines.append("[%d] %s" % (i, fr.url))
        lines.append("")
        lines.append("===== 内容区 HTML(截断到 400000 字符) =====")
        try:
            lines.append(frame.content()[:400000])
        except Exception as exc:
            lines.append("导出失败: %s" % exc)
        lines.append("")
        for i, fr in enumerate(children):
            lines.append("===== 子 frame[%d] %s =====" % (i, fr.url))
            try:
                lines.append(fr.content()[:200000])
            except Exception as exc:
                lines.append("导出失败: %s" % exc)
            lines.append("")
        try:
            path.write_text(chr(10).join(lines), encoding="utf-8")
            log.info("内容区结构已写入: %s", path)
        except Exception as exc:
            log.warning("写内容区调试文件失败: %s", exc)

    def _wait_video_ready(self, video) -> float:
        duration = 0.0
        for _ in range(40):
            try:
                info = video.evaluate(
                    'v => ({dur: v.duration || 0, cur: v.currentTime || 0})'
                )
                duration, _cur = info['dur'], info['cur']
                if duration and duration > 1:
                    return duration
            except Exception:
                pass
            time.sleep(0.5)
        return duration

    def _try_play(self, video) -> bool:
        '''让视频动起来: 先调 play(), 不行就去各个 frame 里点播放按钮'''
        for _ in range(3):
            try:
                ok = video.evaluate(
                    """
                    v => {
                      v.muted = true;
                      const p = v.play();
                      if (p && p.catch) p.catch(() => {});
                      return !v.paused;
                    }
                    """
                )
            except Exception:
                ok = False
            if ok:
                return True
            clicked = False
            owner = self._frame_of_video(video)
            frames = self._page_frames()
            if owner is not None:
                frames = [owner] + [fr for fr in frames if fr is not owner]
            for fr in frames:
                for sel in (
                    '.vjs-big-play-button',
                    '.vjs-play-control',
                    'button[class*="play"]',
                    '.pic_video',
                    '.popbtn',
                ):
                    try:
                        loc = fr.locator(sel).first
                        if loc.count() == 0:
                            continue
                        loc.click(timeout=800)
                        clicked = True
                        break
                    except Exception:
                        continue
                if clicked:
                    break
            if not clicked:
                try:
                    video.click(timeout=1000)
                except Exception:
                    pass
            time.sleep(0.8)
        return False

    def _page_frames(self) -> list:
        '''当前页面里所有 frame, 含嵌套的'''
        try:
            return list(self.page.frames)
        except Exception:
            return [self.page.main_frame]

    @staticmethod
    def _current_rate(video) -> float:
        '''读一下播放器当前的实际倍速'''
        try:
            value = video.evaluate("v => v.playbackRate")
        except Exception:
            return 0.0
        try:
            return float(value)
        except Exception:
            return 0.0

    def _lock_rate(self, hard: bool = True) -> int:
        '''把所有 frame 里的 video 都锁到目标倍速, 返回命中的播放器个数'''

        '''两个坑:'''
        '''1) 视频不在内容区那一层, 而在更深的子 frame 里, 所以每个 frame 都要设'''
        '''2) 超星播放器会自己把 playbackRate 改回 1, 软锁会被它改回来,'''
        '''   所以默认用 defineProperty 硬锁, 让它怎么赋值都没用'''
        js = _fill_js(
            _RATE_LOCK_JS,
            rate=float(self.rate_now),
            hard=1 if (hard and HARD_LOCK_RATE) else 0,
            video=SELECTORS["video"],
        )
        total = 0
        for fr in self._page_frames():
            try:
                total += fr.evaluate(js) or 0
            except Exception:
                pass
        return total

    def _video_info(self, video) -> dict:
        '''读一次播放器状态; 元素没了就返回空 dict'''
        try:
            return video.evaluate(
                '''
                v => ({
                  dur: v.duration || 0,
                  cur: v.currentTime || 0,
                  ended: v.ended,
                  paused: v.paused,
                  ready: v.readyState,
                  net: v.networkState,
                  rate: v.playbackRate,
                  buf: (v.buffered && v.buffered.length)
                       ? v.buffered.end(v.buffered.length - 1) : 0,
                  err: v.error ? (v.error.code || 0) : 0,
                  src: v.currentSrc || v.src || '',
                })
                '''
            ) or {}
        except Exception:
            return {}

    def _refind_video(self):
        '''播放器重建之后重新找一次 <video>'''
        frame = self.find_content_frame()
        if not frame:
            return None
        return self.find_visible_video(frame)

    def _nudge(self, video) -> None:
        '''被暂停/卡住时的组合拳: 点掉遮罩 -> 点一下画面 -> 调 play() -> 重锁倍速'''
        if KEEP_ALIVE:
            self.dismiss_blockers()
        try:
            video.click(timeout=1000)
        except Exception:
            pass
        try:
            video.evaluate(
                'v => { v.muted = true; const p = v.play();'
                ' if (p && p.catch) p.catch(() => {}); }'
            )
        except Exception:
            pass
        self._lock_rate()

    # -------------------------------------------------------- 视频弹题
    def _quiz_call(self, mode: str, n=None) -> dict:
        '''在所有 frame 里按 mode 处理一次弹题, 汇总各 frame 的结果'''
        payload = {'mode': mode, 'n': QUIZ_CHOICE if n is None else int(n)}
        total = {'boxes': [], 'over': [], 'optText': [],
                 'options': 0, 'submits': 0, 'closed': 0, 'cont': 0,
                 'answered': False, 'clicked': 0, 'fb': '', 'fbText': '',
                 'qTotal': 0, 'qRight': 0}
        for fr in self._page_frames():
            try:
                res = fr.evaluate(_QUIZ_JS, payload)
            except Exception:
                continue
            if not isinstance(res, dict):
                continue
            for key in ('boxes', 'over', 'optText'):
                values = res.get(key)
                if isinstance(values, list):
                    total[key].extend(values)
            for key in ('options', 'submits', 'closed', 'clicked', 'cont'):
                total[key] += int(res.get(key) or 0)
            # "共 N 题, 已答对 M 题": 认下来一组题有几道、已经过了几道
            if int(res.get('qTotal') or 0) > 0 and not total['qTotal']:
                total['qTotal'] = int(res.get('qTotal') or 0)
                total['qRight'] = int(res.get('qRight') or 0)
            if res.get('answered'):
                total['answered'] = True
            # 判分反馈: 只要有一个 frame 说答错了就先按答错算(宁可信其错)
            fb = res.get('fb')
            if fb == 'wrong' or (fb == 'right' and total['fb'] != 'wrong'):
                total['fb'] = fb
                total['fbText'] = res.get('fbText') or ''
        return total

    @staticmethod
    def _quiz_present(found: dict) -> bool:
        '''扫完之后判断到底有没有弹题'''
        if not isinstance(found, dict):
            return False
        if found.get('options') or found.get('submits') or found.get('over'):
            return True
        return bool(found.get('boxes'))

    @staticmethod
    def _quiz_desc(found: dict) -> str:
        '''把扫描结果压成一行日志'''
        parts = []
        for b in (found.get('boxes') or [])[:3]:
            parts.append('%s %sx%s %s' % (b.get('kind') or '容器',
                                          b.get('w'), b.get('h'),
                                          (b.get('text') or '')[:30]))
        for o in (found.get('over') or [])[:2]:
            parts.append('压在视频上 <%s class=%s (%s)> %s' % (
                o.get('tag'), o.get('cls'), o.get('hit'),
                (o.get('text') or '')[:24]))
        if found.get('options'):
            parts.append('选项 %d 个: %s' % (
                found['options'],
                ' / '.join((found.get('optText') or [])[:4])))
        elif found.get('submits'):
            parts.append('有提交按钮 %d 个' % found['submits'])
        if found.get('fb'):
            parts.append('平台反馈: %s(%s)' % (
                '回答正确' if found['fb'] == 'right' else '回答错误',
                found.get('fbText') or ''))
        if found.get('cont'):
            parts.append('有"继续学习"按钮 %d 个' % int(found['cont']))
        return ' | '.join(parts) or '识别到可疑浮层'

    def _quiz_dump(self, reason: str = '', found: dict = None) -> None:
        '''把弹题的现场导一份出来(含视频上压着的浮层), 方便补选择器'''
        path = Path(__file__).resolve().parent / '调试-弹题.txt'
        lines = ['弹题/互动测验现场取证', '原因: %s' % (reason or '未知'), '']
        if isinstance(found, dict) and found:
            lines.append('上一次扫描: %s' % self._quiz_desc(found))
            lines.append('判分反馈: %s(%s) | 继续学习按钮: %d 个 | 选项: %d 个'
                         % (found.get('fb') or '没读到',
                            found.get('fbText') or '-',
                            int(found.get('cont') or 0),
                            int(found.get('options') or 0)))
            lines.append('')
        got = False
        for fr in self._page_frames():
            try:
                text = fr.evaluate(_QUIZ_DUMP_JS) or ''
            except Exception as exc:
                text = '读取失败: %s' % exc
            if not text:
                continue
            got = True
            lines.append('===== frame: %s =====' % getattr(fr, 'url', ''))
            lines.append(text)
            lines.append('')
        if not got:
            lines.append('(所有 frame 都没抓到结构, 下面是每个 frame 的悬浮层)')
            lines.append('')
            for fr in self._page_frames():
                lines.append('frame: %s' % getattr(fr, 'url', ''))
                try:
                    lines.append(fr.evaluate(_OVERLAY_DUMP_JS) or '(无悬浮层)')
                except Exception as exc:
                    lines.append('读取失败: %s' % exc)
                lines.append('')
        try:
            path.write_text(chr(10).join(lines), encoding='utf-8')
            log.info('弹题现场已写入: %s', path)
        except Exception as exc:
            log.warning('写弹题调试文件失败: %s', exc)

    def _quiz_finish(self) -> None:
        '''答完/绕开之后点掉"继续学习", 否则视频不会接着播'''
        got = self._quiz_call('continue')
        n = int((got or {}).get('cont') or 0)
        if n:
            log.info('已点"继续学习"(%d 次点击), 视频接着播', n)
        self.dismiss_blockers()
        # 答题期间是我们把视频停住的, 收尾之后要把它放回去接着播
        self._release_video(self.quiz_video)

    def handle_video_quiz(self, dump_miss: bool = False) -> bool:
        '''视频弹题/互动测验: 能点掉就点掉, 点不掉就作答, 答错了换下一个选项

        四个硬规矩:
        1) 互动测验必须答完, 否则这个视频的任务点不会被判完成;
        2) 提交之后浮层不会自己消失 —— 它显示"回答正确", 要再点一次"继续学习",
           视频才会接着播;
        3) 浮层不一定暂停视频, 答题期间要先把视频停住, 否则 2 倍速下它自己就跑到
           结尾了 —— 末尾那道题尤其明显: 拉回去又触发一次, 来回就是死循环;
        4) 一组题(浮层上写着"共 N 题, 已答对 M 题")必须整组答对才点"继续学习":
           少答一道就收尾, 平台会把视频退回这个知识点重播, 题再弹一次 —— 这就是
           "答完题视频被拉回开头重播"那个死循环。

        所以判断顺序是: 先读平台反馈 + 题数 -> 整组过了就点继续, 没过就换下一个
        选项重来, 过了一题接着答下一题。
        '''
        if VIDEO_QUIZ == 'off':
            return False
        found = self._quiz_call('scan')
        q_total = int(found.get('qTotal') or 0)
        q_right = int(found.get('qRight') or 0)
        # 上一轮已经答对, 浮层还停在"回答正确"上: 点继续学习收尾
        # (一组有多题时, 只有整组都答对才算过)
        if found.get('fb') == 'right' and (not q_total or q_right >= q_total):
            log.info('平台反馈: 回答正确(%s), 点"继续学习"接着播',
                     found.get('fbText') or '')
            self._quiz_finish()
            return True
        if not self._quiz_present(found):
            if dump_miss and self.quiz_miss_dumps < 2:
                self.quiz_miss_dumps += 1
                log.warning('视频卡住了, 但没识别出弹题, 先把现场导一份出来')
                self._quiz_dump('视频卡住但没有识别出弹题')
            return False
        log.info('检测到视频弹题/互动测验: %s', self._quiz_desc(found))
        if q_total:
            log.info('这组互动测验共 %d 题, 已答对 %d 题', q_total, q_right)
            if q_right >= q_total:
                log.info('这组题已经全部答对, 点"继续学习"接着播')
                self._quiz_finish()
                return True
        # 弹题浮层不一定暂停视频: 先把它停住, 免得答题这几秒钟视频自己冲到结尾
        self._hold_video(self.quiz_video)
        if QUIZ_DUMP_ALWAYS:
            self._quiz_dump('检测到弹题', found)
        if VIDEO_QUIZ != 'answer':
            got = self._quiz_call('bypass')
            if got.get('closed'):
                log.info('已点掉弹题浮层(%d 次点击)', got['closed'])
                self._quiz_finish()
                return True
            if VIDEO_QUIZ == 'bypass':
                log.warning('弹题没点掉, 按设置不代为作答; 这一段要你自己点一下')
                self._release_video(self.quiz_video)
                return False
        opts = max(1, min(int(found.get('options') or 1), QUIZ_MAX_TRIES))
        first = (max(1, int(QUIZ_CHOICE)) - 1) % opts + 1
        # 答错就换下一个选项, 每个选项都试一遍(至少两轮)
        rounds = max(2, min(opts, QUIZ_MAX_TRIES))
        # 一组题里还剩几道: 答对一道就换下一道, 从头再试选项
        left = min(max(1, q_total - q_right), QUIZ_GROUP_MAX) if q_total else 1
        for _q_no in range(1, left + 1):
            tried = []
            for round_no in range(1, rounds + 1):
                pick = (first - 1 + round_no - 1) % opts + 1
                if pick in tried:
                    # 同一个选项已经点过了, 再点一遍还是同一个结果, 别空转
                    continue
                tried.append(pick)
                got = self._quiz_call('answer', pick)
                if not got.get('answered'):
                    log.warning('弹题里没找到可点的选项, 现场在 调试-弹题.txt')
                    self._quiz_dump('找不到可点的选项')
                    self._release_video(self.quiz_video)
                    return False
                log.info('弹题已作答并提交(第 %d 个选项, 共 %d 个)', pick, opts)
                time.sleep(QUIZ_VERIFY_WAIT)
                now = self._quiz_call('scan')
                fb = now.get('fb')
                now_total = int(now.get('qTotal') or 0)
                now_right = int(now.get('qRight') or 0)
                if now_total and now_right > q_right:
                    # 平台把"已答对"加了一: 这道题它认了, 接着答下一道
                    q_right = now_right
                    q_total = q_total or now_total
                    log.info('这题答对了(已答对 %d/%d 题)', q_right, q_total)
                    break
                if fb == 'right':
                    if not now_total or now_right >= now_total:
                        log.info('平台反馈: 回答正确(%s), 点"继续学习"接着播',
                                 now.get('fbText') or '')
                        self._quiz_finish()
                        return True
                    q_right = now_right
                    break
                if not self._quiz_present(now):
                    log.info('弹题浮层已消失, 继续播放')
                    self._quiz_finish()
                    return True
                if fb == 'wrong':
                    log.info('平台反馈: 回答错误(%s), 换下一个选项重试',
                             now.get('fbText') or '')
                elif now_total:
                    # 有题数就不猜了: 浮层上冒出"继续学习"不等于这题过了 ——
                    # 少答一道就收尾, 平台会把视频退回这个知识点重播(死循环)
                    log.info('这题平台还没认(仍是已答对 %d/%d 题), 换下一个选项',
                             now_right, now_total)
                elif int(now.get('cont') or 0) > int(found.get('cont') or 0):
                    # 没读到文字反馈, 但浮层上多出了"继续学习": 说明这题已经过了
                    log.info('浮层上出现了"继续学习", 这题已经过了')
                    self._quiz_finish()
                    return True
                else:
                    log.info('浮层还在, 但没读到对错反馈, 换个选项接着试……')
            if q_total and q_right >= q_total:
                log.info('这组互动测验 %d 题已经全部答对, 点"继续学习"接着播',
                         q_total)
                self._quiz_finish()
                return True
        log.warning('试了 %d 轮还没把弹题答对, 现场在 调试-弹题.txt', rounds)
        self._quiz_dump('多轮作答后浮层仍在', found)
        self._release_video(self.quiz_video)
        return False

    def _hold_video(self, video) -> None:
        '''弹题浮层挡着的时候先把这一段停住'''

        '''超星的互动测验不一定会暂停视频: 边答题边播, 2 倍速下几秒钟就能冲到结尾,'''
        '''结果"题答完了, 视频也播完了/被平台拉回开头了", 后面怎么拉都对不上。'''
        ''''''
        if not QUIZ_PAUSE_VIDEO or video is None:
            return
        try:
            video.evaluate("v => { try { v.pause(); } catch (e) {} }")
        except Exception:
            pass

    def _pause_video(self, video) -> None:
        '''这一段已经不用再播了(任务点已完成), 先把它停住再看下一个'''
        try:
            video.evaluate("v => { try { v.pause(); } catch (e) {} }")
        except Exception:
            pass

    def _release_video(self, video) -> None:
        '''弹题处理完, 把这一段接着播起来'''
        if not QUIZ_PAUSE_VIDEO or video is None:
            return
        try:
            video.evaluate("v => { const p = v.play();"
                           " if (p && p.catch) p.catch(() => {}); }")
        except Exception:
            pass
        self._lock_rate()

    def _seek_wait(self, video, target: float, timeout: float) -> float:
        '''等"粘性拉回"把进度压到位, 返回最后读到的进度

        页面里那个定时器会一直压, 但它得等播放器重载完才能压住 —— 刚赋值完就判定
        一定会误判成失败, 所以这里盯几秒再下结论。
        '''
        target = float(target or 0)
        deadline = time.monotonic() + max(0.0, float(timeout))
        back = 0.0
        while True:
            cur = -1.0
            try:
                raw = video.evaluate(_SEEK_READ_JS)
                if raw is not None:
                    cur = float(raw)
            except Exception:
                cur = -1.0
            info_cur = float((self._video_info(video) or {}).get("cur") or 0)
            back = max(cur, info_cur)
            if target > 0 and back >= target - 3:
                return back
            if time.monotonic() >= deadline:
                return back
            time.sleep(0.5)

    def _seek_to(self, video, pos) -> bool:
        '''把播放器拉回之前的位置: 答完题/点完"继续学习"后平台常从 0 重新播

        这里最容易踩的坑是"赋值了却没用": 平台在这时候常把播放器整个重载一遍,
        我们在旧元素上写的 currentTime 会被它的初始化抹掉 —— 日志里就是
        "想拉回 2分35秒, 但播放器还停在 0分07秒"(读回来还是原样, 进度在白走)。
        所以改成往页面里装一个"粘性拉回"(_SEEK_GUARD_JS): 它盯着这个播放器,
        进度一退回去就再压一次, 直到真的压到位(或超时自己收手)。
        '''
        pos = float(pos or 0)
        if pos <= 1.0:
            return False
        dur = float((self._video_info(video) or {}).get("dur") or 0)
        if dur > 3 and pos > dur - 2.0:
            # 贴到结尾时播放器会拒绝这次 seek, 往回让一点
            pos = dur - 2.0
        try:
            video.evaluate(_fill_js(_SEEK_GUARD_JS, target=pos, nearend=3.0,
                                    hold=int(float(QUIZ_SEEK_HOLD) * 1000),
                                    video=SELECTORS["video"]))
        except Exception as exc:
            log.warning("把播放器拉回 %s 失败: %s", self._fmt(pos), exc)
            return False
        back = self._seek_wait(video, pos, QUIZ_SEEK_VERIFY)
        if back >= pos - 3:
            log.info("平台把进度拉回了开头, 已拉回 %s 继续播", self._fmt(back))
            return True
        log.warning("想拉回 %s, 但播放器还停在 %s(页面里还在持续压住进度)",
                    self._fmt(pos), self._fmt(back))
        return False

    def _seek_end(self, video) -> bool:
        '''测验就卡在视频末尾时, 直接把进度推到结尾'''

        '''这种位置拉回原位等于又放回刚触发测验的地方 —— 立刻再弹一次, 答完再重播,'''
        '''就是死循环。推到结尾既满足"看完", 也不会把整段重播一遍。'''
        ''''''
        '''和 _seek_to 一样, 单次赋值压不住平台的重载, 所以也交给"粘性拉回":'''
        '''传 target=0 表示目标就是"时长 - 3"。'''
        ''''''
        dur = float((self._video_info(video) or {}).get("dur") or 0)
        if dur <= 4:
            return False
        target = dur - 3.0
        try:
            video.evaluate(_fill_js(_SEEK_GUARD_JS, target=0.0, nearend=3.0,
                                    hold=int(float(QUIZ_SEEK_HOLD) * 1000),
                                    video=SELECTORS["video"]))
        except Exception as exc:
            log.warning("把进度推到结尾失败: %s", exc)
            return False
        back = self._seek_wait(video, target, QUIZ_SEEK_VERIFY)
        if back >= target - 3:
            log.info("已把进度推到结尾(%s), 让这一段自己收尾", self._fmt(back))
            return True
        log.warning("想推到结尾, 但播放器还停在 %s", self._fmt(back))
        return False

    def _finish_tail(self, video) -> bool:
        '''停在最后一秒不算播完: 逼播放器把最后一点真正放完, 发出 ended 通知

        超星的"任务点完成"上报是挂在 ended 那一刻的。视频只是停在最后一秒
        (被暂停, 或被平台拉到了结尾)时, 进度看着是 100%, 但 ended 从没响过,
        父页面就收不到 JOB_FINISH_INFO —— 于是就成了"视频看完了、弹题也过了,
        任务点却显示未完成"。所以这里必须补这一下。
        '''
        js = ("(v => { try { const d = v.duration || 0; if (!d) return;"
              " if (v.currentTime >= d - 0.6) {"
              " v.currentTime = Math.max(0, d - 1.5); }"
              " v.muted = true;"
              " const p = v.play(); if (p && p.catch) p.catch(() => {});"
              " } catch (e) {} })")
        try:
            video.evaluate(js)
        except Exception:
            return False
        deadline = time.monotonic() + max(2.0, float(JOB_TAIL_WAIT))
        while time.monotonic() < deadline:
            info = self._video_info(video)
            if not info:
                return False
            if info.get('ended'):
                log.info('播放器已发出"看完"通知(ended), 这一段才算真播完')
                return True
            time.sleep(0.5)
        log.warning('把最后一秒放完了, 播放器还是没发出"看完"通知;'
                    ' 任务点可能标不上, 后面会再核对')
        return False

    def _watch_until_finished(self, video) -> bool:
        '''盯着播放器把这一段看完; 返回 True 才代表真的播到结尾了'''
        start = time.monotonic()
        deadline = start + MAX_TASK_MINUTES * 60
        last_pos = -1.0
        last_key = ''
        stalled_since = 0.0
        stalled_reported = False
        near_end = 0
        last_log = 0.0
        last_lock = 0.0
        last_alive = 0.0
        lost_since = 0.0
        last_quiz = 0.0
        last_quiz_scan = 0.0
        last_job_check = 0.0
        paused_since = 0.0
        last_lost_try = 0.0
        seen_dur = 0.0
        best_cur = 0.0
        src = ''
        done = False
        # 弹题处理成功时记下当时的进度, 平台把视频拉回 0 之后好拉回来;
        # end/ends 专门管"弹题就卡在末尾"这种情况(拉回去只会又触发一次弹题)
        quiz_state = {'pos': 0.0, 'src': '', 'dur': 0.0, 'at': 0.0, 'seeks': 0,
                      'end': False, 'ends': 0, 'reps': 0}

        def try_quiz(dump_miss: bool = False) -> bool:
            '''处理一次弹题; 成功后记下当前进度'''
            self.quiz_video = video   # 答题期间让 handle_video_quiz 把这段停住
            try:
                ok = self.handle_video_quiz(dump_miss)
            finally:
                self.quiz_video = None
            if not ok:
                return False
            # 同一个位置又弹了一次: 说明平台把这一段退回去了, 光拉回原位只会又
            # 把它勾出来 —— 记下来, 这次拉回时往前让开一点; 换了新位置就清零
            if (quiz_state['pos'] > 1.0
                    and abs(best_cur - quiz_state['pos']) <= 8.0):
                quiz_state['reps'] += 1
            else:
                quiz_state['reps'] = 0
                quiz_state['seeks'] = 0
            quiz_state['pos'] = best_cur
            quiz_state['src'] = src
            quiz_state['dur'] = seen_dur
            quiz_state['at'] = time.monotonic()
            # 弹题落在这一段最后 10% 时, 答完题平台会把视频拉回 0:
            # 这时"拉回原位"等于又放回触发点, 得改成直接推到结尾
            quiz_state['end'] = bool(seen_dur > 1
                                      and best_cur >= seen_dur * QUIZ_END_RATIO)
            return True

        while time.monotonic() < deadline:
            # 风控验证页一出来就停手, 等人工过验证(超时就结束这一段)
            if not self._guard_antispider():
                return False
            info = self._video_info(video)

            if not info:
                now = time.monotonic()
                paused_since = 0.0
                if not lost_since:
                    lost_since = now
                    log.warning('视频元素暂时不可用(可能被弹题或播放器重建顶掉),'
                                ' 持续尝试重新定位……')
                elif now - last_lost_try >= 3.0:
                    last_lost_try = now
                    # 元素不在的时候也不能干等: 互动测验很可能就是把它顶掉的东西
                    if VIDEO_QUIZ != 'off' and try_quiz():
                        log.info('元素消失期间处理掉了弹题, 等播放器重建……')
                    self.dismiss_blockers()
                again = self._refind_video()
                if again is not None:
                    log.info('重新找到播放器, 继续盯进度')
                    video = again
                    last_pos = -1.0
                    last_key = ''
                    lost_since = 0.0
                    continue
                if now - lost_since > LOST_GRACE:
                    pct = (best_cur / seen_dur * 100) if seen_dur > 1 else 0.0
                    if seen_dur > 1 and (pct >= 88 or best_cur >= seen_dur - 1):
                        log.info('已看到 %.0f%% 且元素消失 %.0f 秒, 视为这一段播完',
                                 pct, now - lost_since)
                        return True
                    log.warning('视频元素消失 %.0f 秒, 但只看到 %.0f%%(时长 %s),'
                                ' 不能算播完, 这一段要重来',
                                now - lost_since, pct,
                                self._fmt(seen_dur) if seen_dur > 1 else '未知')
                    self.dump_player_diagnostics()
                    return False
                time.sleep(2)
                continue
            lost_since = 0.0

            dur = info.get('dur') or 0
            cur = info.get('cur') or 0
            ended = info.get('ended')
            paused = info.get('paused')
            ready = int(info.get('ready') or 0)
            net = int(info.get('net') or 0)
            buf = info.get('buf') or 0
            rate = info.get('rate')
            err = int(info.get('err') or 0)
            src = info.get('src') or ''
            key = '%s@%d' % (src, round(dur * 10))
            if dur > 1:
                seen_dur = dur
            if cur > best_cur:
                best_cur = cur

            # 答完题/点完"继续学习"之后, 平台常把播放器重建并从 0 重播 ——
            # 同一个 src、时长也没变, 却倒退了这么多, 那就是被拉回开头了
            if (RESUME_AFTER_QUIZ and quiz_state['pos'] >= 10 and dur > 1
                    and (not src or src == quiz_state['src'])
                    and (not quiz_state['dur']
                         or abs(dur - quiz_state['dur']) < 2.0)
                    and cur + 10 <= quiz_state['pos']
                    and cur <= quiz_state['pos'] * 0.5
                    and time.monotonic() - quiz_state['at'] <= 120):
                if quiz_state['end']:
                    # 弹题就卡在末尾: 把视频拉回原位 = 又放回触发弹题的地方,
                    # 立刻再弹一次, 答完又重播 —— 来回几下就是死循环。
                    # 这种位置直接把进度推到结尾, 既算"看完"又不会重播整段。
                    quiz_state['ends'] += 1
                    quiz_state['at'] = time.monotonic()
                    if quiz_state['ends'] > QUIZ_END_GIVEUP:
                        log.warning('这段视频末尾的弹题已经答过, 也算看到 %.0f%% 了,'
                                    ' 平台还在反复把视频拉回开头; 不再重播, 这一段算完成',
                                    best_cur / dur * 100)
                        return True
                    if self._seek_end(video):
                        quiz_state['pos'] = 0.0
                        last_pos = -1.0
                        near_end = 0
                        stalled_since = 0.0
                        continue
                    log.warning('想直接推到结尾, 但播放器又跳回了开头, 再试一次……')
                    continue
                if quiz_state['seeks'] < QUIZ_SEEK_MAX:
                    quiz_state['seeks'] += 1
                    quiz_state['at'] = time.monotonic()
                    # 同一道题又被弹回来时(平台把这一段退回去了), 拉回的位置往后
                    # 让一点 —— 正好拉回触发点又会被它立刻勾出来, 来回就是死循环
                    target = quiz_state['pos']
                    if quiz_state['reps']:
                        target += QUIZ_SEEK_MARGIN
                    if self._seek_to(video, target):
                        last_pos = -1.0
                        near_end = 0
                        stalled_since = 0.0
                        continue

            if last_key and key != last_key and dur > 1 and cur < dur * 0.2:
                log.info('检测到播放器已切换到下一段视频, 继续播放……')
                last_pos = -1.0
                near_end = 0
                last_key = key
                time.sleep(1)
                continue
            if not last_key and dur > 0:
                last_key = key

            elapsed = time.monotonic() - start
            if elapsed - last_log >= 30:
                pct = cur / dur * 100 if dur > 1 else 0
                log.info('播放中: %.0f%% (%s / %s)',
                         pct, self._fmt(cur), self._fmt(dur))
                last_log = elapsed
            # 任务点已经被标成"已完成"就不用再等它播到结尾了: 直接收尾去下一个视频
            if JOB_EARLY_EXIT and elapsed - last_job_check >= JOB_EARLY_INTERVAL:
                last_job_check = elapsed
                if self._job_done_now(video):
                    pct = cur / dur * 100 if dur > 1 else 0
                    log.info('这一段的任务点已经标成"已完成"(播到 %.0f%%), 不用再等它'
                             ' 播到结尾, 直接收尾切下一个视频', pct)
                    self._pause_video(video)
                    return True
            if elapsed - last_lock >= RATE_LOCK_INTERVAL:
                self._lock_rate()
                last_lock = elapsed
            if elapsed - last_alive >= KEEP_ALIVE_INTERVAL:
                if KEEP_ALIVE:
                    self.install_keep_alive()
                    self.dismiss_blockers()
                # 闸门只开给当前这一段, 别的播放器不动
                self._focus_video(video)
                last_alive = elapsed
            # 互动测验/弹题: 必须答完, 否则这个视频任务点不计入
            # 有的浮层不会暂停视频, 只等暂停或卡住会整段漏掉 —— 所以主动扫
            if VIDEO_QUIZ != 'off' and elapsed - last_quiz_scan >= QUIZ_SCAN_INTERVAL:
                last_quiz_scan = elapsed
                if try_quiz():
                    log.info('播放中处理掉了互动测验/弹题, 继续播放')
                    last_pos = -1.0
                    stalled_since = 0.0
                    paused_since = 0.0

            if ended or (dur > 1 and cur >= dur - 1):
                near_end += 1
                if near_end >= 3:
                    done = True
                    if not ended:
                        # 只是"停在最后一秒"不算播完: 平台的任务点完成通知挂在
                        # ended 上, 少了它就成了"视频看完了但任务点未完成"
                        self._finish_tail(video)
                    break
            else:
                near_end = 0

            if paused:
                if not paused_since:
                    paused_since = time.monotonic()
            else:
                paused_since = 0.0

            if (paused and paused_since and VIDEO_QUIZ != 'off'
                    and time.monotonic() - paused_since >= QUIZ_PAUSE_WAIT
                    and elapsed - last_quiz >= QUIZ_COOLDOWN):
                last_quiz = elapsed
                if try_quiz():
                    log.info('弹题已处理, 继续播放视频')
                    last_pos = -1.0
                    stalled_since = 0.0
                    paused_since = 0.0
                    time.sleep(1.5)
                    continue
            if abs(cur - last_pos) < 0.001 and cur > 0:
                if time.monotonic() - quiz_state['at'] < QUIZ_SETTLE:
                    # 刚答完弹题/刚做完 seek: 播放器在重新缓冲, 这不是"卡住"
                    stalled_since = 0.0
                    stalled_reported = False
                elif not stalled_since:
                    stalled_since = time.monotonic()
                    stalled_reported = False
                elif time.monotonic() - stalled_since > STALL_SECONDS:
                    quiz_done = False
                    if VIDEO_QUIZ != 'off':
                        last_quiz = time.monotonic() - start
                        quiz_done = try_quiz(True)
                    if quiz_done:
                        log.info('卡住的其实是课堂弹题, 已处理, 继续播放')
                    else:
                        if paused:
                            log.warning('视频被暂停(可能切出了窗口/被平台暂停), 尝试恢复……')
                        else:
                            log.warning('视频进度停滞超过 %.0f 秒但没被暂停, 尝试恢复……',
                                        STALL_SECONDS)
                        if not stalled_reported:
                            stalled_reported = True
                            log.info('卡住时的播放器状态: 就绪=%d 网络=%d 缓冲到=%s '
                                     '倍速=%s 暂停=%s 错误码=%d',
                                     ready, net, self._fmt(buf), rate, paused, err)
                        self._nudge(video)
                    stalled_since = 0.0
            else:
                stalled_since = 0.0
                stalled_reported = False

            last_pos = cur
            time.sleep(2)
        else:
            log.warning('单个任务点处理时间超过上限(%.0f 分钟), 提前结束',
                        MAX_TASK_MINUTES)
        return done

    @staticmethod
    def _fmt(seconds: float) -> str:
        seconds = int(seconds or 0)
        return '%d分%02d秒' % (seconds // 60, seconds % 60)

    # -------------------------------------------------------- 主流程
    def _pick_next_unit(self, attempted: set):
        '''重新读一遍章节树, 挑出下一个还没碰过的未完成章节'''

        '''每轮都重读, 是因为点开一章之后超星会重绘章节树,'''
        '''之前拿到的元素 id 会失效'''
        ''''''
        tally = {}
        for unit in self.collect_units_wait(8.0):
            text = " ".join((unit.get("text") or "").split())
            ordinal = tally.get(text, 0)
            tally[text] = ordinal + 1
            key = (text, ordinal)
            if key in attempted:
                continue
            if self._is_finished(unit.get("state", "")):
                continue
            unit["text"] = text
            unit["_key"] = key
            return unit
        return None

    def _count_unfinished(self) -> int:
        '''数一数章节树里还剩多少个未完成的章节'''
        count = 0
        for unit in self.collect_units_wait(8.0):
            if not self._is_finished(unit.get("state", "")):
                count += 1
        return count

    def run(self, headless: bool) -> None:
        self.wait_until_logged_in(headless)
        units = self.collect_units_wait(90.0)
        if not units:
            self.dump_page_diagnostics()
            log.warning("没有解析到任何章节, 已把页面结构导出到 "
                        "调试-页面结构.txt, 把这个文件发出来就能定位选择器")
            return

        log.info("共解析到 %d 个章节(叶子节点)", len(units))
        if KEEP_ALIVE:
            self.install_keep_alive()
        self.install_autoplay()
        if JOB_FINISH_CHECK:
            self.install_job_hook()
        attempted = set()
        results = {}
        seq = 0
        max_rounds = len(units) * 2 + 20

        def handle_one(unit, label: str) -> str:
            text = unit["text"]
            unfinish = unit.get("unfinish", 0)
            if unfinish:
                log.info("%s 开始处理: %s (本章还有 %d 个任务点)",
                         label, text, unfinish)
            else:
                log.info("%s 开始处理: %s", label, text)
            if not self.click_unit(unit):
                log.error("%s 点击章节失败", label)
                return "fail"
            self._wait_chapter_loaded(unit)
            time.sleep(random.uniform(*DWELL_AFTER_OPEN))
            return self.handle_current_task()

        for _ in range(max_rounds):
            if not self._guard_antispider():
                break
            unit = self._pick_next_unit(attempted)
            if unit is None:
                break
            attempted.add(unit["_key"])
            seq += 1
            results[unit["_key"]] = handle_one(unit, "[%d]" % seq)
            time.sleep(random.uniform(*DWELL_BETWEEN_UNITS))

        stuck = [u for u in units if results.get(u["_key"]) == "fail"]
        if stuck:
            log.info("=" * 60)
            log.info("第一轮有 %d 个章节没做完, 再跑一轮试试", len(stuck))
            for unit in stuck:
                if not self._guard_antispider():
                    break
                seq += 1
                results[unit["_key"]] = handle_one(unit, "[重试%d]" % seq)
                time.sleep(random.uniform(*DWELL_BETWEEN_UNITS))

        done = sum(1 for v in results.values() if v == "video_done")
        no_video = sum(1 for v in results.values() if v == "no_video")
        failed = sum(1 for v in results.values() if v == "fail")
        log.info("=" * 60)
        log.info("处理结束: 尝试 %d 个章节 | 新完成视频 %d | 非视频(已跳过) %d "
                 "| 失败 %d | 章节树里仍未完成 %d 个",
                 seq, done, no_video, failed, self._count_unfinished())
        if self.job_missed:
            log.warning('有 %d 个视频播完了, 平台却没把任务点标成完成;'
                        ' 现场在 调试-任务点未完成.txt, 把这个文件发出来就能定位原因',
                        self.job_missed)
        if failed:
            log.info('这次如果弹过风控验证页, 当时的页面结构在 调试-风控.txt')
            log.warning("还有 %d 个章节没啃下来, 多半是风控验证(9010) 或「互动测验/弹题」把播放器顶掉了;"
                        " 同目录的 调试-风控.txt / 调试-播放器消失.txt 里是当时的结构,"
                        " 发出来就能补选择器", failed)
        log.info("章节测验/讨论/文档/学习检测等非视频任务点脚本不处理, 请自行完成")
        log.info("视频里的随堂弹题会按 --quiz 设置处理, 处理不了时看 调试-弹题.txt")


def _launch_args() -> list:
    '''Chromium 启动参数: 允许自动播放, 并让窗口被遮挡/最小化时也不降级'''
    args = [
        '--autoplay-policy=no-user-gesture-required',
        '--mute-audio',
        '--start-maximized',
        '--disable-infobars',
        '--disable-blink-features=AutomationControlled',
    ]
    if BACKGROUND_PLAYBACK:
        args += [
            # Windows 原生遮挡检测: 窗口被别的窗口盖住就当成不可见, 必须关掉
            '--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-background-timer-throttling',
            '--disable-background-mode',
            '--disable-hang-monitor',
        ]
    return args


def pick_course_url(cli_url) -> str:
    '''课程网址: --url > 脚本里的 COURSE_URL > 同目录的 course_url.txt'''
    if cli_url and str(cli_url).strip():
        return str(cli_url).strip()
    if COURSE_URL and str(COURSE_URL).strip():
        return str(COURSE_URL).strip()
    try:
        if COURSE_URL_FILE.exists():
            got = COURSE_URL_FILE.read_text(encoding='utf-8').strip()
            if got:
                log.info('课程网址取自 %s', COURSE_URL_FILE)
                return got
    except Exception as exc:
        log.warning('读 %s 失败: %s', COURSE_URL_FILE, exc)
    log.error('还没给课程网址, 任选一种:')
    log.error('  1) python chaoxing_auto_course.py --url "https://mooc1.chaoxing.com/mycourse/studentstudy?..."')
    log.error('  2) 把网址写进 %s (一行就行, 已被 .gitignore 排除)', COURSE_URL_FILE)
    log.error('  3) 直接改脚本顶部的 COURSE_URL')
    sys.exit(2)

def main() -> None:
    global VIDEO_QUIZ, QUIZ_CHOICE, BACKGROUND_PLAYBACK, AUTOPLAY_POKE
    ap = argparse.ArgumentParser(description='超星学习通自动刷课(视频)工具')
    ap.add_argument('--url', default=None,
                    help='课程章节页网址; 不传就读脚本同目录的 course_url.txt')
    ap.add_argument('--rate', type=float, default=PLAYBACK_RATE,
                    help='视频倍速, 默认 2.0')
    ap.add_argument('--headless', action='store_true',
                    help='无头模式(完成过一次登录后可用)')
    ap.add_argument('--channel', default=None,
                    choices=['chromium', 'chrome', 'msedge'],
                    help='用哪个浏览器: 默认 playwright 自带 chromium; '
                         'chrome/msedge 直接调用系统已装的浏览器(免下载)')
    ap.add_argument('--dump', action='store_true',
                    help='只把页面结构导出到 调试-页面结构.txt 然后退出(排错用)')
    ap.add_argument('--quiz', default=VIDEO_QUIZ,
                    choices=['auto', 'bypass', 'answer', 'off'],
                    help='视频弹题(随堂提问)怎么处理: auto=先绕开, 绕不开就作答; '
                         'bypass=只绕开; answer=直接作答; off=不处理 (默认 auto)')
    ap.add_argument('--quiz-choice', type=int, default=QUIZ_CHOICE,
                    help='代为作答时点第几个选项, 从 1 开始 (默认 1)')
    ap.add_argument('--no-background', action='store_true',
                    help='关掉后台播放优化(关掉后浏览器被遮挡/最小化容易暂停视频)')
    ap.add_argument('--no-poke', action='store_true',
                    help='关掉自动起播补丁(关掉后第一次进视频可能停在播放按钮上)')
    args = ap.parse_args()

    VIDEO_QUIZ = args.quiz
    QUIZ_CHOICE = max(1, int(args.quiz_choice))
    if args.no_background:
        BACKGROUND_PLAYBACK = False
    if args.no_poke:
        AUTOPLAY_POKE = False

    setup_logging()
    url = pick_course_url(args.url)
    log.info('=' * 60)
    log.info('超星学习通自动刷课脚本启动')
    log.info('目标: %s', url)
    if VIDEO_QUIZ == 'off':
        log.info('视频弹题: 不处理(遇到随堂提问会一直卡住, 需要你手动点)')
    elif VIDEO_QUIZ == 'bypass':
        log.info('视频弹题: 只尝试绕开, 不代为作答')
    elif VIDEO_QUIZ == 'answer':
        log.info('视频弹题: 直接选第 %d 个选项作答', QUIZ_CHOICE)
    else:
        log.info('视频弹题: 先尝试绕开, 绕不开就选第 %d 个选项作答', QUIZ_CHOICE)
    if BACKGROUND_PLAYBACK:
        log.info('后台播放: 已关闭窗口遮挡/后台节流, 浏览器被盖住或最小化也能继续播')
    if AUTOPLAY_POKE:
        log.info('自动起播: 已开启(只给当前这一段补 play, 每 %d 毫秒一次;'
                 ' 一页多个播放器不会一起起播)', POKE_INTERVAL_MS)
    log.info('风控保护: 弹出验证页(9010)立刻停手, 等你在浏览器里手动过验证,'
             ' 最多等 %.0f 分钟', ANTISPIDER_WAIT_MINUTES)
    if VIDEO_QUIZ != 'off':
        log.info('互动测验/弹题: 每 %d 秒主动扫一次, 必须答对才算完成'
                 ' (答错自动换下一个选项; 一组多题时逐题答对, 整组过了才点'
                 '"继续学习", 免得平台把视频退回重播; 现场写在 调试-弹题.txt)',
                 QUIZ_SCAN_INTERVAL)
    if JOB_FINISH_CHECK:
        log.info('任务点核对: 每个视频播完后都会盯着 ans-job-finished, 确认平台'
                 '把它标成"已完成"; 播完只看 %.0f 秒, 没标上就直接从头重播一遍,'
                 ' 重播还标不上才记进 调试-任务点未完成.txt', JOB_FINISH_WAIT_FIRST)
    if JOB_FINISH_CHECK and JOB_EARLY_EXIT:
        log.info('提前收尾: 播放中每 %d 秒核对一次, 一旦这个视频的任务点已经标成'
                 ' "已完成"就直接切下一个视频, 不再等它播到结尾', JOB_EARLY_INTERVAL)
    if RETRY_RATE and float(RETRY_RATE) > 0:
        log.info('重播倍速: 第一遍播完但平台没标"任务点完成"时, 重播这一段会用 x%.1f'
                 ' 补播(倍速越高越容易被判异常, 风险自负)', float(RETRY_RATE))
    else:
        log.info('重播倍速: 第一遍播完但平台没标"任务点完成"时, 重播这一段用原倍速 x%.1f'
                 ' (不额外加速)', float(args.rate))
    if MULTI_VIDEO_PER_PAGE:
        log.info('一页多个视频(情景剧/合辑): 每一段都会单独核对任务点, 谁没被标上'
                 '就用 x%.1f 补播, 最多补 %d 遍; 第一段没成也不会把整页丢掉',
                 float(RETRY_RATE) if RETRY_RATE and float(RETRY_RATE) > 0
                 else float(args.rate), MULTI_PAGE_VIDEO_PENDING)
    if RESUME_AFTER_QUIZ:
        log.info('答完题后如果平台把视频拉回开头, 会自动拉回原位接着播(最多 %d 次);'
                 ' 拉回是"粘"的: 平台这时候常把播放器整个重载一遍, 单次赋值会被它'
                 '抹掉, 所以页面里会持续压住进度最多 %d 秒直到真的到位;'
                 ' 弹题卡在末尾时改成直接推到结尾, 不重播整段',
                 QUIZ_SEEK_MAX, QUIZ_SEEK_HOLD)
    if args.rate > 2.0:
        log.warning('倍速 x%.1f 已超过 2 倍: 超星的学习行为检测更容易判定为异常,'
                    ' 后果请自行权衡', args.rate)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
            channel=None if args.channel in (None, 'chromium') else args.channel,
            no_viewport=True,
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            args=_launch_args(),
        )
        if KEEP_ALIVE:
            try:
                context.add_init_script(_KEEP_ALIVE_BOOT)
            except Exception as exc:
                log.warning('注入防切屏脚本失败: %s', exc)
        if AUTOPLAY_POKE:
            try:
                context.add_init_script(_POKE_BOOT)
            except Exception as exc:
                log.warning('注入自动起播脚本失败: %s', exc)
        if JOB_FINISH_CHECK:
            try:
                context.add_init_script(_JOB_HOOK_BOOT)
            except Exception as exc:
                log.warning('注入任务点上报钩子失败: %s', exc)
        page = context.pages[0] if context.pages else context.new_page()
        page.on('dialog', lambda dialog: dialog.accept())
        runner = ChaoxingRunner(page, args.rate)
        try:
            log.info('正在打开课程页……')
            page.goto(url, wait_until='domcontentloaded', timeout=90000)
            if args.dump:
                runner.wait_until_logged_in(args.headless)
                unit = runner._pick_next_unit(set())
                if unit is not None:
                    log.info('先点开第一个未完成章节: %s', unit['text'])
                    runner.click_unit(unit)
                    runner._wait_chapter_loaded(unit)
                    time.sleep(5)
                else:
                    log.info('章节树里没有未完成的章节, 直接导出当前页面')
                runner.dump_page_diagnostics()
                frame = runner.find_content_frame()
                if frame is not None:
                    runner.dump_content_diagnostics(frame)
                else:
                    log.warning('没找到内容区 iframe')
                log.info('页面结构已导出, 直接退出 (未做任何刷课动作)')
            else:
                runner.run(args.headless)
        except KeyboardInterrupt:
            log.warning('收到中断信号, 正在退出……')
        except PlaywrightTimeoutError:
            log.error('页面操作超时, 请检查网络, 或页面结构变化需要更新选择器')
        except SystemExit as e:
            log.error(str(e))
        except Exception:
            log.exception('运行出错')
        finally:
            log.info('浏览器将在 5 秒后关闭……')
            time.sleep(5)
            context.close()


if __name__ == '__main__':
    main()
