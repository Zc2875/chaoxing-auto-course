#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
自动做「学习检测」—— 不依赖任何题库, 靠平台自己的判分反馈把答案试出来。

原理
----
提交之后的结果页, 每道题都带一个判分标记:
    .CorrectOrNot span.marking_dui      -> 回答正确
    .CorrectOrNot span.marking_bandui   -> 回答部分正确(选的都是对的, 但漏了)
    .CorrectOrNot span.marking_cuo      -> 回答错误(勾里混进了错误选项)

而答案页每道题的答案就存在隐藏域 input[id^=answer]<qid> 里, 选题走
li 自己的 onclick="addMultipleChoice(this)"。

于是可以"逐个选项单测", 一轮提交一份卷子(所有题一起推进):

    第 1 轮: 每道题只勾第 1 个选项 -> 看这一题是 dui / bandui / cuo
             dui     说明答案就是它一个, 这题结束
             bandui  说明它是正确项(但还有别的), 记进"正确集合"
             cuo     说明它是错误项
    第 2 轮: 每题勾 {已确认的正确项} + 下一个待测选项, 同样看反馈
    ...
    最后: 每题只勾已确认的正确项提交 -> 应该全对

一次提交最多烧掉 max(选项数) + 1 次机会, 而每份卷子允许 100 次, 够用。

几处"认页面"的讲究(踩过的坑都在这里)
------------------------------------
* 答题页和结果页都有 .singleQuesId, 靠网址区分会认错(重做之后的网址和第一次
  不一定一样), 所以只按页面结构判断: 有隐藏域 input#answer<qid> 才是答题页。
* 提交弹窗只认"确认提交"这种白名单正文; 平台那句"还有未完成的题目, 是否提交?"
  一律点取消 —— 点成确定就等于交一张空卷, 白烧一次作答机会。
* 每次提交前后都对一下结果页上的"第 N 次作答"; 次数没涨说明这次没被平台记账,
  直接停下, 不硬着头皮往下试。
* 点"重做"之后要等它真的回到答题页(最多 30 秒, 允许重试一次); 回不去就认输并
  把当时页面导出成 调试-重做失败.txt。

用法(和 chaoxing_auto_course.py 放同一个目录, 复用已登录的 profile):
    python 刷学习检测.py                  # 只做一节, 边做边打日志
    python 刷学习检测.py --limit 5        # 连做 5 节(0 = 不限)
    python 刷学习检测.py --all            # 每一章都翻一遍(默认只翻有未完成任务的)
    python 刷学习检测.py --chapter 校园    # 只做标题含"校园"的章节
    python 刷学习检测.py --dry-run        # 只把题目结构读出来, 不提交

默认连"做过但没满分"的也重做一遍, 只做没做过的加 --only-new。
'''

import argparse
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAIN = HERE / 'chaoxing_auto_course.py'
if not MAIN.exists():
    sys.exit('没找到 chaoxing_auto_course.py, 请把这个脚本和它放在同一个目录')

spec = importlib.util.spec_from_file_location('cx', MAIN)
cx = importlib.util.module_from_spec(spec)
sys.modules['cx'] = cx
spec.loader.exec_module(cx)

CARD_NAME = '学习检测'
TEXT_JS = '() => (document.body ? document.body.innerText : "")'

# 读答题页: 每题的 qid / 题型码 / 选项字母
READ_QUESTIONS_JS = """
() => {
  const out = [];
  for (const q of document.querySelectorAll(".singleQuesId")) {
    const qid = q.getAttribute("data") || "";
    const t = q.querySelector("input[id^=answertype]");
    const letters = [];
    for (const li of q.querySelectorAll(".Zy_ulTop li")) {
      const sp = li.querySelector("label span") || li.querySelector("span");
      letters.push(sp ? (sp.getAttribute("data") || "") : "");
    }
    out.push({qid: qid, type: t ? t.value : "", letters: letters});
  }
  return out;
}
"""

# 把选题设成指定状态: arg = {qid: ["A","C"]}; 返回每题最终写进隐藏域的值
SET_SELECTION_JS = """
(arg) => {
  const want = arg || {};
  const out = [];
  for (const q of document.querySelectorAll(".singleQuesId")) {
    const qid = q.getAttribute("data") || "";
    const list = want[qid] || [];
    for (const li of q.querySelectorAll(".Zy_ulTop li")) {
      const sp = li.querySelector("label span") || li.querySelector("span");
      if (!sp) { continue; }
      const letter = sp.getAttribute("data") || "";
      const on = String(sp.className || "").indexOf("check_answer") >= 0;
      const should = list.indexOf(letter) >= 0;
      if (on !== should) {
        try { li.click(); } catch (e) {}
      }
    }
    const hid = q.querySelector("input[id^=answer]");
    out.push(qid + "=" + (hid ? hid.value : "?"));
  }
  return out;
}
"""

# 读结果页: 每题的判分标记 / 我的答案 / 得分
READ_MARKS_JS = """
() => {
  const out = [];
  for (const q of document.querySelectorAll(".TiMu")) {
    const sp = q.querySelector(".CorrectOrNot span");
    const c = sp ? String(sp.className || "") : "";
    let mark = "";
    if (c.indexOf("marking_bandui") >= 0) { mark = "bandui"; }
    else if (c.indexOf("marking_dui") >= 0) { mark = "dui"; }
    else if (c.indexOf("marking_cuo") >= 0) { mark = "cuo"; }
    const mine = q.querySelector(".myAnswer .answerCon");
    const sc = q.querySelector(".answerScore");
    out.push({mark: mark,
              mine: mine ? String(mine.textContent || "").trim().slice(0, 40) : "",
              score: sc ? String(sc.textContent || "").trim().slice(0, 16) : ""});
  }
  return out;
}
"""

# 结果页上的"重做"
CLICK_REDO_JS = """
() => {
  for (const el of document.querySelectorAll("a, button, span")) {
    const onclick = el.getAttribute("onclick") || "";
    const t = String(el.textContent || "").trim();
    if (onclick.indexOf("redoTest") >= 0 || t.indexOf("重做") === 0) {
      try { el.click(); } catch (e) {}
      return t.slice(0, 24);
    }
  }
  return "";
}
"""

CLICK_SUBMIT_JS = """
() => {
  for (const el of document.querySelectorAll("a, button, .btnSubmit")) {
    const t = String(el.textContent || el.value || "").trim();
    if (t === "提交" || t === "提 交" || t === "交卷") {
      try { el.click(); } catch (e) {}
      return t;
    }
  }
  return "";
}
"""

# 确认弹窗。只认白名单里的"确认提交/确认重做", 别的一律点取消 —— 尤其是平台那句
# "还有未完成的题目, 是否提交?", 点错一次就等于交了一张空卷, 白烧一次作答机会。
# 页面上有三个弹窗: #workpop(提交/重做确认, 有确定+取消) / #worktoast / #hintPop(纯提示, 只有一个按钮)
DIALOG_JS = """
(arg) => {
  const cfg = arg || {};
  const OK = cfg.ok || [];
  const BAD = cfg.bad || [];
  const pops = [["#workpop", "#popcontent", "#popok", "#popno"],
                ["#worktoast", "#toastcontent", "#toastok", "#toastno"],
                ["#hintPop", "#hintCon", "#hintOk", ""]];
  for (const p of pops) {
    const pop = document.querySelector(p[0]);
    if (!pop) { continue; }
    const st = window.getComputedStyle(pop);
    if (st.display === "none" || st.visibility === "hidden") { continue; }
    const body = pop.querySelector(p[1]);
    const text = String(body ? body.textContent : "").trim().slice(0, 120);
    let ok = false;
    for (const w of OK) { if (text.indexOf(w) >= 0) { ok = true; } }
    for (const w of BAD) { if (text.indexOf(w) >= 0) { ok = false; } }
    const cancelEl = p[3] ? pop.querySelector(p[3]) : null;
    // 只有"确定"一个按钮的纯提示框, 不点掉会一直挡着页面
    const mustOk = !cancelEl;
    const useOk = ok || mustOk;
    const el = useOk ? pop.querySelector(p[2]) : cancelEl;
    const label = el ? String(el.textContent || "").trim() : "";
    if (el) { try { el.click(); } catch (e) {} }
    return {pop: p[0], text: text, action: useOk ? "ok" : "cancel",
            label: label, forced: mustOk && !ok};
  }
  return null;
}
"""

# 结果页上的成绩: 最终成绩 / 满分 / 第几次作答
SCORE_JS = """
() => {
  const t = document.body ? document.body.innerText : "";
  const grab = (key) => {
    const i = t.indexOf(key);
    if (i < 0) { return ""; }
    const rest = t.slice(i + key.length, i + key.length + 16);
    let started = false;
    let num = "";
    for (const ch of rest) {
      const isNum = (ch >= "0" && ch <= "9") || ch === ".";
      if (!started) {
        if (isNum) { started = true; num += ch; }
        continue;
      }
      if (isNum) { num += ch; } else { break; }
    }
    return num;
  };
  // "第3次作答" -> "3" (不用正则, 免得脚本里出现反斜杠)
  const grabTimes = () => {
    const i = t.indexOf("次作答");
    if (i < 0) { return ""; }
    let s = i - 1;
    let num = "";
    while (s >= 0 && t[s] >= "0" && t[s] <= "9") { num = t[s] + num; s -= 1; }
    return num;
  };
  return {full: grab("满分"), final: grab("最终成绩"),
          thisTime: grab("本次成绩"), times: grabTimes()};
}
"""

# 判断这个 frame 现在是答题页还是结果页。
# 只看页面结构, 不看网址 —— 重做之后的网址和第一次进来时不一定一样, 靠网址会认错。
#   答题页: .singleQuesId 里带隐藏域 input#answer<qid>, 而且没有判分标记
#   结果页: 每题一个 .CorrectOrNot 判分标记, 没有隐藏域
PAGE_KIND_JS = """
() => {
  let answers = 0;
  const blocks = document.querySelectorAll(".singleQuesId");
  for (const q of blocks) {
    const qid = q.getAttribute("data") || "";
    let hit = qid ? q.querySelector('input[id="answer' + qid + '"]') : null;
    if (!hit) {
      for (const inp of q.querySelectorAll("input")) {
        const id = inp.getAttribute("id") || "";
        if (id.indexOf("answer") === 0 && id.indexOf("answertype") !== 0) {
          hit = inp;
          break;
        }
      }
    }
    if (hit) { answers += 1; }
  }
  return {answers: answers, blocks: blocks.length,
          graded: document.querySelectorAll(".CorrectOrNot").length,
          options: document.querySelectorAll(".Zy_ulTop li").length};
}
"""

TYPE_NAMES = {'0': '单选', '1': '多选', '2': '填空', '3': '判断', '4': '简答'}


def _eval(frame, js, arg=None):
    try:
        if arg is None:
            return frame.evaluate(js)
        return frame.evaluate(js, arg)
    except Exception as exc:
        cx.log.warning('执行 JS 失败: %s', exc)
        return None


def _eval_quiet(frame, js):
    '''和 _eval 一样, 但失败时不出声(翻页时跨域 frame 会一直报错)'''
    try:
        return frame.evaluate(js)
    except Exception:
        return None


def confirm_any(page, frame, ok_words, bad_words=()) -> dict:
    '''处理确认弹窗; 返回 {text, action, label}; 没弹窗返回 {}

    只有正文里出现白名单 ok_words 才点"确定", 别的一律点"取消"。
    平台那句"还有未完成的题目, 是否提交?"要是被点成"确定", 就等于交了一张
    空卷, 白烧一次作答机会 —— 所以认不出来的一律当"别提交"处理。'''
    arg = {'ok': list(ok_words), 'bad': list(bad_words)}
    for _ in range(8):
        for fr in [frame] + list(page.frames):
            try:
                got = fr.evaluate(DIALOG_JS, arg)
            except Exception:
                got = None
            if isinstance(got, dict) and got.get('action'):
                return got
        time.sleep(1.0)
    return {}


def read_result(frame) -> dict:
    '''结果页上的 成绩/满分/第几次作答; 读不出来返回 {}'''
    got = _eval(frame, SCORE_JS)
    if not isinstance(got, dict):
        return {}
    out = {}
    for key in ('final', 'thisTime', 'full'):
        try:
            out[key] = float(got.get(key))
        except Exception:
            out[key] = None
    try:
        out['times'] = int(got.get('times'))
    except Exception:
        out['times'] = None
    out['score'] = out['final'] if out['final'] is not None else out['thisTime']
    return out


def frame_kind(frame) -> str:
    '''只看页面结构判断: answer(答题页) / graded(结果页) / other / none'''
    got = _eval_quiet(frame, PAGE_KIND_JS)
    if not isinstance(got, dict):
        return 'none'
    if int(got.get('answers') or 0) > 0:
        return 'answer'
    if int(got.get('graded') or 0) > 0:
        return 'graded'
    if int(got.get('blocks') or 0) > 0 or int(got.get('options') or 0) > 0:
        return 'other'
    return 'none'


def find_work_frame(page, seconds: float, want=('answer', 'graded')):
    '''在页面里找一个"学习检测"的 frame; 返回 (frame, kind), 找不到返回 (None, 最后看到的 kind)'''
    deadline = time.monotonic() + seconds
    last = ''
    while True:
        for fr in page.frames:
            kind = frame_kind(fr)
            if kind in want:
                return fr, kind
            if kind != 'none' and not last:
                last = kind
        if time.monotonic() >= deadline:
            return None, last
        time.sleep(1.5)


def wait_for_kind(page, want: str, seconds: float):
    '''等页面变成 want 那种页面, 返回那一层的 frame'''
    fr, _kind = find_work_frame(page, seconds, want=(want,))
    return fr


def dump_frame(frame, name: str) -> None:
    '''出错时把当前这一层的样子存下来, 方便定位'''
    if frame is None:
        return
    try:
        text = frame.evaluate(TEXT_JS) or ''
    except Exception:
        return
    path = HERE / ('调试-%s.txt' % name)
    try:
        path.write_text('网址: %s\n\n%s' % (frame.url or '', text),
                        encoding='utf-8')
        cx.log.info('    当前页面结构已写入: %s', path)
    except Exception as exc:
        cx.log.warning('    写 %s 失败: %s', path, exc)


def read_questions(frame) -> list:
    got = _eval(frame, READ_QUESTIONS_JS)
    return got if isinstance(got, list) else []


def set_selection(frame, plan: dict) -> list:
    got = _eval(frame, SET_SELECTION_JS, plan)
    return got if isinstance(got, list) else []


def read_marks(frame) -> list:
    got = _eval(frame, READ_MARKS_JS)
    return got if isinstance(got, list) else []


# 提交弹窗只有正文里出现这两个词才敢点"确定"
SUBMIT_DIALOG_OK = ('确认提交', '确定提交')
SUBMIT_DIALOG_BAD = ('未完成', '未作答', '没有作答', '未答', '请完成', '不能为空',
                     '不得留空', '漏题', '尚未', '请检查')
# 重做弹窗: "之前答题内容不会保留，确认重做？"
REDO_DIALOG_OK = ('确认重做', '确定重做')


def submit_round(page, frame, plan: dict, label: str, prev_times=None):
    '''设好答案 -> 提交 -> 确认 -> 等结果页

    返回 (每题判分, 结果页frame, 这是第几次作答); 任何一步不对就返回 None,
    绝不在"没选上/被弹窗拦下/平台没记账"的时候继续往下走。'''
    kind = frame_kind(frame)
    if kind != 'answer':
        cx.log.warning('  %s 当前不是答题页(%s), 不提交', label, kind)
        return None
    wrote = set_selection(frame, plan)
    empty = [x for x in wrote if x.split('=')[-1] in ('', '?')]
    if empty:
        time.sleep(1.0)
        wrote = set_selection(frame, plan)
        empty = [x for x in wrote if x.split('=')[-1] in ('', '?')]
    cx.log.info('  %s 勾选: %s', label, ' / '.join(str(x) for x in wrote))
    if empty:
        cx.log.warning('  %s 这些题没选上(%s), 交空卷没意义, 先停下',
                       label, ' / '.join(empty))
        dump_frame(frame, '没选上')
        return None
    time.sleep(0.8)
    hit = _eval(frame, CLICK_SUBMIT_JS)
    if not hit:
        cx.log.warning('  %s 没找到提交按钮', label)
        return None
    time.sleep(1.2)
    dlg = confirm_any(page, frame, SUBMIT_DIALOG_OK, SUBMIT_DIALOG_BAD)
    if dlg.get('action') == 'cancel':
        cx.log.warning('  平台拦下了这次提交(弹窗: %s), 这一段先不做', dlg.get('text'))
        return None
    if dlg.get('text'):
        cx.log.info('  确认弹窗: %s  [%s]', dlg.get('label') or '(没弹)',
                    dlg.get('text'))
    else:
        cx.log.info('  确认弹窗: %s', dlg.get('label') or '(没弹)')
    graded = wait_for_kind(page, 'graded', 60.0)
    if graded is None:
        cx.log.warning('  %s 提交后没等到结果页, 这一段先不做', label)
        dump_frame(frame, '提交后没结果页')
        return None
    time.sleep(1.0)
    marks = read_marks(graded)
    times = read_result(graded).get('times')
    if not marks:
        cx.log.warning('  %s 结果页上一道题都没读到, 先停下', label)
        dump_frame(graded, '结果页没题目')
        return None
    if prev_times is not None and times is not None:
        if times < prev_times:
            # 有的卷子重做之后会重新计数, 这不算异常
            cx.log.info('  作答次数从 %s 变成 %s(重做后重新计数), 继续', prev_times, times)
        elif times == prev_times:
            cx.log.warning('  平台没记这一次作答(还是第 %s 次), 这一段先不做', times)
            return None
    cx.log.info('  判分: %s', ' / '.join(
        '%s=%s(%s)' % (i + 1, m.get('mark') or '?', m.get('score') or '')
        for i, m in enumerate(marks)))
    return marks, graded, times


def redo(page, frame):
    '''点"重做"并确认; 成功返回重做之后的答题页 frame(可能换了对象), 失败返回 None'''
    for attempt in range(2):
        hit = _eval(frame, CLICK_REDO_JS)
        if not hit:
            cx.log.warning('没找到"重做"按钮')
            dump_frame(frame, '没找到重做按钮')
            return None
        cx.log.info('点重做: %s', hit)
        time.sleep(1.5)
        dlg = confirm_any(page, frame, REDO_DIALOG_OK)
        if dlg.get('action') == 'cancel':
            cx.log.warning('  重做被拦下(弹窗: %s)', dlg.get('text'))
            return None
        cx.log.info('  确认重做: %s', dlg.get('label') or '(没弹)')
        new_frame = wait_for_kind(page, 'answer', 30.0)
        if new_frame is not None:
            cx.log.info('  回到答题页: %s', (new_frame.url or '')[:90])
            return new_frame
        cx.log.info('  这次重做没回到答题页, 再来一次')
        fr2, _kind = find_work_frame(page, 3.0)
        if fr2 is None:
            break
        frame = fr2
    cx.log.warning('  重做没成功, 这一段先算了')
    dump_frame(frame, '重做失败')
    return None


def solve_work(page, frame) -> bool:
    qs = read_questions(frame)
    if not qs:
        cx.log.warning('读不到题目结构')
        dump_frame(frame, '读不到题目')
        return False
    for i, q in enumerate(qs, 1):
        cx.log.info('第 %d 题: qid=%s 题型=%s 选项=%s', i, q.get('qid'),
                    TYPE_NAMES.get(str(q.get('type')), q.get('type')),
                    q.get('letters'))
    blank = [str(i) for i, q in enumerate(qs, 1)
             if not [x for x in (q.get('letters') or []) if x]]
    if blank:
        cx.log.warning('第 %s 题不是选择题(没读到可点的选项), 脚本做不了, 跳过这一节',
                       ' / '.join(blank))
        return False
    state = {}
    for q in qs:
        state[q['qid']] = {'correct': [], 'wrong': [], 'done': False}
    rounds = max(len(q['letters']) for q in qs) + 1
    times = None
    for r in range(rounds):
        plan = {}
        probing = False
        for q in qs:
            qid = q['qid']
            st = state[qid]
            if st['done']:
                plan[qid] = list(st['correct'])
            elif r < len(q['letters']):
                plan[qid] = list(st['correct']) + [q['letters'][r]]
                probing = True
            else:
                plan[qid] = list(st['correct'])
                probing = True
        if not probing:
            break
        got = submit_round(page, frame, plan, '第 %d 轮' % (r + 1), times)
        if got is None:
            return False
        marks, graded, times = got
        frame = graded
        for q, m in zip(qs, marks):
            qid = q['qid']
            st = state[qid]
            mark = (m or {}).get('mark') or ''
            if st['done']:
                continue
            probe = q['letters'][r] if r < len(q['letters']) else ''
            if mark == 'dui':
                st['correct'] = list(plan[qid])
                st['done'] = True
            elif probe and mark == 'bandui':
                if probe not in st['correct']:
                    st['correct'].append(probe)
            elif probe and mark == 'cuo':
                if probe not in st['wrong']:
                    st['wrong'].append(probe)
            else:
                cx.log.warning('  第 %d 题的判分是 %s, 当作未知',
                               qs.index(q) + 1, mark or '空')
        if all(st['done'] for st in state.values()):
            cx.log.info('全部答对: %s', ' / '.join(str(m.get('score')) for m in marks))
            return True
        if r < rounds - 1:
            new_frame = redo(page, frame)
            if new_frame is None:
                cx.log.warning('重做失败, 这一段先算了')
                return False
            frame = new_frame
    cx.log.warning('试完所有轮次还没全对; 已确认的正确项: %s',
                   {k: v['correct'] for k, v in state.items()})
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=None)
    ap.add_argument('--chapter', default=None, help='只做标题里含这几个字的章节')
    ap.add_argument('--limit', type=int, default=1, help='最多做几节(默认 1, 0 = 不限)')
    ap.add_argument('--only-new', action='store_true',
                    help='只做从没做过的; 默认连"做过但没满分"的也重做一遍')
    ap.add_argument('--all', action='store_true',
                    help='把每一章都翻一遍(默认只翻"还有未完成任务点"的章节)')
    ap.add_argument('--max-chapters', type=int, default=0,
                    help='最多翻多少个章节(默认 0 = 不限)')
    ap.add_argument('--dry-run', action='store_true', help='只读题目结构, 不提交')
    ap.add_argument('--channel', default='msedge',
                    help='msedge(默认, 和你平时用的 run.bat 一致) / chrome / chromium')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--keep-open', type=float, default=5.0)
    args = ap.parse_args()

    cx.setup_logging()
    url = cx.pick_course_url(args.url)
    done = 0
    skipped = 0
    failed = 0
    with cx.sync_playwright() as p:
        context = cx._launch_context(p, args.channel, args.headless)
        if context is None:
            return
        page = context.pages[0] if context.pages else context.new_page()
        runner = cx.ChaoxingRunner(page, cx.PLAYBACK_RATE)
        try:
            cx.log.info('正在打开课程页……')
            page.goto(url, wait_until='domcontentloaded', timeout=90000)
            runner.wait_until_logged_in(args.headless)
            units = runner.collect_units_wait(90.0)
            if not units:
                cx.log.error('没解析到章节')
                return
            if args.chapter:
                wanted = [u for u in units if args.chapter in (u.get('text') or '')]
            elif args.all:
                wanted = list(units)
                wanted.sort(key=lambda u: -int(u.get('unfinish') or 0))
            else:
                wanted = [u for u in units if int(u.get('unfinish') or 0) > 0]
                wanted.sort(key=lambda u: -int(u.get('unfinish') or 0))
            cx.log.info('共 %d 章节, 待查 %d 个', len(units), len(wanted))
            tried = 0
            for unit in wanted:
                if (args.limit and done >= args.limit) or \
                        (args.max_chapters and tried >= args.max_chapters):
                    break
                tried += 1
                text = unit.get('text') or ''
                if not runner.click_unit(unit):
                    continue
                runner._wait_chapter_loaded(unit)
                time.sleep(2.5)
                card = None
                for c in runner.list_cards():
                    if CARD_NAME in (c.get('title') or ''):
                        card = c
                        break
                if card is None:
                    continue
                cx.log.info('[%d] 章节「%s」-> 学习检测', tried, text)
                runner.switch_card(card['index'])
                time.sleep(2.5)
                frame, kind = find_work_frame(page, 25.0)
                if frame is None:
                    cx.log.info('    没等到答题页/结果页, 换下一节')
                    continue
                if kind == 'graded':
                    res = read_result(frame)
                    score, full = res.get('score'), res.get('full')
                    if score is not None and full is not None and score >= full:
                        cx.log.info('    已经满分(%s/%s), 不用做', score, full)
                        skipped += 1
                        continue
                    if args.only_new:
                        cx.log.info('    已经做过了(--only-new), 跳过')
                        continue
                    cx.log.info('    做过但没满分(成绩 %s / 满分 %s), 点重做再来',
                                score, full)
                    frame = redo(page, frame)
                    if frame is None:
                        cx.log.warning('    重做失败, 换下一节')
                        continue
                if args.dry_run:
                    for i, q in enumerate(read_questions(frame), 1):
                        cx.log.info('    第 %d 题: 题型=%s 选项=%s', i,
                                    TYPE_NAMES.get(str(q.get('type')), q.get('type')),
                                    q.get('letters'))
                    continue
                if solve_work(page, frame):
                    done += 1
                    cx.log.info('这一节完成 (%d/%d)', done, args.limit)
                else:
                    failed += 1
                    cx.log.warning('这一节没搞定, 换下一节')
            cx.log.info('=' * 56)
            cx.log.info('结束: 完成 %d 节 | 本来就满分跳过 %d 节 | 没搞定 %d 节',
                        done, skipped, failed)
            if args.keep_open > 0:
                time.sleep(args.keep_open)
        except KeyboardInterrupt:
            cx.log.warning('手动中断')
        except SystemExit as exc:
            cx.log.error('%s', exc)
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == '__main__':
    main()
