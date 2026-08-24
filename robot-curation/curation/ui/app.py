"""Gradio Blocks 四 tab UI(薄壳层,2026-07-27 U1)。

分工:数据整形全在 manifest.py(纯函数,全测试);本文件只摆组件+接回调,
不做业务逻辑。gradio 是可选依赖——import 收在函数内,没装 gradio 时
`curation run`/`backends` 等一切照常。

四 tab:质检总览 / Episodes(三桶 + 左清单右详情,详情以视频为主=demo 高光页)/
技能画像 / 后端状态(复用 backends 探活)。

启动方式(2026-07-29 U4 改):不再 `blocks.launch()`,而是
`gr.mount_gradio_app(FastAPI(), blocks, "/")` + uvicorn 自跑——因为要在同一个端口上
挂自定义路由(`/ws/term` 内嵌终端 + `/term-static/` 前端资产 + `/healthz`)。
外部行为(端口/主题/页签/CSS/allowed_paths)与 launch() 时代逐项一致。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys

from ..delivery import (delivery_root_of, resolve_run, run_choices,
                        run_name_of_run_id)
from .manifest import (clear_discover_cache,  # noqa: F401
                       APPEAL_CHOICES, APPEAL_HEADERS, QUEUE_HEADERS,
                       BUCKET_ALL, DECISION_CHOICES,
                       DETAIL_LABELS, MERGE_FILTER_ALL, SUCCESS_MODES,
                       VERDICT_CHOICES,
                       appeal_hint_md, appeal_reason_text,
                       appeal_rows, application_counts_md, audit_clip_paths,
                       audit_note_md,
                       bucket_choices, bucket_ids, carryover_note_md,
                       decision_trace_md,
                       load_label_decisions, load_reject_appeals,
                       load_task_verdicts, merge_filter_mode,
                       merged_filter_choices, merged_hint_md,
                       merged_queue_view, play_all_button_html,
                       success_block_mode,
                       unapplied_banner_md, unapplied_card_note,
                       record_label_decision, record_reject_appeal,
                       record_task_verdict_checked,
                       VERDICT_HOLD as _HOLD,
                       AUDIT_TERM, LATENCY_HEADERS,
                       LATENCY_KIND_NOTE, LATENCY_NOTE, LATENCY_PCTL_NOTE,
                       SKILL_HEADERS, SYNC_FILTER_ALL, SYNC_FILTERS,
                       TL_FILTERS, TL_SORTS,
                       merged_queue_rows, merged_review_queue, check_table_html, delivery_choices,
                       detail_table_choices,
                       discover_deliveries, episode_card_html,
                       episode_list_view, episode_video_html,
                       latency_bar_html, latency_rows,
                       load_delivery, load_detail_table, load_perf,
                       load_timeline, manual_hint_html, resolve_delivery,
                       OVERVIEW_HEADERS, overview_markdown, overview_note_md,
                       overview_rows, perf_backend_md,
                       perf_env_md, readings_text, skill_bar_html, skill_rows,
                       sync_camera_html, sync_conclusion_html, sync_health_html,
                       sync_view, SYNC_HOWTO,
                       task_question_md, task_reference_html, task_reference_md,
                       timeline_html,
                       video_detail_view)
from . import runner            # 任务执行层(任务台跑批 + 报告页「执行裁决」共用)
# 公共数据集目录(2026-08-21):只读一份清单、登记匿名桶,不 import 管道
from ..ingest import public_catalog
from .. import tos_store   # 只用纯函数:parse_tos_url / classify_list_error

log = logging.getLogger("curation.ui")

#: 数据集根目录的出厂默认。面板只列这个根下的数据集,**不接受任意路径输入**——
#: 一个自由路径输入框等于把整个 pod 文件系统开给任何拿到 UI 密码的人(终端页签
#: 本来就是 shell 所以无所谓,面向客户的面板性质完全不同)。
DEFAULT_DATA_ROOT = "/mnt/tos/datasets"

#: vendored 的 xterm.js 资产 + 我们的 term.js,由 `/term-static/` 静态目录服务。
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: 标签页图标(Arco 蓝 + 白对勾),见 presentation() 里的说明。
FAVICON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "assets", "favicon.png")

def _terminal_head(root: str) -> str:
    """终端页签的前端装配:资产从**本服务**取(pod 内无 CDN 通路),只在开了终端时注入。

    root = UI 挂载前缀(如 "/curation";挂根路径传 "")。资产路径必须带上前缀:
    APIG 按路径分流时**不剥前缀**,写死 `/term-static/*` 会被网关分给别的后端。
    `window.CURATION_ROOT` 让 term.js 用同一个前缀拼 `/ws/term`。
    """
    return f"""
<script>window.CURATION_ROOT = {json.dumps(root)};</script>
<link rel="stylesheet" href="{root}/term-static/xterm.css" />
<script src="{root}/term-static/xterm.js"></script>
<script src="{root}/term-static/addon-fit.js"></script>
<script src="{root}/term-static/term.js"></script>
"""

#: 同步证据图三挡:界面说法 → 配置值(pipeline.sync_plots)。
PLOT_MODES = {"flagged": "只给判废/存疑的画(默认)", "all": "每条都画", "off": "不画"}

#: 三个并发旋钮 → 配置路径。留空 = 不发 --set,用生效配置里的值。
CONC_KEYS = {
    "ep": "pipeline.vlm_episode_concurrency",
    "fr": "checks.task_success.vlm.max_concurrency",
    "cap": "skill_profile.caption_concurrency",
}

#: 哪几步要调 VLM —— 并发旋钮只对它们有意义,范围里没有它们就整组置灰。
VLM_CHECKS = ("task_success", "skill_profile")

#: 「质检范围」三挡的界面说法(改名只改这里,判断都比这三个常量)。
FULL_SCAN = "完整质检"
#: 「数据集目录」标签旁的二选一(2026-08-21 同事看图后定稿):「私有」(默认)= 填自己的桶;
#: 选镜像 = 目录框与地区填上公共镜像桶并置灰只看不填,只剩「数据集」下拉可选;交付目录一概
#: 不动。镜像那项的名字来自 public_datasets.label(缺省「字节 HuggingFace 镜像」);没配置不出现。
SRC_PRIVATE = "私有"
QUICK_SCAN = "快速质检"
CUSTOM_SCAN = "自选模块"   # 曾叫「自定义模块」——听着像"自己定义模块里查什么"
                          # (那是以后的事),其实是"从现成模块里挑几个跑"

#: 「交付名」下面那行说明。选一个与选多个,这个名字的含义**不一样**,不说清楚
#: 客户会以为三个数据集的结果会互相覆盖(2026-08-13 用户提多选时点名要说明白)。
#: 多选时的落盘形状与 CLI `--batch` 一致(`<交付名>/<数据集名>/`),报告页的递归
#: 发现本来就找得到。
#: 单选时交付名下面不再写说明(2026-08-21 用户:删掉,界面要简洁);多选时那句还在
OUT_NAME_HINT_ONE = ""
# 2026-08-18 精简:原句四小句挤在窄列里要折三四行。只留"这个名字当什么用"
# 这一件事(它回答的就是交付名的含义),执行顺序与容错("按序跑、一个没跑成
# 后面照跑")挪出说明行 —— 那是任务台的进度区本来就看得见的事实。
OUT_NAME_HINT_MANY = "多选:这个名字当**父文件夹**,每个数据集各出一份子交付"

#: 「快速质检」旁边那个问号里的话。**按实际跑什么写**:--lite 只是跳过要 VLM 的
#: 三步(任务成败判定 / 打标 / 技能画像),其余检查一步不少。
QUICK_SCAN_TIP = ("跑不需要模型的那几项:视觉质量、运动质量、运动学极限、"
                  "视频-动作同步、时间戳与精确去重。"
                  "不做任务成败判定、打标与技能分布画像 —— 这三项要调 VLM。")

#: 「完整质检」旁边那个问号(2026-08-20 用户要:和快速质检一样能悬停看包含什么)。
#: 同样按实际流水线写,顺序就是漏斗顺序。
FULL_SCAN_TIP = ("全部六项检查:时间戳、运动学极限、运动质量、视觉质量、"
                 "视频-动作同步,以及要调模型的任务成败判定;"
                 "随后精确去重、打标与技能分布画像,导出清洗后的数据集。"
                 "耗时大头在模型调用。")

#: 问号表:选项文字 → 悬停提示。加一项只改这里。
SCAN_TIPS = {FULL_SCAN: FULL_SCAN_TIP, QUICK_SCAN: QUICK_SCAN_TIP}

#: 任务面板的自动刷新(issue #57,2026-08-21):页面脚本按节奏点「刷新」按钮。
#: 节奏:任务在跑(状态条里有「运行中」/「正在停止」)2 秒一次;没在跑 10 秒一次
#: (别的标签页/命令行起了任务也能在 10 秒内出现);切回页面立刻补点一次。
TASK_POLL_ACTIVE_MS = 2000
TASK_POLL_IDLE_MS = 10000
_TASK_POLL_JS = """<script>(function(){
  var BUSY = ['运行中', '正在停止'];
  var ACTIVE_MS = __ACTIVE__, IDLE_MS = __IDLE__, last = 0;
  function busy(){
    var s = document.getElementById('tk-status');
    var t = s ? (s.innerText || '') : '';
    return BUSY.some(function(k){ return t.indexOf(k) >= 0; });
  }
  function refresh(){
    var b = document.getElementById('tk-refresh');
    if (b && !b.disabled) { last = Date.now(); b.click(); }
  }
  // 不按 visibilityState 拦:浏览器本来就把后台标签页的定时器压到每秒/每分钟一次,
  // 再拦一道只会让"切回来那一瞬"多等;切回来那一下由 visibilitychange 补一次
  setInterval(function(){
    var gap = busy() ? ACTIVE_MS : IDLE_MS;
    if (Date.now() - last >= gap) refresh();
  }, 500);
  document.addEventListener('visibilitychange', function(){
    if (document.visibilityState === 'visible') refresh();
  });
})();</script>""".replace("__ACTIVE__", str(TASK_POLL_ACTIVE_MS)).replace("__IDLE__", str(TASK_POLL_IDLE_MS))


def readonly_block_msg(m) -> str:
    """镜像自只读桶的交付:裁决记了也写不回去 → 三个记录入口一律拒,返回这句话;
    不是只读返回空串。宁可锁:人以为记上了、实际只活在本地缓存,是"静默丢人工判断"
    级别的事故(2026-08-21 用户定)。看报告/看片段不受影响。"""
    ro = (m or {}).get("tos_readonly") if isinstance(m, dict) else ""
    if not ro:
        return ""
    return f"⚠️ 未记录:这份交付所在的桶只读,裁决写不回去 —— {ro}"


def readonly_banner_md(m) -> str:
    """「待你裁决」顶部的锁提示(与 readonly_block_msg 同一个判据)。"""
    ro = (m or {}).get("tos_readonly") if isinstance(m, dict) else ""
    if not ro:
        return ""
    return f"🔒 **这份交付所在的桶只读,裁决无法回写,本页只能看不能记。** {ro}\n\n"

#: 把问号塞进「快速质检」那一项的文字后面。Gradio 的 Radio 选项只吃纯文本
#: (给 HTML 会被转义),所以只能在前端补;组件重绘后要能自愈,故用
#: MutationObserver 兜着,而不是只在 load 时跑一次。
_TIP_JS = """
<script>
(function () {
  function hide() {
    var box = document.getElementById('qc-tipbox');
    if (box) box.style.display = 'none';
  }
  window.addEventListener('scroll', hide, true);
  document.addEventListener('click', hide, true);
  document.addEventListener('visibilitychange', hide);

  var TIPS = __TIPS__;   // 选项文字 → 提示(Python 端 SCAN_TIPS 注入)
  function attach(span, tip) {
      var q = document.createElement('span');
      q.className = 'qc-tip';
      q.textContent = '?';
      // 浮层做成**单例**并在多个信号上收起(mouseleave / 滚动 / 点击 / 页面
      // 隐藏)。曾经一次一个 div 地建,遇到组件重绘或没配对的 mouseleave 就留下
      // 一个悬在屏幕上撤不掉的框(2026-08-13 用户实见)。
      q.addEventListener('mouseenter', function () {
        var box = document.getElementById('qc-tipbox');
        if (!box) {
          box = document.createElement('div');
          box.id = 'qc-tipbox';
          box.className = 'qc-tipbox';
          document.body.appendChild(box);
        }
        box.textContent = tip;
        box.style.display = 'block';
        var r = q.getBoundingClientRect(), b = box.getBoundingClientRect();
        var left = Math.min(Math.max(8, r.left + r.width / 2 - b.width / 2),
                            window.innerWidth - b.width - 8);
        var top = r.top - b.height - 8;              // 上方放不下就翻到下方
        box.style.left = left + 'px';
        box.style.top = (top < 8 ? r.bottom + 8 : top) + 'px';
      });
      q.addEventListener('mouseleave', hide);
      span.appendChild(q);
  }
  function inject() {
    var box = document.getElementById('qc-scope');
    if (!box) return;
    var labels = box.querySelectorAll('label');
    for (var i = 0; i < labels.length; i++) {
      var span = labels[i].querySelector('span');
      if (!span || span.querySelector('.qc-tip')) continue;
      var tip = TIPS[span.textContent.trim()];
      if (tip) attach(span, tip);
    }
  }
  new MutationObserver(inject).observe(document.documentElement,
                                       { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', inject);
  inject();
})();
</script>
"""


#: 下拉浮层跟着页面滚(2026-08-13 用户实见,有照片):点开「数据集」下拉、不选、
#: 直接滚鼠标,选项表**留在原地**,和输入框错开老远,像两个不相干的东西浮在页上。
#:
#: 根因:Gradio 6.9 的选项表是 `ul.options`,`position: fixed` + 顶格 z-index,
#: **坐标只在打开那一刻算一次**;而输入框在同一个 wrap 里、随页面滚动。fixed 的
#: 浮层不动 ⇒ 脱节。它自己没有跟随逻辑,只能我们补一层。
#:
#: 四个坑各有代价,别简化:
#: ① **锚点必须是 `div.wrap`,不是里面的 input**(2026-08-13 真机量出来的):
#:    Gradio 自己就是按 wrap 算的 —— 实测 `ul.left == wrap.left`、
#:    `ul.style.top == wrap.bottom`,而 input 比 wrap 窄 60px、右偏 16px(它有
#:    内边距)。按 input 重排,第一次滚动浮层就会横向跳一下、宽度缩一截 ——
#:    修好了纵向却新添一个横向的毛病。
#:    同理 **只改 top,不碰 left/width**:滚动只会让纵向脱节,横向本来就没错。
#:    实测「要执行的交付」那个长列表比 wrap 宽 19px(它自己的样式),写一次
#:    width 就会把它缩一下 —— 顺手改的每一个属性都是一次白挨的跳动。
#: ② **必须保住原来的展开方向**——下方空间不够时浮层是开在输入框**上方**的,
#:    一律按"下方"重排会把它甩到输入框头上把输入框盖住;
#: ③ **列表自己滚动不重算**——长列表里滚动也会冒出 scroll 事件,跟着重算等于
#:    自己跟自己打架(轻则无谓开销,重则抖动);
#: ④ **锚点滚出视野就收起**——挂在半空、指向一个看不见的输入框的浮层,比直接
#:    关掉更让人摸不着头脑。
#: 走全局监听 + 每次现查 DOM(不在打开时一次性绑),理由与 _TIP_JS 用
#: MutationObserver 自愈同一条:Gradio 随时重建节点。捕获阶段是必须的 —— 滚动
#: 事件的 target 是 document,内层容器滚动更是不冒泡,只有捕获阶段收得到。
#: 全站生效:认的是 `ul.options` 这个形状,不认哪一个下拉 —— 任务台的数据集/
#: 模型服务/交付、报告页顶部的交付、明细页的选择明细表,都是同一段代码管着。
_DROPDOWN_JS = """
<script>
(function () {
  function place(ul) {
    var wrap = ul.closest('.wrap') || ul.parentElement;   // gradio 自己的锚点
    if (!wrap) return;
    var u = ul.getBoundingClientRect();
    if (!u.height) return;               // 已收起/没渲染:没有位置可言
    var w = wrap.getBoundingClientRect();
    if (w.bottom < 0 || w.top > window.innerHeight) {
      var input = wrap.querySelector('input');
      if (input) input.blur();           // 锚点已滚出视野:收起,别挂在半空
      return;
    }
    // 开在上方时 ul.top 必然小于 wrap.top;开在下方时 ul.top == wrap.bottom
    var above = u.top < w.top;
    ul.style.top = (above ? w.top - u.height : w.bottom) + 'px';
  }
  function reflow(e) {
    var t = e && e.target;
    if (t && t.closest && t.closest('ul.options')) return;   // 列表自己滚:不重算
    var uls = document.querySelectorAll('ul.options');
    for (var i = 0; i < uls.length; i++) place(uls[i]);
  }
  window.addEventListener('scroll', reflow, true);
  window.addEventListener('resize', reflow, true);

  // ── 点箭头展不开(issue #53,2026-08-19 实机量出来的)────────────────
  // 单选下拉:.icon-wrap 是 pointer-events:none 且**压在 input 上面**
  //   (实测箭头中心 505 < input 右边界 519)⇒ 点击穿透到 input → 聚焦 → 展开。
  // 多选下拉:.icon-wrap 是 pointer-events:**auto** 且**整个落在 input 右边之外**
  //   (箭头中心 1157 > input 右边界 1144)⇒ 点击命中 SVG 里的 <path>,被吃掉,
  //   什么也不发生。两者 svelte 哈希不同(1dv2vbb / 1xfsv4t),本就是两个组件。
  // ⚠️ 只加 CSS pointer-events:none **不够**:实测穿透后命中的是 .secondary-wrap
  //    而不是 input(input 根本没延伸到箭头底下),照样不展开。
  // ✅ input.focus() 就是展开的开关(实测 focus 之后 8 个选项立刻出来)。
  // 对单选下拉是**空操作**:它们的事件 target 就是 input,closest('.icon-wrap')
  // 为 null 直接返回 —— 本来正常的那些一个字不受影响。
  // 不做"再点收起":展开态没有可靠判据(ul.options 是 position:fixed,
  // offsetParent 恒为 null,这个坑今天刚踩过),收起交给点别处/Esc。
  document.addEventListener('click', function (e) {
    var t = e.target;
    var icon = (t && t.closest) ? t.closest('.icon-wrap') : null;
    if (!icon) return;
    var wrap = icon.closest('.wrap');
    var inp = wrap && wrap.querySelector('input');
    if (inp && !inp.disabled) inp.focus();
  }, true);
})();
</script>
"""


#: 表格列宽可拖(2026-08-13 用户要)。Gradio 6.9 的 Dataframe **没有**这个能力
#: (前端只有整表右下角那个高度手柄),所以自己加一层:在表头右边缘 6px 内按下
#: 即进入拖动,拖动期间只改那一列 <th> 的 width/min/max —— 不碰数据、不发请求、
#: 不改任何布局属性(高度那一刀已经把表压塌过一次,见 _ARCO_CSS 里的告诫)。
#: 走**捕获阶段 + stopPropagation**:否则这一按会被 Gradio 认成"点表头=排序"。
#: 重绘(换交付/换明细表/翻页)后宽度回到自动值 —— 有意不持久化:存宽度就得跟着
#: 它的虚拟滚动与列集合对齐,收益远不抵复杂度。
_TABLE_JS = """
<script>
(function () {
  var MIN = 60, st = null, seq = 0;

  // ⚠️ Gradio 把一张表渲染成**两张 <table>**(粘性表头一张、表体一张,表体还带
  // 虚拟滚动)。只改被拖的那个 <th> 的宽度 ⇒ 表头动了表体没动,整表错位
  // (2026-08-13 用户实见)。所以宽度不写在元素上,而是**写成一条 CSS 规则**,
  // 作用域挂在这两张表共同的 .block 上,按列序号命中 —— 表头表体一起走,
  // 虚拟滚动新渲染出来的行也自动带上。
  function ruleEl(scope, idx) {
    if (!scope.getAttribute('data-colres')) {
      scope.setAttribute('data-colres', 'c' + (++seq));
    }
    scope._cr = scope._cr || {};
    if (!scope._cr[idx]) {
      var e = document.createElement('style');
      document.head.appendChild(e);
      scope._cr[idx] = e;
    }
    return scope._cr[idx];
  }
  function apply(scope, idx, w) {
    var el = ruleEl(scope, idx);        // 必须先建:属性是它打上去的,
    var sel = '[data-colres="' + scope.getAttribute('data-colres') +
              '"] tr > *:nth-child(' + (idx + 1) + ')';
    el.textContent =
      sel + '{width:' + w + 'px !important;min-width:' + w +
      'px !important;max-width:' + w + 'px !important}';
  }
  function edge(e) {
    var th = e.target.closest && e.target.closest('th');
    if (!th) return null;
    var r = th.getBoundingClientRect();
    var d = r.right - e.clientX;
    return (d <= 6 && d >= -2) ? th : null;
  }
  document.addEventListener('mousemove', function (e) {
    if (st) {
      apply(st.scope, st.idx, Math.max(MIN, st.w + e.clientX - st.x));
      e.preventDefault();
      return;
    }
    var th = e.target.closest && e.target.closest('th');
    if (th) th.style.cursor = edge(e) ? 'col-resize' : '';
  }, true);
  document.addEventListener('mousedown', function (e) {
    var th = edge(e);
    if (!th) return;
    var scope = th.closest('.block') || th.closest('table');
    var idx = Array.prototype.indexOf.call(th.parentElement.children, th);
    st = { scope: scope, idx: idx, x: e.clientX,
           w: th.getBoundingClientRect().width };
    document.body.style.userSelect = 'none';
    e.preventDefault();
    e.stopPropagation();     // 否则这一按会被 Gradio 当成"点表头 = 排序"
  }, true);
  document.addEventListener('mouseup', function () {
    if (st) { st = null; document.body.style.userSelect = ''; }
  }, true);
})();
</script>
"""

_TERMINAL_CSS = """
/* 终端按 Arco 的**暗色面板**来(2026-08-13 用户:终端页还没跟上 Arco):
   底色走 Arco 暗色中性 #17171A、描边 #2E2E30、圆角与卡片同档 8px。
   终端本身不做成浅色 —— 深底浅字是终端的通用心智,改浅只会更难读;
   要统一的是圆角/描边/中性色阶这些"外框语言"。 */
#curation-term-screen {
  height: 78vh; width: 100%;
  background: #17171A; border: 1px solid #2E2E30; border-radius: 8px;
  padding: 10px 8px;
  /* 2026-08-13 用户截图:面板底部多出一条深色带,比白卡还宽、带直角,压在圆角
     外面。成因 = xterm 行区高度是「行数 × 行高」,和 78vh 的容器高度对不齐时会
     多出几像素,而容器原来是 overflow: visible ⇒ 多出的那几像素画在圆角之外。
     **靠裁剪解决,不要改成动态算高** —— 算高是 fit addon 的活,再写一份必然
     跟它打架。⚠️ 只加在最外层:.xterm-viewport 是终端自己的滚动区,给它加
     overflow 等于把终端滚动砍掉。 */
  overflow: hidden;
}
/* 2026-07-30 用户反馈:终端右侧有一条刺眼的白条——那是 xterm 滚动区
   (.xterm-viewport)的**浏览器默认滚动条**,白色轨道贴在深色终端上。
   改成与终端同色系:轨道融入背景,滑块深灰、悬停略亮。Firefox 走
   scrollbar-color,WebKit 系走 ::-webkit-scrollbar 三件套。 */
#curation-term-screen .xterm-viewport {
  scrollbar-color: #4E5969 #17171A;
  scrollbar-width: thin;
}
#curation-term-screen .xterm-viewport::-webkit-scrollbar { width: 10px; background: #17171A; }
#curation-term-screen .xterm-viewport::-webkit-scrollbar-track { background: #17171A; }
#curation-term-screen .xterm-viewport::-webkit-scrollbar-thumb {
  background: #4E5969; border-radius: 5px; border: 2px solid #17171A;
}
#curation-term-screen .xterm-viewport::-webkit-scrollbar-thumb:hover { background: #86909C; }
/* "字太淡"的真凶(2026-07-30 JS 实测):gradio 的 `.prose *` 把终端里**每一层**
   后代(行 div、字符 span)全染成 var(--body-text-color)(rgb(39,39,42) 深灰),
   压过 xterm 主题的继承——无论前景设什么,默认文字都是深灰。
   修法必须覆盖**整棵子树**(第一版只改 span 不够:span 的 inherit 会从紧邻的
   父级行 div 继承,而行 div 还是灰的——继承链断在中间)。带 xterm-fg- 类或
   内联色的 span 保持 ANSI 原色,不碰。 */
#curation-term-screen .xterm-rows *:not([class*="xterm-fg-"]) { color: inherit; }
"""



# ── Arco Design 视觉标准(2026-08-13 用户定:整个 UI 按字节自家的 Arco 走)──────
# Arco 本体是 React,装不进 Gradio ⇒ 这里用 CSS 逼近它的设计语言。**改配色只改这一处**。
# token **逐个核对过官方发行包**(2026-08-13):从 @arco-design/web-react 的
# dist/css/arco.min.css 里解出真值比对——arcoblue-6 #165DFF、green-6 #00B42A、
# orange-6 #FF7D00、red-6 #F53F3F、深字档 -7(#009A29/#D25F00/#CB272D)、
# 浅底档 -1、边框档 -2(#BEDAFF/#AFF0B5/#FFE4BA/#FDCDC5)、中性 gray-1..10
# (#F7F8FA/#F2F3F5/#E5E6EB/#C9CDD4/#86909C/#4E5969/#1D2129)、圆角 medium 4px。
# ⚠️ 曾把 orange-2/red-2 错拿成 -3 档(边框重一档),核对时才发现——
# 改配色前先去发行包取真值,别凭印象。
# 关键取向:**小圆角(2/4px)、无渐变、克制阴影、14px 正文、按钮 32px 高**——
# 此前那套橙色巨型按钮与 8-12px 圆角是逐次需求堆出来的,不是设计体系。
_ARCO_CSS = """
:root, .gradio-container {
  --arco-primary: #165DFF; --arco-primary-hover: #4080FF;
  --arco-success: #00B42A; --arco-warning: #FF7D00; --arco-danger: #F53F3F;
  --arco-t1: #1D2129; --arco-t2: #4E5969; --arco-t3: #86909C; --arco-t4: #C9CDD4;
  --arco-border: #E5E6EB; --arco-fill: #F2F3F5; --arco-fill-1: #F7F8FA;
  --arco-radius: 4px;
}
/* 字号:Arco 规范正文是 14px,**这里刻意走 15px**(2026-08-13 用户两次点名
   "字太小看着费劲")。屏幕远、看一天报表的场景,可读性压过规范的字面一致;
   整套只加这一档,层级关系不动。表格与说明文字一并跟上,否则正文变大反衬得
   数据更小。 */
/* 层次感(2026-08-13 用户:"白板上画个灰框,文字漂在里面,没有立体感")。
   做法照 Arco Pro 的控制台版式:**页面底灰、内容白卡、1px 描边 + 一层浅阴影**,
   靠底色差把卡片"抬"起来,而不是靠重阴影或渐变。全部改 gradio 自己的主题变量,
   一个布局属性都不碰(动 height/overflow 那类曾把表体压塌,见下方告诫)。 */
:root, .gradio-container {
  --body-background-fill: #F2F3F5 !important;
  --background-fill-primary: #FFFFFF !important;
  --background-fill-secondary: #F7F8FA !important;
  --panel-background-fill: #FFFFFF !important;
  --block-background-fill: #FFFFFF !important;
  --block-border-color: #E5E6EB !important;
  --block-border-width: 1px !important;
  --block-radius: 8px !important;
  --block-shadow: none !important;
  --block-label-background-fill: transparent !important;
  --input-background-fill: #FFFFFF !important;
  --input-border-color: #E5E6EB !important;
  --input-radius: 6px !important;
  --radius-sm: 4px !important; --radius-md: 6px !important;
  --radius-lg: 8px !important; --radius-xl: 12px !important;
  --button-large-radius: 6px !important; --button-small-radius: 6px !important;
}
/* 抬起来的是**整页内容**,不是每个小块 —— 逐块加阴影会碎成一地发光的白条
   (试过,更难看)。所以:页签面板 = 一张大白卡,卡内一切保持平的。 */
.gradio-container .block, .gradio-container .form { box-shadow: none !important; }
.gradio-container .tabitem {
  background: #FFFFFF !important; border: 1px solid var(--arco-border) !important;
  border-radius: 10px !important; padding: 18px 20px !important;
  box-shadow: 0 1px 3px rgba(29,33,41,.06), 0 8px 20px rgba(29,33,41,.05) !important;
}
/* 内层页签(跑质检页的任务与日志、明细的四个子页)在大卡里面,
   再套一层卡就成了"盒中盒"。 */
.gradio-container .tabitem .tabitem {
  background: transparent !important; border: none !important;
  box-shadow: none !important; padding: 6px 0 !important;
}
/* 输入类按 Arco 的填充式:浅灰底、无描边,聚焦才变白 + 主色边。
   白卡上再放白输入框 = 全靠一根灰线区分,正是"没有立体感"的来源。 */
.gradio-container input[type="text"], .gradio-container input[type="number"],
.gradio-container textarea, .gradio-container .wrap-inner,
.gradio-container .secondary-wrap {
  background: var(--arco-fill) !important; border-color: transparent !important;
  border-radius: 6px !important;
}
.gradio-container input[type="text"]:focus, .gradio-container textarea:focus {
  background: #FFFFFF !important; border-color: var(--arco-primary) !important;
}
.gradio-container {
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
               "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif !important;
  font-size: 15px !important; color: var(--arco-t2) !important;
}
.gradio-container h1 { font-size: 22px !important; color: var(--arco-t1) !important;
                       font-weight: 600 !important; }
.gradio-container h2 { font-size: 18px !important; color: var(--arco-t1) !important;
                       font-weight: 600 !important; }
.gradio-container h3 { font-size: 16px !important; color: var(--arco-t1) !important;
                       font-weight: 600 !important; }
.gradio-container p, .gradio-container li, .gradio-container label,
.gradio-container table { font-size: 15px !important; }
.gradio-container table th, .gradio-container table td {
  font-size: 14px !important;      /* 表体略小于正文:一行十几列,再大就全靠横拖 */
}
/* 按钮:逐条对齐官方 .arco-btn-size-default —— 高 32px、padding 0 15px、
   font-size 14px、font-weight 400。**圆角走 6px 不是 Arco 规范的 2px**:
   用户 2026-08-13 点名"圆角不明显,再做点圆角"——同上,可读观感压过字面规范。
   primary 的边框是 transparent(不是同色描边);secondary 背景 = color-secondary
   (中性 2 档 #F2F3F5)、文字 color-text-2。 */
/* ⚠️ 高度/内边距**只给真正的操作按钮**,绝不写成 `button {height:...}` ——
   Gradio 的表格内部也拿 <button> 当布局件,给所有按钮强行定高会把表体压塌
   (2026-08-13 实测:.table-wrap 从 281px 塌到 37px,199 行只渲染出 1 行)。
   圆角/字号/阴影这类不影响布局高度的,才可以全局给。 */
.gradio-container button {
  border-radius: 6px !important; font-size: 14px !important;
  font-weight: 400 !important; box-shadow: none !important;
  transition: all .1s cubic-bezier(0,0,1,1) !important;
}
.gradio-container button.primary, .gradio-container button.secondary,
.gradio-container button.stop {
  min-height: 32px !important; height: 32px !important;
  padding: 0 15px !important; line-height: 1.5715 !important;
  font-size: 15px !important;
}
/* 页签跟正文同档:它是导航,比数据更该看得清。role=tab 选得准,不会误伤
   表格内部那些当布局件用的 <button>。 */
.gradio-container button[role="tab"] { font-size: 15px !important; }
.gradio-container button.primary, .gradio-container button.lg.primary {
  background: var(--arco-primary) !important; border: 1px solid transparent !important;
  color: #fff !important;
}
.gradio-container button.primary:hover { background: var(--arco-primary-hover) !important; }
.gradio-container button.secondary {
  background: var(--arco-fill) !important; border: 1px solid transparent !important;
  color: var(--arco-t2) !important;
}
.gradio-container button.secondary:hover { background: #E5E6EB !important; }
/* 危险按钮走 outline 形态(白底红描边红字):Arco 里"停止"这类破坏性动作的常见写法 */
.gradio-container button.stop {
  background: #fff !important; border: 1px solid var(--arco-danger) !important;
  color: var(--arco-danger) !important;
}
.gradio-container button.stop:hover { background: #FFECE8 !important; }
/* 输入/选择/卡片:统一 4px 圆角与 Arco 边框色 */
.gradio-container input, .gradio-container textarea, .gradio-container select,
.gradio-container .block, .gradio-container .form {
  border-radius: var(--arco-radius) !important;
}
.gradio-container .block { border-color: var(--arco-border) !important; }
/* Gradio 自带主题色是橘色(加载动画、排序箭头、选中态、滑块都吃它)。
   只改我们自己的类盖不住,必须把它的 CSS 变量本身换掉 —— 否则页面上永远
   飘着几处橘色(2026-08-13 用户在明细表右上角的橘色箭头上点名)。 */
.gradio-container, :root {
  --color-accent: #165DFF !important; --color-accent-soft: #E8F3FF !important;
  --primary-50: #E8F3FF !important; --primary-100: #E8F3FF !important;
  --primary-200: #BEDAFF !important; --primary-300: #94BFFF !important;
  --primary-400: #4080FF !important; --primary-500: #165DFF !important;
  --primary-600: #165DFF !important; --primary-700: #0E42D2 !important;
  --button-primary-background-fill: #165DFF !important;
  --button-primary-background-fill-hover: #4080FF !important;
  --button-primary-border-color: #165DFF !important;
  --slider-color: #165DFF !important; --loader-color: #165DFF !important;
  --checkbox-background-color-selected: #165DFF !important;
  --checkbox-border-color-selected: #165DFF !important;
  /* ⚠️ 别给 --radio-circle 塞颜色:它是**选中态那个白点的背景图**(一段
     fill=white 的 svg data-uri),塞成 #165DFF 等于把白点擦掉 —— 蓝底蓝点,
     看着就是一个实心蓝方块,只在切换的一瞬间闪出白点(2026-08-13 用户实见)。
     选中色由上面两个变量给足了,这一条不该动。 */
}
/* 单选圆、复选方(Arco 如此,也是通用心智);Gradio 默认把两者都做成 4px 圆角方块。 */
.gradio-container input[type="radio"] { border-radius: 50% !important; }
/* 单选/多选组按 Arco RadioGroup/CheckboxGroup(issue #54、#59 第 1 条,2026-08-20):
   gradio 默认把**每个选项**画成一个带边框的药丸,四个圈各自成框、外面却没有
   "组"的框(#ep-buckets 还是 hide-container)。Arco 的做法相反:选项本身不带框,
   选中态靠实心圆点表达;整组装进一个框。选项去框 + .wrap 补组框,两条规则全站
   生效(质检范围 / Episodes 三桶筛选 / episode 列表 / 自选模块多选 同一套)。 */
.gradio-container .wrap:has(> label > input[type="radio"]),
.gradio-container .wrap:has(> label > input[type="checkbox"]) {
  border: 1px solid var(--arco-border) !important; border-radius: 4px !important;
  background: #FFFFFF !important; padding: 6px 12px !important;
  gap: 4px 20px !important;
}
.gradio-container .wrap > label:has(> input[type="radio"]),
.gradio-container .wrap > label:has(> input[type="checkbox"]) {
  border: none !important; background: transparent !important;
  box-shadow: none !important; padding: 2px 0 !important;
}
/* episode 列表(#ep-list)外面本来就是带标题的框,里面不再套一层组框 */
#ep-list .wrap:has(> label > input[type="radio"]) {
  border: none !important; padding: 0 !important; background: transparent !important;
}
.gradio-container .wrap > label:has(> input[type="radio"]):hover,
.gradio-container .wrap > label:has(> input[type="checkbox"]):hover {
  background: transparent !important;
}
/* 带解释的小问号:灰底圆点,悬停出深色浮层(Arco 的 tooltip 是 gray-10 底白字)。
   文案挂在 data-tip 上,纯 CSS 显示,不引任何组件库。 */
.qc-tip {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; margin-left: 6px; border-radius: 50%;
  background: var(--arco-t4); color: #fff; font-size: 11px; font-weight: 600;
  cursor: help; position: relative; vertical-align: middle;
}
.qc-tip:hover { background: var(--arco-t3); }
/* 并发框置灰要**看得出来**(Gradio 只是 disabled,视觉上毫无变化)。
   作用域钉死在 .conc-num:日志窗也是 disabled 的,一刀切会把日志文字也刷成浅灰。 */
.conc-num textarea:disabled, .conc-num input:disabled {
  background: var(--arco-fill-1) !important; color: var(--arco-t4) !important;
  cursor: not-allowed !important;
}
/* ⚠️ 浮层**不能**用 ::after 挂在问号上:Gradio 的表单块是 overflow:hidden,
   气泡会被齐刷刷裁掉一半(2026-08-13 实测)。所以由 JS 在 body 上另起一个
   position:fixed 的框 —— 定位脱离一切祖先,谁也裁不到。 */
.qc-tipbox {
  position: fixed; max-width: 340px; background: #FFFFFF; color: var(--arco-t1);
  border: 1px solid var(--arco-border); font-size: 13px; line-height: 1.7;
  text-align: left; padding: 10px 12px; border-radius: 6px; z-index: 9999;
  pointer-events: none; box-shadow: 0 4px 10px rgba(29,33,41,.10);
}
/* 真模态对话框(2026-08-19 用户点名:确认框必须"跳出来",不能是平铺框)。
   Gradio 没有原生模态框,但模态不需要它原生支持 —— fixed 居中 + 一层遮罩即可,
   Python 那边只是给 gr.Column 挂个 class,组件树与事件接线一个字不动。
   ⚠️ 遮罩用 box-shadow 大扩散,**不许用 position:fixed 的 `::before`**
   (2026-08-19 真机打脸):对话框自己带 transform:translate(-50%,-50%),而 CSS
   规定 fixed 元素一旦有 transform 祖先就改为相对该祖先定位 —— 于是 inset:0 的
   "全屏遮罩"实际只盖住对话框自己(框内发灰、页面四周纹丝不动)。box-shadow 的
   第二段 0 0 0 200vmax 从框边向外铺满整个视口,没有额外元素、不进任何
   overflow:hidden 的裁剪(Gradio 表单块裁兄弟元素那坑也躲开了)。
   代价:box-shadow 不吃点击,页面其余部分仍可点 —— 拿"改写交付"这一下换整屏
   变暗的强提示,可以接受;真正的断路器是「确定」本身(不点绝不发起)。 */
.gradio-container .modal-dialog {
  position: fixed !important;
  top: 50% !important; left: 50% !important;
  transform: translate(-50%, -50%) !important;
  z-index: 10000 !important;
  width: min(560px, calc(100vw - 48px)) !important;
  background: #FFFFFF !important;
  border: 1px solid var(--arco-border) !important;
  border-radius: 8px !important;
  padding: 20px 24px !important;
  box-shadow: 0 8px 30px rgba(29,33,41,.28),
              0 0 0 200vmax rgba(29,33,41,.45) !important;
}
/* 对话框按钮:居中、定宽(2026-08-19 用户点名)。不居中的根因:Row 默认
   flex-start,而 Button 的 min-width 让 scale=0 也铺成大宽条。 */
#ex-ask-btns, #out-ask-btns, #in-ask-btns { justify-content: center !important; gap: 12px !important; }
#ex-ask-btns button, #out-ask-btns button, #in-ask-btns button {
  flex: 0 0 auto !important; width: 120px !important; min-width: 0 !important;
}
/* 待裁决队列单表(2026-08-23 用户拍板:两表合一)。滚动容器钉死高度,
   条数多少不影响外观,内部滚动(表头吸顶)。 */
#audit-queue .table-wrap, #audit-queue .svelte-virtual-table-viewport {
  height: 420px !important; max-height: 420px !important;
}

/* ①标注问题 ‖ ②成败问题 并排(2026-08-19 用户拍板)。同样要等高:两块的表单
   项数不同,不撑齐的话右边那块的按钮行会吊在半空。 */
#adj-blocks { gap: 16px !important; align-items: stretch !important; }
#adj-blocks > div { display: flex !important; flex-direction: column !important; }

/* 「其它原因-整条弃用」:独立一行、弱化。它是这张卡上唯一会**扔掉数据**的动作,
   不能和上面六个裁决按钮长一个样(点错的代价不对等)。 */
#adj-kill { margin-top: 14px !important; justify-content: flex-start !important; }
#adj-kill button {
  background: transparent !important;
  border: 1px solid var(--arco-border) !important;
  color: var(--arco-t3) !important;
}
#adj-kill button:hover {
  border-color: var(--arco-danger) !important; color: var(--arco-danger) !important;
}

/* 折叠块的标题条(「更多设置」「并发」):Gradio 默认就是一行光秃秃的字,
   用户 2026-08-13 反馈"猛一看根本反应不过来能点开"。做成一条**看得出可点**的
   横条:浅灰底 + 描边 + 悬停加深,左侧一个三角(展开时转 90°),标题左对齐,
   Gradio 自带的那个箭头留在最右并染成主色。只改观感,不碰展开逻辑。 */
.gradio-container button.label-wrap {
  display: flex !important; align-items: center !important;
  justify-content: flex-start !important; gap: 8px !important;
  text-align: left !important;
  background: var(--arco-fill-1) !important;
  border: 1px solid var(--arco-border) !important;
  border-radius: 6px !important; padding: 9px 12px !important;
  color: var(--arco-t1) !important; font-weight: 500 !important;
  transition: background .12s, border-color .12s !important;
}
.gradio-container button.label-wrap:hover {
  background: var(--arco-fill) !important; border-color: var(--arco-t4) !important;
}
.gradio-container button.label-wrap > span:first-child { flex: 0 0 auto !important; }
.gradio-container button.label-wrap > span.icon {
  margin-left: auto !important; color: var(--arco-primary) !important;
  font-size: 12px !important; opacity: 1 !important;
}
.gradio-container button.label-wrap::before {
  content: ""; flex: 0 0 auto; width: 0; height: 0;
  border-left: 5px solid var(--arco-t3);
  border-top: 4px solid transparent; border-bottom: 4px solid transparent;
  transition: transform .15s ease;
}
.gradio-container button.label-wrap.open::before { transform: rotate(90deg); }
/* ⚠️ 别再给表格容器强制 overflow —— Gradio 的 .table-wrap/.table-container 自带
   滚动与虚拟渲染,外部再压一层 overflow:auto 会让它算不出高度,**表体直接塌成
   一行**(2026-08-13 实测:table-wrap 高 37px 而表本身 100px,只渲染出 1 行)。
   列太宽要控,请用 Dataframe 自己的 column_widths / max_height / wrap 参数。 */
/* 表格:行高与描边按 Arco */
.gradio-container table thead th {
  background: var(--arco-fill-1) !important; color: var(--arco-t2) !important;
  font-weight: 500 !important; border-bottom: 1px solid var(--arco-border) !important;
}
.gradio-container table td { border-bottom: 1px solid var(--arco-fill) !important; }
/* 日志窗:等宽字体。CLI 输出本来就是按列对齐的,用比例字体看就是一团乱
   (2026-08-13 用户点名"预设/状态/服务端类型没对齐") */
.mono-log textarea, .mono-log input {
  font-family: "SFMono-Regular", ui-monospace, Menlo, Consolas, monospace !important;
  font-size: 12px !important; line-height: 1.6 !important; color: var(--arco-t1) !important;
}
/* 控件下方的说明行(2026-08-17):说明不再走 Dropdown 的 info= —— 它被渲染在
   标签和控件**之间**,谁有说明谁的控件就被往下推,三列对不齐(用户实机点名)。
   改为控件下一行的独立 Markdown。这里只管长相:比正文小一号的灰字(对齐原来
   info 的观感),负 margin 把它从主题默认行距里拉回控件底下,否则看着像一段
   无关文字。为空时组件本身零内容零高度,负 margin 也不会把别的内容顶歪。 */
.gradio-container .field-note,
.gradio-container .field-note p,
.gradio-container .field-note span {
  /* ⚠️ 必须连 p/span 一起写:只写外层时实测外层拿到了 #86909C/13px,而真正
     显示文字的 <p> 仍是 rgb(39,39,42)/15px —— gradio 的 prose 规则直接命中
     p,颜色继承打不过一条更具体的规则(用户实见:"怎么能用黑色字平铺描述")。
     字号/颜色照 Arco:辅助说明用 text-3(#86909C),行高 1.5715 是 Arco 的
     标准值(见 .arco-tabs-header-title)。 */
  color: var(--arco-t3) !important; font-size: 12px !important;
  line-height: 1.5715 !important;
}
.gradio-container .field-note p { margin: 0 !important; }
/* 左内边距 13px = 实测的错位量:说明行在控件面板之外,默认贴到面板左沿,比
   字段标签还靠左 13px,看着像跟上面那组控件没关系。
   ⚠️ 不许用负 margin 往上拉:上面那块控件面板是不透明白底,-6px 会把这行的
   字顶进面板底下**被切掉半截**(用户实见:"端点那一行被盖住了")。要贴近就
   靠正的小间距,别靠负值。 */
.gradio-container .field-note { margin-top: 2px !important; padding-left: 13px; }

/* 「数据集目录」自画标题行(2026-08-21):Markdown 标签 + 紧挨着的「私有 / 镜像」二选一,
   观感对齐 gradio 原生 block 标签(同字号/同灰/同下间距),右边列的原生标签才不会
   跟它错行。单选 container=False 只剩选项,组框/内边距全去掉,行高压到与标签一致。 */
/* 这一列自己当卡片:gradio 只把**连续的表单控件**合进一张 .form 卡,中间插了
   Markdown 标签就散成三块各带边框(实机:勾选框掉到下一行自成一框)。做法 = 列外框
   画成 .form 的样子,里面的 .form/.block 全部去框去内边距。 */
#rn-tin-col { border: 1px solid var(--arco-border) !important;
              border-radius: var(--arco-radius) !important;
              background: var(--block-background-fill, #fff) !important;
              padding: var(--block-padding, 10px 12px) !important; gap: 0 !important; }
#rn-tin-col .form, #rn-tin-col .block {
  border: none !important; background: transparent !important;
  padding: 0 !important; box-shadow: none !important; min-height: 0 !important;
}
#rn-tin-head { align-items: center !important; justify-content: flex-start !important;
               gap: 14px !important; flex-wrap: nowrap !important;
               margin-bottom: var(--spacing-sm, 4px) !important; min-height: 0 !important; }
#rn-tin-head .field-label, #rn-tin-head .field-label p {
  font-size: var(--block-title-text-size, 14px) !important;
  color: var(--block-title-text-color, var(--arco-t2)) !important;
  font-weight: var(--block-title-text-weight, 400) !important;
  line-height: 1.4 !important; margin: 0 !important; padding: 0 !important;
}
/* gradio 给每个 .block 写死 width:100%,flex 行里两个"自适应宽"的块会各占满一行
   (实机:勾选框被顶出卡片外)—— 标题行里的块按内容定宽 */
#rn-tin-head > .block, #rn-tin-head .field-label, #rn-pub {
  width: auto !important; flex: 0 0 auto !important; min-width: 0 !important;
  /* gradio 的 .auto-margin 给块 margin:auto,flex 行里它会把剩余宽度全吃成左外边距
     (实测勾选框被推远 200px,计算样式却报 0px) */
  margin: 0 !important;
}
#rn-pub .wrap { border: none !important; padding: 0 !important; background: transparent !important;
                gap: 0 16px !important; min-height: 0 !important; }
#rn-pub label { padding: 0 !important; gap: 6px !important; }
#rn-pub label span { font-size: 13px !important; color: var(--arco-t2) !important; }
/* 单选下拉的箭头是绝对定位压在 input 右端上的(见 _DROPDOWN_JS 的注释):input 文字
   要给它让出位置,窄列时末尾按省略号截,不许被箭头盖住(2026-08-21 用户实见) */
.gradio-container .wrap .secondary-wrap input {
  padding-right: 28px !important; text-overflow: ellipsis !important;
}
/* 置灰的输入框(interactive=False → disabled):内容用 Arco 的 text-3 灰,一眼看出
   "这是给你看的,不是让你填的"(2026-08-21 用户:选了镜像后目录/地区要灰) */
.gradio-container textarea:disabled, .gradio-container input:disabled {
  color: var(--arco-t3) !important; -webkit-text-fill-color: var(--arco-t3) !important;
  opacity: 1 !important;
}

/* 内层子页签按 Arco 的 line 型(实测自 arco.css):
   .arco-tabs-header-title = 14px / text-2 / **无边框无背景**;
   active = 主色 + font-weight 500,选中态靠底部那条 ink 下划线表达。
   gradio 默认给的是 6px 圆角方框 + 选中时蓝色边框(用户实见:"太难看了"）。
   2026-08-18 用户放宽了"报告页页签不许动"那条红线(仅限外观统一),于是规则
   放开成全局。**顶层导航为何不受影响**:#topnav 那组用的是 ID 选择器
   (#topnav > .tab-container button),特异性高于本规则,且它设的属性与本规则
   完全重合 —— 逐条都被它盖回去,不会漏出半套样式。 */
.gradio-container button[role="tab"] {
  background: transparent !important; border: none !important;
  border-bottom: 2px solid transparent !important; border-radius: 0 !important;
  box-shadow: none !important; color: var(--arco-t2) !important;
  font-weight: 400 !important;
  padding: 6px 0 !important; margin-right: 24px !important;
}
/* ⚠️ 这里**不设 font-size**:上面那条 `button[role="tab"] { font-size: 15px }`
   是有意的("页签跟正文同档"),而本 UI 正文就是 15px。Arco 规范写 14px 是
   因为它正文也是 14px —— 该照搬的是"页签=正文同档"这个关系,不是绝对数字。 */
.gradio-container button[role="tab"]:hover { color: var(--arco-primary) !important; }
.gradio-container button[role="tab"][aria-selected="true"],
.gradio-container button[role="tab"].selected {
  color: var(--arco-primary) !important; font-weight: 500 !important;
  border-bottom-color: var(--arco-primary) !important;
}
"""


# 顶层导航按钮样式(2026-07-29 用户定:大、明显、立体)。只作用于 elem_id=topnav
# 的外层两页签,内层六个报告 tab 不受影响。立体感=渐变+外阴影(凸起),选中态=
# 橙色渐变+内阴影(按下)。选中类名在 gradio 版本间摇摆,selected/aria-selected 双保。
_TOPNAV_CSS = """
/* 顶层导航(跑质检 / 质检报告 / 终端)。2026-08-13 改按 Arco:去掉橙色渐变与
   立体阴影,改成"选中即主色下划线 + 主色文字"的克制样式。 */
#topnav > .tab-wrapper button::after, #topnav > .tab-container button::after {
  display: none !important;
}
#topnav > .tab-wrapper, #topnav > .tab-container {
  border-bottom: 1px solid #E5E6EB !important;
}
#topnav > .tab-container button, #topnav > .tab-wrapper button {
  font-size: 15px !important; font-weight: 500 !important;
  padding: 10px 20px !important; margin: 4px 6px 0 0 !important;
  border: none !important; border-bottom: 2px solid transparent !important;
  border-radius: 0 !important; background: transparent !important;
  box-shadow: none !important; color: #4E5969 !important; min-height: 40px !important;
}
#topnav > .tab-container button:hover, #topnav > .tab-wrapper button:hover {
  color: #165DFF !important;
}
#topnav > .tab-container button.selected, #topnav > .tab-wrapper button.selected,
#topnav button[aria-selected="true"] {
  background: transparent !important; color: #165DFF !important;
  border-bottom: 2px solid #165DFF !important; box-shadow: none !important;
}
"""


def _config_yaml(m: dict) -> str:
    if m.get("load_error"):
        return "(读不到这份交付,见「质检总览」页)"
    ce = m.get("config_effective")
    if not ce:
        return "(此交付无 config_effective 快照——U0 之前的老交付)"
    try:
        import yaml
        return yaml.safe_dump(ce, allow_unicode=True, sort_keys=False)
    except Exception:  # noqa: BLE001
        return json.dumps(ce, ensure_ascii=False, indent=1)


def _probe_backends(config_path: str | None, timeout: float) -> list[list]:
    """后端状态 tab 的数据源:复用 backends 子命令同款探活(config+list_models)。"""
    from ..adapters.vlm_client import list_models
    from ..pipeline.config import load_config

    rows = []
    try:
        cfg = load_config(config_path)
    except Exception as e:  # noqa: BLE001
        return [["(配置加载失败)", str(e), ""]]
    from ..adapters.vlm_client import probe_failure_reason
    for name, b in sorted((cfg.get("vlm_backends") or {}).items()):
        try:
            models = list_models(b["endpoint"], b.get("api_key_env"), timeout_s=timeout)
            shown = ", ".join(models[:3]) + (f" …(共{len(models)}个)" if len(models) > 3 else "")
            rows.append([name, "✅在线", shown])
        except Exception as e:  # noqa: BLE001
            # 原因分三类说(密钥/HTTP/网络),别把 401 说成"不可达"(2026-08-21)
            reason = probe_failure_reason(e, b.get("api_key_env"))
            state = "❌密钥问题" if "密钥" in reason or "鉴权" in reason else "❌不可达"
            rows.append([name, state, reason])
    return rows


#: 探活结果缓存(秒):自动探活挂在开页/切页签上,一分钟内反复切页不必每次都出网
PROBE_CACHE_S = 60
_PROBE_CACHE: dict = {}


def _probe_backends_cached(config_path, timeout: float, *, now=None) -> list[list]:
    import time as _t
    t = now if now is not None else _t.time()
    hit = _PROBE_CACHE.get(config_path)
    if hit and t - hit[0] < PROBE_CACHE_S:
        return hit[1]
    rows = _probe_backends(config_path, timeout)
    _PROBE_CACHE[config_path] = (t, rows)
    return rows



# 分歧队列的可点性提示(2026-08-05 用户反馈:怕用户不知道行能点):
# 悬停变手型 + 行高亮,配合首列「裁决 ▶」操作列,双保险。
_AUDIT_CSS = """
/* 同步曲线卡片(2026-08-07 三轮反馈后定稿):一行一张(两列会把图压瘦)、
   卡片之间大间距、内部留白充足;点图在新标签页开原图(gradio 的全屏按钮
   在本环境不生效,用户实测)。 */
.sync-cards { display:flex; flex-direction:column; gap:22px; margin:14px 0 8px; }
/* 卡片整块居中(2026-08-07 用户最终定:靠左"还是不好看");内部曲线在左、诊断框在右 */
.sync-card { background:#fff; border:1px solid #E5E6EB; border-radius:14px;
             padding:6px 18px 14px; box-shadow:0 1px 3px rgba(15,23,42,.05);
             transition:box-shadow .18s ease, border-color .18s ease;
             max-width:1240px; width:100%; margin:0 auto; }
.sync-card-body { display:flex; gap:18px; align-items:flex-start; }
.sync-figure { flex:1 1 auto; min-width:0; display:block; }
/* 诊断框:定宽不参与压缩,窄屏时整块掉到图下面(flex-wrap 由 body 的换行控制) */
.sync-diag { flex:0 0 316px; align-self:stretch; border-left:1px solid #F7F8FA;
             padding:2px 0 0 16px; font:12px/1.65 system-ui; color:#4E5969; }
.sync-diag-title { font-weight:800; color:#1D2129; font-size:.86rem;
                   margin:2px 0 8px; }
.sync-diag-row { padding:8px 0; border-bottom:1px dashed #F7F8FA; }
.sync-diag-row:last-of-type { border-bottom:none; }
.sync-diag-head { display:flex; align-items:center; gap:7px; }
.sync-dot { width:8px; height:8px; border-radius:50%; flex:0 0 8px; }
.sync-diag-lag { margin-left:auto; font:600 .78rem ui-monospace,Menlo,monospace;
                 color:#4E5969; }
.sync-diag-label { font-weight:700; font-size:.78rem; margin:3px 0 2px; }
.sync-diag-text { color:#4E5969; }
.sync-diag-advice { color:#86909C; margin-top:4px; font-style:italic; }
.sync-diag-foot { margin-top:10px; padding-top:9px; border-top:1px solid #F7F8FA;
                  color:#86909C; font-size:.78rem; }
@media (max-width:1100px) {
    .sync-card-body { flex-direction:column; }
    .sync-diag { flex:1 1 auto; border-left:none; padding:10px 0 0;
                 border-top:1px solid #F7F8FA; }
}
.sync-card:hover { box-shadow:0 8px 26px rgba(15,23,42,.09); border-color:#E5E6EB; }
.sync-card-head { display:flex; align-items:center; gap:12px; padding:12px 0 12px 12px;
                  margin-bottom:6px; border-bottom:1px solid #F7F8FA; }
.sync-eid { font:700 .95rem ui-monospace,Menlo,monospace; color:#1D2129; }
.sync-badge { font:600 .82rem system-ui; }
.sync-open { margin-left:auto; font:.8rem system-ui; color:#86909C !important;
             text-decoration:none; border:1px solid #E5E6EB; border-radius:7px;
             padding:3px 11px; }
.sync-open:hover { color:#1D2129 !important; border-color:#C9CDD4; background:#F7F8FA; }
.sync-img { width:100%; display:block; border-radius:8px; cursor:zoom-in; }
/* 页内灯箱(纯 CSS):checkbox 选中 → 全屏遮罩显大图;点遮罩(同一 label)即关闭。
   ⚠️ 祖先若带 transform 会把 position:fixed 变成相对它定位——.sync-card 的
   hover 效果因此只动阴影不动 transform,别加回来。 */
.sync-lb-toggle { display:none; }
.sync-lb { display:none; position:fixed; inset:0; z-index:1000;
           background:rgba(15,23,42,.86); padding:2.5vh 2.5vw;
           align-items:center; justify-content:center; cursor:zoom-out; }
.sync-lb img { max-width:96vw; max-height:95vh; width:auto; height:auto;
               border-radius:10px; background:#fff; box-shadow:0 24px 80px rgba(0,0,0,.45); }
.sync-lb-toggle:checked + .sync-lb { display:flex; }

#audit-queue tbody tr { cursor: pointer; }
#audit-queue tbody tr:hover td { background: rgba(255, 140, 0, 0.10) !important; }

/* Episodes 页(2026-08-11 改版):顶部三桶横排大按钮,左侧清单一列可滚。
   清单是**扫读**用的:等宽字体对齐 episode 号,单行不换行(几百条一换行整列就散),
   容器内滚动而不是把页面拉到几屏高。 */
#ep-buckets .wrap { display: flex; flex-direction: row; gap: 10px; flex-wrap: wrap; }
#ep-buckets label { font-size: 1.02rem !important; font-weight: 700; padding: 7px 16px; }
#ep-list { max-height: 68vh; overflow-y: auto; }
#ep-list .wrap { display: flex; flex-direction: column; gap: 1px; }
#ep-list label { font: 12px/1.5 ui-monospace, Menlo, monospace; padding: 4px 8px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#ep-list label:hover { background: rgba(255, 140, 0, 0.10); }
"""


def _conc_placeholder(default: int | None) -> str:
    """并发框的占位符:把**生效配置里的**默认值说给用户听。

    只做占位符,**绝不预填 value** —— 预填等于在界面里多存一份默认值,以后改了
    default.yaml 而界面还按老值发 `--set`,是静默的不一致(_sets 里"只发用户动过
    的"是同一条纪律的另一半)。读不到默认值就退回原来那句,不编一个数字出来。
    """
    return f"默认 {default},留空即用它" if default else "留空 = 用配置里的值"


#: 探活结果缀在下拉选项后面的两种后缀。Gradio 的 Dropdown 不支持单个选项置灰
#: (Arco 的 disabled 态在这里做不出来),所以改用**文字标注 + 开跑前拦截**:
#: 标注让人一眼看见,拦截保证选了不可用的也不会白等一分钟才在日志里看到连接失败。
BACKEND_OK, BACKEND_BAD = " · 可用", " · 暂不可用"


def _backend_label_of(choice) -> str:
    """下拉选中项 → 标签本体(容忍带「· 可用」/「· 暂不可用(原因)」后缀)。"""
    s = str(choice or "")
    for suffix in (BACKEND_OK, BACKEND_BAD):
        i = s.find(suffix)
        if i >= 0:
            return s[:i]
    return s


def _status_ok(v) -> bool:
    """探活状态值:True/False,或 (是否在线, 原因)。"""
    return bool(v[0]) if isinstance(v, tuple) else bool(v)


def _status_reason(v) -> str:
    return str(v[1] or "") if isinstance(v, tuple) and len(v) > 1 else ""


def _backend_options(labels: dict, status: dict | None = None) -> list:
    """{标签: 预设代号} + {代号: 在线?|(在线?, 原因)} → 下拉选项。未检测的只给标签;
    不可用的把原因缀在括号里 —— 「暂不可用」三个字让人去查网络,而多数时候是密钥没注入。"""
    out = []
    for label, code in labels.items():
        if status is None or code not in status:
            out.append(label)
        elif _status_ok(status[code]):
            out.append(label + BACKEND_OK)
        else:
            r = _status_reason(status[code])
            out.append(label + BACKEND_BAD + (f"({r})" if r else ""))
    return out


def _reprobe_options(old_labels: dict, labels: dict, status: dict,
                     selected: list) -> tuple:
    """探活回调的纯逻辑:重读过配置的标签表 + 探活结果 →(下拉选项, 各下拉保住的
    选中值, 提示语)。

    为什么探活要顺便重读配置(2026-08-13):下拉是 build_app 那一刻算出来的,客户
    在站点 YAML 里新加一台自托管服务,得重启 UI 才看得见 —— 而重启 UI 会杀掉正在
    跑的批(那些任务是 UI 进程的子进程)。选中的预设若已从配置里消失,回到未选状态
    并在提示里说清,绝不留一个指向不存在服务的选项。
    提示里**只报数目不报预设代号**(代号不进界面,与标签同一条红线)。
    """
    choices = _backend_options(labels, status)
    values, lost = [], False
    for cur in selected:
        want = _backend_label_of(cur)
        hit = next((c for c in choices if _backend_label_of(c) == want), None)
        values.append(hit)
        lost = lost or bool(want and hit is None)
    n_ok = sum(1 for v in status.values() if _status_ok(v))
    msg = (f"已检测 {len(status)} 个模型服务:{n_ok} 个可用"
           + ("" if n_ok else " —— 一个都连不上,先把服务起起来"))
    added = len(set(labels.values()) - set(old_labels.values()))
    if added:
        msg += f";配置里新增的 {added} 个服务已刷进下拉"
    if lost:
        msg += ";原先选中的那个已不在配置里,请重新选一个"
    return choices, values, msg


def _vlm_involved(mode: str, picks, how: str) -> bool:
    """这次跑批会不会真的调 VLM(决定三个并发旋钮灰不灰)。

    自选模块要按「只跑选中 / 跳过选中」两种语义分别算 —— 一律按"选了就算跑"
    会把「跳过任务判定」也算成要 VLM,灰不下来。
    """
    if mode == QUICK_SCAN:
        return False
    if mode != CUSTOM_SCAN or not picks:
        return True
    picked = set(picks)
    if how == "只跑选中":
        return any(c in picked for c in VLM_CHECKS)
    return any(c not in picked for c in VLM_CHECKS)


def _sets(plots, c_ep, c_fr, c_cap, manual: str) -> list:
    """界面上的几个旋钮 + 手写的参数覆盖 → `--set 路径=值` 列表。

    只发**用户真的动过**的项:同步图非默认才发,并发留空就不发 —— 界面不复制
    一份默认值,否则改了 default.yaml 而界面还在按老值发,神不知鬼不觉。
    手写的放最后:同一个键写两遍时后到者赢,人手写的优先级最高。
    """
    out = []
    key = _label_key(PLOT_MODES, plots, "flagged")
    if key != "flagged":
        out.append(f"pipeline.sync_plots={key}")
    for name, val in (("ep", c_ep), ("fr", c_fr), ("cap", c_cap)):
        try:                             # 填了看不懂的东西 = 当没填,不拦着开跑
            n = int(str(val).strip())
        except (TypeError, ValueError):
            continue
        if n >= 1:                       # 0/负数没有意义,同样当没填
            out.append(f"{CONC_KEYS[name]}={n}")
    out += [x.strip() for x in (manual or "").splitlines() if x.strip()]
    return out


def _label_key(mapping: dict, label: str, default: str) -> str:
    """{键: 界面说法} + 界面说法 → 键。选项对不上就退回默认,绝不抛。"""
    for k, v in mapping.items():
        if v == label:
            return k
    return default


def presentation(terminal: bool = False, root: str = "") -> dict:
    """theme/css/head 三件套(gradio 6 起只认 launch()/mount_gradio_app() 上的这三个
    关键字,传给 `gr.Blocks()` 会被静默丢弃——2026-07-29 实测,顺手修掉的老 bug)。"""
    import gradio as gr

    return {
        # 系统字体:默认主题会向 fonts.googleapis.com 拉字体,国内网络挂起 15s+ 才放行
        # 首屏(实测),demo 一开场就是白屏等待——本 UI 场景里无网络字体的理由
        "theme": gr.themes.Default(font=["system-ui", "sans-serif"],
                                   font_mono=["ui-monospace", "Menlo", "monospace"]),
        "css": _ARCO_CSS + _AUDIT_CSS + _TOPNAV_CSS + (_TERMINAL_CSS if terminal else ""),
        # ↑ 顶层导航常驻 ⇒ 它的样式也常驻;终端专属样式/资产仍只在开终端时注入
        "head": (_TABLE_JS + _DROPDOWN_JS
                 + _TIP_JS.replace("__TIPS__", json.dumps(SCAN_TIPS, ensure_ascii=False))
                 + _TASK_POLL_JS
                 + (_terminal_head(root) if terminal else "")),
        # 标签页图标:不设就是 Gradio 自带的橘色 logo(用户 2026-08-13 点名)。
        # 换成 Arco 蓝圆角方块 + 白色漩涡(照 Daft 那枚的手感重画,底色主色化 ⇒
        # 既认得出这套系统的出身,又和整页的 Arco 蓝一致)。生成脚本
        # scripts/make_favicon.py,纯 stdlib 画的,改形状重跑即可。
        # gradio 只收 .png/.gif/.ico(不吃 svg),所以资产是张 64px PNG。
        "favicon_path": FAVICON,
    }


# 「人工裁决」页的视觉件(2026-08-07 用户反馈:页面太平淡,引导和区块头要一眼看到)。
# 全部内联样式:不依赖主题变量,浅色页面直出。
# 2026-08-13 用户定:引导框改 Arco 蓝(arcoblue-1 底 / -2 边 / -6 主色 / -7 深字)。
# 2026-08-16 合并队列重构后重写文案:原来教的"先裁标注 → 执行 → 再裁成败 →
# 再执行"两趟工序是分区制的产物;现在一条 episode 一张卡、问题一次答完,引导
# 只剩一件要防的事 —— 只改标、留空成败的条目重判后可能仍判不出会回到队列,
# 提前说明白,免得用户以为系统坏了。

def _adj_section_html(num: str, title: str, subtitle: str, color: str, dark: str) -> str:
    """区块头:色块序号 + 加粗标题 + 弱化副题,底部同色粗线把区块"框"出来。
    num 传空串则不画序号色块(「被拒复议」单独成子页签后,页内只有它一块,
    编号反而暗示还有别的块没找到)。"""
    badge = (f'<span style="background:{color};color:#fff;font-weight:800;'
             f'border-radius:8px;padding:2px 13px;font-size:1.05rem">{num}</span>'
             if num else "")
    return (f'<div style="display:flex;align-items:baseline;gap:10px;margin:20px 0 4px;'
            f'padding-bottom:7px;border-bottom:3px solid {color}">'
            f'{badge}'
            f'<span style="font-size:1.18rem;font-weight:800;color:{dark}">{title}</span>'
            f'<span style="color:#86909C;font-size:.9rem">{subtitle}</span></div>')


#: 空交付根的占位交付名与占位跑批名(跑批名要过 is_run_name:时间戳格式)
WELCOME_DELIVERY = "welcome"
WELCOME_RUN = "20260101-000000"


def _bootstrap_empty_delivery(root: str) -> None:
    """空交付根 → 放一份占位交付 `welcome/20260101-000000/passed.json`。

    目录建不了/写不进就让异常抛给调用方 —— 那是"交付根不可写"的部署问题,
    该响亮失败。写法走 safe_write.publish_file(目标目录内临时名 + os.replace):
    交付根在 TOS 挂载上的部署不怕直写坑。幂等:已有占位就不重写。
    """
    import tempfile

    from ..export.safe_write import publish_file
    run_dir = os.path.join(root, WELCOME_DELIVERY, WELCOME_RUN)
    dst = os.path.join(run_dir, "passed.json")
    if os.path.exists(dst):
        return
    os.makedirs(run_dir, exist_ok=True)
    payload = {"生成时间": "",
               "数据集": "(还没有交付 —— 到「跑质检」页跑第一次质检)",
               "dataset": {}, "episodes": {}}
    fd, tmp = tempfile.mkstemp(prefix="curation-welcome-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        publish_file(tmp, dst)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def build_app(delivery: str, config_path: str | None = None, probe_timeout: float = 5.0,
              terminal: bool = False, review_dir: str | None = None,
              data_root: str | None = None):
    """交付目录(或含多份交付的父目录)→ gr.Blocks。

    terminal=True 时套一层顶层导航:「终端」(内嵌 xterm.js,后端是本服务的
    `/ws/term`)+「质检报告」(= 本文件原有的全部内容),默认选中「质检报告」。
    缺省 False → 顶层导航整个不渲染,页面与加这层之前逐字一致(客户部署根本看不到
    终端入口),`/ws/term` 路由也不注册。

    review_dir = 审片站根目录(与 `/review` 静态路由同一个),Episodes 页的视频
    来源链第一档指着它;不给就只剩交付集内的视频。

    data_root = 数据集根目录(「跑质检」页只列这个根下的数据集)。
    ⚠️ 任务台是 2026-08-13 **纯新增**的页签,放在全部页签的最后:现有那套质检报告
    页签(质检总览/Episodes/人工裁决/技能画像/同步曲线/Stuck 时间线/明细/性能剖析/
    后端状态)的顺序、默认落地页、组件与回调**一律不动**(用户红线)。它也不共用
    `state` 与 `outs`——Episodes 那个"重载级联并发吞掉翻页按钮"的 bug(6bb28b5)
    就是共享输出列表惹的祸,不重演。
    """
    import gradio as gr

    choices = discover_deliveries(delivery)
    if not choices:
        # 全新部署的交付根是空的(2026-08-20 同事在 rerun 侧部署 e3307fb2 时
        # 撞上:/data/deliveries 里一份交付都没有,UI 启动即退)。空根不是错,
        # 是"还没跑过" —— 放一份占位交付让页面能起来,第一次质检跑完它就
        # 被真交付挤到后面。思路来自公开 PR#65 的 _bootstrap_empty_delivery,
        # 写法改走 safe_write(交付根可能在挂载上,copyfile 是直写坑家族)。
        _bootstrap_empty_delivery(delivery)
        clear_discover_cache()
        choices = discover_deliveries(delivery)
    if not choices:
        raise SystemExit(f"交付根不可写,连占位交付都放不进去:{delivery}"
                         "(检查 --delivery 指向的目录是否存在且可写)")

    def _ep_list(m, bucket, page, selected):
        """左清单的一屏(装配顺序 = _ep_list_outs)。分页口径全在 manifest。"""
        v = episode_list_view(m or {}, bucket or BUCKET_ALL, page or 0, selected)
        multi = gr.update(visible=v["pages"] > 1)
        return (v["page"], selected,
                gr.update(choices=v["choices"], value=v["value"]),
                v["pos"], multi, multi)

    def _detail(m, eid):
        """选中 episode → 判决卡 / 视频区 / 待人工指路 / 检查明细。返回顺序 = _ep_outs。

        层级是定死的(2026-08-11 用户拍板):判决与理由在最上,**视频是主角**,
        逐维读数退到默认折叠的明细里。静态证据帧整块撤掉(用户原话:体验太差)。
        """
        if not m or not eid:
            return (episode_card_html(m or {}, ""), "", "", "", "",
                    gr.update(value=None, visible=False))
        plot = (m["episodes"].get(eid) or {}).get("plot")
        return (episode_card_html(m, eid), episode_video_html(m, eid, review_dir, data_root),
                manual_hint_html(m, eid), check_table_html(m, eid),
                sync_camera_html(m, eid),
                gr.update(value=plot, visible=bool(plot)))

    def _detail_table_md(m, name):
        if not m or not name:
            return "(此交付无明细表)", ["(无)"], []
        headers, rows, total = load_detail_table(m, name)
        note = f"**{DETAIL_LABELS.get(name, name)}** · 共 {total} 行" + \
               (f"(仅显示前 {len(rows)} 行,完整文件见本次跑批目录下的 details/{name})"
                if total > len(rows) else "")
        return note, headers or ["(空)"], rows

    # ── 裁决卡片的公共装配(三键状态 / 视频槽 / 逐条渲染):两块裁决面板形态
    #    完全一样(队列表 + 翻页卡片 + 三键),差别只在字段与裁决词,所以抽在
    #    这里共用——复制粘贴两份的话,下次改按钮反馈必然只改一边。──

    def _btns(choices, current):
        """三键状态:未裁决=全中性灰;已裁决=只有选中的那个高亮(2026-08-06 用户定:
        按钮各有颜色时按下没反馈,分不清成没成功;改为"同色待选、选中变色",
        且翻页/跳行回到已裁决条目时按钮状态跟着该条的落盘裁决走)。
        choices 的顺序 = 界面上三个按钮的排列顺序。"""
        return [gr.update(variant=("primary" if c == current else "secondary"))
                for c in choices]

    #: ① 标注块里**摆出来的**三个键。⚠️ 不能直接用 DECISION_CHOICES:2026-08-19
    #: 起那个常量有四项(多了「弃用该条」),而「弃用该条」的入口已经提到卡片级的
    #: 「其它原因-整条弃用」,不在这个块里。拿常量当按钮清单的话,_au_btns 会多
    #: 返回一个 update,_load 的返回值就和输出槽对不齐(实测 88 vs 87,八条测试
    #: 一起红)—— "选项集"和"按钮排布"是两件事,别让一个常量兼任。
    AU_BLOCK_BTNS = ("采纳建议改标", "维持原标注", _HOLD)

    def _au_btns(decision):
        return _btns(AU_BLOCK_BTNS, decision)

    def _tv_btns(verdict):
        return _btns(VERDICT_CHOICES, verdict)

    def _ap_btns(appeal):
        return _btns(APPEAL_CHOICES, appeal)

    def _vids(m, eid):
        """三路视频槽:有几路给几路;一路都没有时保留一个可见占位(不然卡片塌掉)。"""
        clips = audit_clip_paths(m, eid) if eid else []
        return [gr.update(value=(clips[i] if i < len(clips) else None),
                          visible=(i < len(clips) or not clips)) for i in range(3)]

    # ── 「待你裁决」合并卡片(2026-08-16):一条 episode 一张卡,标注问题(①)
    #    与成败问题(②)挂在同一张卡上,视频只看一遍。队列合并/筛选/②显隐档位
    #    全在 manifest(纯函数,可单测),这里只装配。──

    def _mg_au_info(m, it):
        """① 标注问题的卡片头(已裁状态 + 溯源一行)。

        溯源一行(2026-08-16):裁决跨跑批沿用,卡片上不写清"哪天裁的、这次
        是沿用还是没应用",用户会把三周前的人工结论当成本轮机器判的。
        """
        # 2026-08-19 用户点名「都是废话,删除」——这里印的每一项,**上面的队列表格
        # 里都已经有一列**:档位=「档位」列、分歧说明=「分歧说明」列、已裁决=
        # 「裁决」列。同一条信息在一屏里说两遍,第二遍就是噪音。
        # 「成败线判定:voc_tripwire」还是行话(内部状态名),按"M 编号不进界面"
        # 那条纪律本来就不该出现。
        # 已裁过哪一项由**按钮高亮**表达(_au_btns),不必再写一行字。
        return ""

    def _mg_tv_section(m, it, label_decision):
        """② 成败问题那一块的装配(显隐档位 + 文案 + 三键)。
        返回 (整块可见性, 档位说明, 卡片头, 读数行, 三键更新×3)。"""
        mode = success_block_mode(it, label_decision or "")
        if mode == "hidden":
            return (gr.update(visible=False), "", "", "",
                    *[gr.update(interactive=True)] * 3)
        _note, enabled = SUCCESS_MODES[mode]
        # 那句"你采纳了改标,可以顺手给成败结论(机器直接采信,不再重判);留空则
        # 由机器按新标注重判"是**解释性文字**,用户反复说过界面上不要(2026-08-19
        # 再次点名)。行为不变,只是不写在脸上:留空由机器重判这件事,用户点了才
        # 会知道,而知道它的时机是"我要不要点",不是"我在读一段说明"。
        # 这里唯一留的一句是**判断的依据**(2026-08-21 用户定):任务文本放在三个
        # 按钮正上方 —— 没有它,"完成了吗"无从判起;采纳改标后这句跟着换新标注。
        note = task_question_md(m, it["id"], load_label_decisions(m).get(it["id"]))
        v = load_task_verdicts(m).get(it["id"], {})
        trace = decision_trace_md(m, "verdict", it["id"])
        t = it.get("task")
        if t is not None:
            # 同上(2026-08-19 用户点名):当前判决 / 弃权原因 / 关键读数,右边那张
            # 队列表格里各有一列;已裁决由按钮高亮表达。全删。
            info = ""
            readings = ""
        else:
            # 只有标注问题的条目:② 因「采纳改标」而出现,机器没弃权,没有弃权
            # 原因和读数可讲 —— 只回显已有裁决与溯源,不硬凑读数
            info = ""
            readings = ""
        btns = [gr.update(variant=("primary" if c == v.get("verdict")
                                   else "secondary"), interactive=enabled)
                for c in VERDICT_CHOICES]
        return (gr.update(visible=True), note, info, readings, *btns)

    def _mg_head(m, it):
        """卡头两行:episode 与要答的问题;**任务标注常驻第二行**(2026-08-21 用户定:
        只判成败的卡此前看不到任务是什么,等于对着视频猜题)。"""
        # "本条要答:① + ②" 已删(2026-08-21 用户点名多余:两个问题块各自就在下面);
        # 任务标注用醒目块,episode 号只留一行小字
        eid = it["id"]
        return (f"**{eid}**\n\n"
                + task_reference_html(m, eid, load_label_decisions(m).get(eid)))

    def _mg_render(m, filt, idx):
        """渲染合并队列第 idx 张卡(越界回绕)。装配顺序 = _mg_outs。"""
        q = merged_queue_view(m or {}, merge_filter_mode(filt))
        if not q:
            return (0, "(本档没有待你裁决的条目)", "",
                    *[gr.update(value=None, visible=(i == 0)) for i in range(3)],
                    gr.update(visible=False), "", "", "", "",
                    *_au_btns(None),
                    gr.update(visible=False), "", "", "", "",
                    *[gr.update(variant="secondary", interactive=True)] * 3)
        idx = idx % len(q)
        it = q[idx]
        eid = it["id"]
        dec = load_label_decisions(m).get(eid, {})
        info = _mg_head(m, it)
        if it["audit"] is not None:
            au_vis = gr.update(visible=True)
            au_info = _mg_au_info(m, it)
            origlab = it["audit"].get("label", "")
            newlab = it["audit"].get("caption", "")
        else:
            au_vis, au_info, origlab, newlab = gr.update(visible=False), "", "", ""
        tv_sec = _mg_tv_section(m, it, dec.get("decision", ""))
        return (idx, f"第 {idx + 1} / {len(q)} 条", info, *_vids(m, eid),
                au_vis, au_info, origlab, newlab, "",
                *_au_btns(dec.get("decision")),
                tv_sec[0], tv_sec[1], tv_sec[2], tv_sec[3], "",
                *tv_sec[4:])

    def _ap_render(m, idx):
        """渲染第 idx 条被拒复议卡片(越界回绕)。装配顺序 = _ap_outs。"""
        q = (m or {}).get("reject_appeal") or []
        if not q:
            return (idx, "(无可复议的被拒条目)", "", "", "", *_ap_btns(None))
        idx = idx % len(q)
        a = q[idx]
        eid = a.get("id", "")
        d = load_reject_appeals(m).get(eid, {})
        trace = decision_trace_md(m, "appeal", eid)
        info = (f"**{eid}** · 系统判决:拒绝"
                + (f" · 已复议:**{d['appeal']}**" if d.get("appeal") else "")
                + f"\n\n{task_reference_html(m, eid)}"
                + f"\n\n**拒绝原因**:{appeal_reason_text(m, eid) or '未注明'}"
                + (f"\n\n{trace}" if trace else ""))
        readings = f"关键读数:{readings_text(a.get('readings') or {})}"
        return (idx, f"第 {idx + 1} / {len(q)} 条", info, readings,
                episode_video_html(m, eid, review_dir, data_root), *_ap_btns(d.get("appeal")))

    def _sync_view(m, mode, page):
        """同步曲线页的一屏(装配顺序 = _sy_outs)。分页/筛选逻辑全在 manifest。"""
        v = sync_view(m or {}, mode or SYNC_FILTER_ALL, page or 0)
        # items = [(路径, 标题)];逐槽位填充,多出来的槽位隐藏(不留空框)
        multi = gr.update(visible=v["pages"] > 1)
        return v["page"], v["note"], v["pos"], multi, multi, v["cards"]

    def _load(path):
        # 直连批次(2026-08-20 阶段4):值是完整 tos:// URL → 先懒镜像到本地
        # 缓存(报告/明细/裁决片段,数据集本体不下载),再让下面的加载原样吃
        # 缓存路径 —— 读端代码零改动。镜像失败落到 load_error 那条路,整页
        # 明说读不到,不静默
        if str(path or "").startswith("tos://"):
            try:
                path = runner.mirror_run(str(path),
                                         _rp_src["region"] or None)
            except Exception as e:  # noqa: BLE001 网络/SDK 异常族杂
                gr.Warning(f"镜像这份交付失败:{type(e).__name__}: "
                           f"{str(e)[:120]}")
                path = ""
        # 镜像交付:打开时顺手探一次源桶可写(2026-08-21)。只读 → 裁决记了也写
        # 不回去,给 m 打上 tos_readonly,人工裁决页按它上锁;看报告不受影响
        ro_why = ""
        origin = runner.tos_origin_of(path) if path else None
        if origin:
            ok, why = runner.writable_verdict(
                origin["delivery_url"] + "/human-decisions",
                origin.get("region") or None)
            ro_why = "" if ok else why
        # 先把下拉里的值还原成真正的目录(手输半截字的情形,见 resolve_delivery);
        # 还原不了的照旧交给 load_delivery,它会挂 load_error 让整页明说读不到
        m = load_delivery(resolve_delivery(path, discover_deliveries(delivery)))
        if ro_why and isinstance(m, dict):
            m["tos_readonly"] = ro_why
        eids = bucket_ids(m, BUCKET_ALL)
        first = eids[0] if eids else None
        # 视觉质量那张表单独成页了 → 从下拉里撤掉(同一份数据不给两个入口)
        tables = detail_table_choices(m)
        t_first = tables[0] if tables else None
        note0, h0, r0 = _detail_table_md(m, t_first)
        vnote, vh, vr = video_detail_view(m)
        tl = load_timeline(m)
        tl_note0 = f"口径:{tl['note']}" if tl.get("note") else ""
        perf = load_perf(m)
        # 沿用计数进表下小字区(2026-08-16 用户拍板):**绝不往对账表里加行** ——
        # 那张表的口径是「输入 = 判废 + 精确去重删除 + 交付」,加一行就破了对账。
        # 零沿用时是空串,口径小字保持原样一个字不多。
        ov_note = overview_note_md(m)
        carry = carryover_note_md(m)
        if carry:
            ov_note = f"{ov_note}\n\n{carry}" if ov_note else carry
        # 详情面板随交付切换一起刷新:换目录后选中 eid 若恰好同名(ep000000 常见),
        # Dropdown 值不变→change 不触发→详情停留在上一份交付的陈旧渲染(实测踩过)
        return (m, overview_markdown(m), overview_rows(m), ov_note,
                _config_yaml(m),
                # 桶随交付切换复位到「全部」:停在「拒绝」而新交付一条都没被拒,
                # 看到的是空清单 + 一个还亮着的桶,等于骗人
                gr.update(choices=bucket_choices(m), value=BUCKET_ALL),
                *_ep_list(m, BUCKET_ALL, 0, first),
                skill_bar_html(m), skill_rows(m), audit_note_md(m),
                # 「待你裁决」从「全部」档第 0 条重新起(换交付不复位 = 停在
                # 上一份交付的条目上,按钮状态还是旧的,实测踩过)
                gr.update(choices=merged_filter_choices(m),
                          value=merged_filter_choices(m)[0]),
                readonly_banner_md(m) + (merged_hint_md(m) or ""),
                merged_queue_rows(m),
                *_mg_render(m, MERGE_FILTER_ALL, 0),
                # 被拒复议:没有可复议条目就整块不渲染;子页签藏不掉,空着一片
                # 会像页面坏了,所以空态时换一句说明顶上
                gr.update(visible=bool(m.get("reject_appeal"))),
                gr.update(visible=not m.get("reject_appeal")),
                appeal_hint_md(m), appeal_rows(m), *_ap_render(m, 0),
                *_detail(m, first),
                gr.update(choices=[DETAIL_LABELS[t] for t in tables],
                          value=(DETAIL_LABELS[t_first] if t_first else None)),
                note0, gr.update(value=r0, headers=h0),
                vnote, gr.update(value=vr, headers=vh),
                # 同步曲线页:筛选与页码一起复位(理由同上)
                gr.update(value=SYNC_FILTER_ALL),
                *_sync_view(m, SYNC_FILTER_ALL, 0),
                sync_conclusion_html(m), sync_health_html(m),
                # 换交付时筛选/排序一起复位(理由同上:停在上一份的筛选上,
                # 看到的条数和标题对不上)
                gr.update(value=TL_FILTERS["both"]), gr.update(value=TL_SORTS["episode"]),
                tl_note0, timeline_html(tl),
                perf_backend_md(perf), perf_env_md(perf), LATENCY_NOTE,
                latency_rows(perf), latency_bar_html(perf),
                # 顶部「有裁决尚未应用」提醒(排在 outs 末尾 = 纯新增,不动老槽位)
                unapplied_banner_md(m),
                # 「人工裁决 · 执行裁决」的状态行与源数据集兜底(2026-08-19,
                # 同样只在末尾追加,不动老槽位)
                *_ex_view(m))

    # theme/css/head 不在这里传:gradio 6 把它们从 Blocks() 挪到了 launch()/
    # mount_gradio_app()(传给 Blocks 只换来一条 UserWarning,值被丢掉)。见 presentation()。
    with gr.Blocks(title="Robot Data Curation") as app:
        gr.Markdown("# 机器人数据 Curation 质检台")
        # 顶层导航从左到右 =「跑质检 / 质检报告 / 终端」,默认落在**跑质检**
        # (2026-08-13 用户定:客户进来先看到能干活的面板;终端是排障用的,靠最右)。
        # terminal 关闭时终端页签整块不建 → 客户部署里看不到终端入口。
        with contextlib.ExitStack() as shell:
            # 顶层导航(2026-08-13 起**总是**渲染):「跑质检」(原「任务台」,2026-08-19
            # 改名并拍平——下面只剩一页,子页签层删掉)与「质检报告」并列,
            # 「终端」仍由 --terminal 控制。用户定:面板是面向客户的那张脸,压在
            # 报告页第十个子页签里等于没做。默认落地页仍是质检报告(selected=report),
            # 报告页那套子页签的顺序与内容一个字没动。
            # 顶层 Tabs 留个名字:报告页「人工裁决 · 执行裁决」确认后要把视图切到
            # 任务台看进度(gr.Tabs(selected=…) 作输出),没有引用就切不了。
            topnav = shell.enter_context(gr.Tabs(selected="console",
                                                 elem_id="topnav"))
            # ── 任务台(2026-08-13;布局与文案按用户当日反馈重排)──────────
            # 上半部 = 控制面板(客户来这里干活),下半部 = 任务与日志(干完看这里)。
            # 界面上**不写**"安全边界""并发配额"这类内部考量:那是我们的实现细节,
            # 客户只需要知道能点什么(用户点名删掉整段说明文字)。
            # 多 TOS 桶(2026-08-17):桶清单来自站点配置的 tos_buckets 段
            # (桶名 / 数据集目录 / 可选别名);没配就用 --data-root 合成单桶,
            # 跑批用的路径/argv 与今天完全一致(界面**有意**多一个「数据集根目录」
            # 下拉,单桶时也显示 —— 用户 2026-08-17 拍板:要随时看得见数据来自
            # 哪个桶,且与以后多桶时长得一模一样;标签叫「数据集根目录」也是他
            # 拍板:下拉实际选的就是"到哪个根目录下去列数据集",桶名含在里头)。
            # 下拉用 (显示文本, 内部标识) 成对:显示 = tos://桶/桶内前缀
            # (本地挂载路径不进显示串,挪到下拉底下的只读说明行;没挂上的根
            # 标 ⚠️ 未挂载),value = name(白名单查表的 key)—— 显示串永远
            # 不当标识用。
            # _data_root 保留 = **默认桶**的目录:裸名字深链与初始列表都落在它上。
            _given_root = data_root or os.environ.get("CURATION_DATA_ROOT")
            _buckets = runner.tos_buckets(config_path,
                                          _given_root or DEFAULT_DATA_ROOT,
                                          given_root=_given_root)
            runner.log_unmounted_roots(_buckets)   # 部署事故启动即点名进日志
            _bkt_ids = [b["name"] for b in _buckets]
            _bkt_choices = runner.bucket_dropdown_choices(_buckets)
            _data_root = _buckets[0]["datasets_path"]
            _deliv_root = runner.deliveries_root_of(delivery)
            # 公共数据集(2026-08-21):站点配置 public_datasets 有桶 → 「数据来源」
            # 多一项,桶同时登记成匿名读(读端/探针/列目录自动换不签名的客户端)。
            # 配置层按出厂→站点顺序套用,后者赢;读坏了一律当没配,只在日志里点名。
            _public_cfg = None
            for _layer in runner._config_layers(config_path):
                if isinstance(_layer, dict) and public_catalog.CONFIG_KEY in _layer:
                    try:
                        _public_cfg = public_catalog.apply_config(_layer)
                    except Exception as e:  # noqa: BLE001 坏配置不该让界面打不开
                        print(f"[curation-ui] public_datasets 配置不可用,忽略:{e}",
                              file=sys.stderr)
                        _public_cfg = None
            # 存储形态自检(2026-08-20):没挂载的实例在启动日志里说清默认走直连,
            # 别让人从"交付怎么没了"反推
            _shape = runner.deployment_shape_note(_deliv_root, _data_root)
            if _shape:
                print(f"[curation-ui] {_shape}", file=sys.stderr)
            _runs_root = runner.runs_root_of(_deliv_root)
            # {人话标签: 内部代号}。**原地更新**(不重新绑名字):探活时会重读配置
            # 刷新它,下面那几个闭包要跟着一起看见新的表。
            _backend_map = runner.vlm_backend_labels(config_path)
            _backends = list(_backend_map)
            _conc_defaults = runner.concurrency_defaults(config_path)

            def _backend_status() -> dict:
                """{预设代号: (在线?, 不可用短原因)}(探活一次,带 60s 缓存,给下拉标可用性用)。
                下拉里只放短语(密钥未配置 / 服务不可达 …),长原因留给 `curation backends`。"""
                from ..adapters.vlm_client import short_reason
                return {name: (("在线" in state), "" if "在线" in state else short_reason(detail))
                        for name, state, detail
                        in _probe_backends_cached(config_path, probe_timeout)}

            def _backend_choices(status: dict | None = None) -> list:
                """下拉选项:未检测时只给名字;检测过就把状态缀在后面。"""
                return _backend_options(_backend_map, status)

            def _backend_code(choice: str):
                """下拉选中项 → 预设代号(容忍带「· 可用/暂不可用」后缀)。"""
                return _backend_map.get(_backend_label_of(choice))

            def _done_run_note(st) -> str:
                """跑批**成功结束**的任务卡片上,"还有裁决没应用"的那句提醒。

                那是离「忘记执行裁决」最近的时刻(2026-08-16 用户点名)。计数走
                run_decision_records(带 mtime 缓存 —— 本函数在 2 秒轮询里,不能
                每跳都去 FSX 读几 MB 的 passed.json)。这句提醒是锦上添花,算不出
                来(目录还没可见/结构意外)就闭嘴,绝不能把整个轮询拖炸。
                """
                if not st or st.get("state") != "done":
                    return ""
                try:
                    run_dir = runner.run_output_dir(st)
                    return unapplied_card_note(run_dir) if run_dir else ""
                except Exception:  # noqa: BLE001  见上:提醒挂了不许连累状态条
                    return ""

            def _run_btn(st):
                """「开始质检」按钮该长什么样(issue #55,2026-08-19):有任务在跑
                就置灰、文案自己说明为什么 —— 界面不再长得像"可以再开一个",点了
                才挨一句黄字。判忙在 runner.is_busy_state(纯函数,fail-open:
                算不出状态一律恢复可点,误禁比误放贵得多,理由见那边 docstring)。
                任务落终态后由同一条 2 秒轮询把按钮放回蓝色,不需要人刷新。
                """
                if runner.is_busy_state(st):
                    return gr.update(interactive=False, value="有任务在跑")
                return gr.update(interactive=True, value="开始质检")

            def _stop_btn(st):
                """「停止」只在有任务在跑/正在停时可点(issue #56)。判忙同 _run_btn。"""
                return gr.update(interactive=runner.is_busy_state(st))

            def _tk_view(msg: str = ""):
                """当前任务(没有在跑的就显示最近一个)→ 状态条 + 日志尾部 + 提示
                + 「开始质检」按钮状态(issue #55:按钮与状态条同源,永不各说各话)。"""
                st = (runner.active_run(_runs_root)
                      or next(iter(runner.list_runs(_runs_root, limit=1)), None))
                if not st:
                    return (runner.status_html(None), "", msg, _run_btn(None),
                            _stop_btn(None))
                logtxt = runner.tail_log(_runs_root, st["run_id"])
                # 累积进度(2026-08-13 用户):跑完的阶段留在原地,新阶段追加一根条 ——
                # 只画最后一条时,阶段一换就归零重来,等着的人看不出"已经过了几关"
                return (runner.status_html(st, runner.parse_progress_all(logtxt),
                                           extra=_done_run_note(st)),
                        logtxt, msg, _run_btn(st), _stop_btn(st))

            def _tk_start(command, label, then_argv=None, *, jobs=None,
                          run_id=None, **params):
                """统一的发起入口:拼 argv → 起任务 → 立刻回显状态。

                jobs 给了就是"顺序跑几个数据集"(见 runner.build_run_script),argv
                取第一个 job 的第一步 —— cmd.json 里那一栏仍能看出这是条什么命令,
                完整作业表另存一份。

                一切异常都变成界面上的一句话(参数不合法/路径越界/已有任务在跑),
                绝不让 Gradio 抛红框——那对客户等于什么都没说。
                """
                try:
                    argv = jobs[0]["steps"][0] if jobs else runner.build_argv(
                        command, **params)
                    runner.start(_runs_root, command, argv, label=label,
                                 cwd=_deliv_root, then_argv=then_argv, jobs=jobs,
                                 run_id=run_id)
                except runner.RunBusyError as e:
                    return _tk_view(f"⚠️ {e}")
                except (ValueError, OSError) as e:
                    return _tk_view(f"⚠️ 没能开始:{e}")
                return _tk_view("已开始,下面会自动刷新进度")

            with gr.Tab("跑质检", id="console") as console_tab:
                # 顶层直接叫「跑质检」(2026-08-19 用户拍板:原「任务台」下面
                # 只剩这一页,没必要再套一层子页签 —— 原「跑质检」子页签删掉,
                # 内容提到顶层)。id 仍是 console:报告页「执行裁决」确认后要
                # gr.Tabs(selected="console") 切过来看进度,改名不改锚点。
                # ① 控制面板在上
                # 头部两行三件套(2026-08-20 用户定稿,融合公开 PR#65 的
                # 「允许填桶路径」思路但不丢我们的下拉):
                #   [数据集目录][数据集 ▼多选][地区 ▼]
                #   [交付目录][交付名][地区 ▼]
                # 路径框默认预填本实例配置桶的 tos:// 写法 → 默认体验与
                # 之前的「数据集根目录」下拉逐字节等价(挂载零预下载直读);
                # 改填陌生桶 = 走 stage_in/stage_out 直连。红线口径
                # (2026-08-20 拍板):允许自由填 tos:// URL,**本地自由
                # 路径仍然禁止**(resolve_root_input 一句话拒)。
                # 两行 scale 逐列相同(4/4/2),路径框和地区下拉上下对齐。
                # ⚠️ 控件不用 info=(2026-08-17 实机:info 会把带说明的列
                # 往下推,三列错位),说明全在下面独立说明行。
                _rg0 = runner.default_tos_region()
                _rg_choices = runner.tos_region_choices()
                with gr.Row(equal_height=True):
                    # 「数据集目录」的标题行自己画(2026-08-21 用户定稿):标签右边
                    # 紧挨着「私有 / 镜像」二选一 —— gradio 的原生标签里塞不进控件,
                    # 所以标签用 Markdown 画、Textbox 自己的标签藏起来(label 仍叫
                    # 「数据集目录」,测试与深链按它定位)。勾上:目录框/地区填镜像桶
                    # 并置灰,只剩数据集下拉可选;交付目录一概不动。没配置就不出现。
                    _src_public = public_catalog.source_label()
                    with gr.Column(scale=4, min_width=160, elem_id="rn-tin-col"):
                        with gr.Row(elem_id="rn-tin-head"):
                            gr.Markdown("数据集目录", elem_classes=["field-label"])
                            rn_pub = gr.Radio([SRC_PRIVATE, _src_public],
                                              value=SRC_PRIVATE, show_label=False,
                                              container=False, elem_id="rn-pub",
                                              visible=bool(_public_cfg),
                                              scale=0, min_width=80)
                        rn_tin = gr.Textbox(label="数据集目录", show_label=False,
                                            value=runner.bucket_url(_buckets[0]),
                                            placeholder="tos://桶名/目录")
                    # 多选(2026-08-13 用户):一次点击顺序跑选中的这几个。
                    rn_ds = gr.Dropdown(choices=runner.list_datasets(_data_root),
                                        label="数据集", scale=4,
                                        multiselect=True, interactive=True)
                    # 地区下拉与 rerun 的 OpenTosModal 同值同序;列表可能落后
                    # 于新地区,允许自由输入
                    rn_tin_rg = gr.Dropdown(choices=_rg_choices, value=_rg0,
                                            label="地区", scale=2, min_width=240,
                                            allow_custom_value=True,
                                            interactive=True)
                # 交付目录默认值(2026-08-21 用户定):永远是个 tos:// 地址 —— 本实例
                # 有桶就用自己的;没桶的实例先留空(占位符),数据集目录定下来后借它
                # 的桶(_borrow_output)。绝不再把本地盘路径摆在这儿冒充交付目录。
                _home_out = runner.home_output_url(_deliv_root)
                _out_default = _home_out if _home_out.startswith("tos://") else ""
                with gr.Row():
                    rn_tout = gr.Textbox(label="交付目录", scale=4,
                                         value=_out_default,
                                         placeholder="tos://桶名/目录")
                    rn_out = gr.Textbox(label="交付名", scale=4,
                                        placeholder="给这次结果起个名字")
                    rn_tout_rg = gr.Dropdown(choices=_rg_choices, value=_rg0,
                                             label="地区", scale=2, min_width=240,
                                             allow_custom_value=True,
                                             interactive=True)
                # 说明行(2026-08-17 从 info= 挪出):第二行按与控件行
                # **相同的 scale(3/4/4)**分三列,说明各自落在自己那列
                # 底下,第三列留空占位(交付名没有说明)。gr.Markdown
                # 不收 scale,所以套 Column;min_width 与上一行控件默认
                # 值一致,否则窄屏时两行列宽算不齐。
                # 说明为空时不留空白的判据:gr.Markdown 空串在前端渲染
                # 零高度(同 pending_banner 的先例"空串不占位"),行高
                # 跟着最高的那列走 —— 第一列的「挂载:」几乎永远在
                # (datasets_path 是桶配置的必填项),右列为空不塌行。
                with gr.Row():
                    with gr.Column(scale=4, min_width=160):
                        # 根目录的说明行:挂载桶时**空着**(2026-08-21 用户:端点/
                        # 挂载路径两行删掉,界面不印读取细节);填陌生桶时这里换成
                        # 直连说明,出错时放原因(_root_changed 负责)
                        rn_src_note = gr.Markdown(
                            "", line_breaks=True, elem_id="rn-src-note",
                            elem_classes=["field-note"])
                    with gr.Column(scale=4, min_width=160):
                        # 根目录三态说明(没挂上/挂了但空/正常时空串不打扰)
                        # —— "下拉是空的"必须能分清是部署事故还是确实没数据
                        rn_ds_note = gr.Markdown(
                            runner.dataset_root_note(_data_root),
                            elem_id="rn-ds-note",
                            elem_classes=["field-note"])
                    with gr.Column(scale=2, min_width=120):
                        # 交付名的说明挪到第二列下面放不下了,跟着交付名走
                        # 到第二行;地区列不需要说明,空串占位
                        gr.Markdown("", elem_classes=["field-note"])
                with gr.Row():
                    with gr.Column(scale=4, min_width=160):
                        # 交付目录的说明:借了谁的桶 / 写不进去的原因
                        rn_tout_note = gr.Markdown("", elem_id="rn-tout-note",
                                                   elem_classes=["field-note"])
                    with gr.Column(scale=4, min_width=160):
                        rn_out_hint = gr.Markdown(
                            OUT_NAME_HINT_ONE, elem_id="rn-out-note",
                            elem_classes=["field-note"])
                    with gr.Column(scale=2, min_width=120):
                        gr.Markdown("", elem_classes=["field-note"])
                # 交付目录写不进去时的对话框(2026-08-21 用户定:填完瞬间弹窗,点
                # 「确定」交付目录清空只剩占位符)。样式复用「执行裁决」的 modal-dialog。
                rn_tout_auto = gr.State(True)   # 交付框还是系统代填的?(用户手改过就不再代填)
                with gr.Column(visible=False, elem_id="out-ask",
                               elem_classes=["modal-dialog"]) as out_ask:
                    out_ask_md = gr.Markdown()
                    with gr.Row(elem_id="out-ask-btns"):
                        out_ask_ok = gr.Button("确定", variant="primary", scale=0,
                                               elem_id="out-ask-ok")
                # 数据集目录读不到时的对话框(2026-08-21 用户问"地址和地区对不上会不会
                # 跳出来提示":会,与交付目录同一套;「确定」只关窗不清空 —— 读侧多半是
                # 地区选错,值留着让人改地区就行)
                with gr.Column(visible=False, elem_id="in-ask",
                               elem_classes=["modal-dialog"]) as in_ask:
                    in_ask_md = gr.Markdown()
                    with gr.Row(elem_id="in-ask-btns"):
                        in_ask_ok = gr.Button("确定", variant="primary", scale=0,
                                              elem_id="in-ask-ok")
                # 「快速质检」原叫「快速冒烟(跳过模型判定)」——"冒烟"是
                # 我们的行话,"模型判定"客户也不知道指哪几步(2026-08-13
                # 用户点名)。改成大白话,细节挂在旁边的问号上。
                rn_mode = gr.Radio([FULL_SCAN, QUICK_SCAN, CUSTOM_SCAN],
                                   value=FULL_SCAN, label="质检范围",
                                   elem_id="qc-scope")
                rn_pick = gr.CheckboxGroup(
                    choices=[(v, k) for k, v in runner.CHECK_LABELS.items()],
                    label="要跑的模块", visible=False)
                rn_how = gr.Radio(["只跑选中", "跳过选中"], value="只跑选中",
                                  label="选中的这些…", visible=False)
                with gr.Row():
                    rn_max = gr.Number(label="只跑前 N 条(留空=全部)",
                                       value=None, precision=0)
                    rn_eps = gr.Textbox(label="指定 episode",
                                        placeholder="34 / 10-20 / 3,10-12")
                    with gr.Column(scale=2):
                        # 「检测可用性」按钮已撤(2026-08-21 用户:可用性该系统自己查):
                        # 开页与切到本页时自动探活,结果直接缀在选项上(含不可用原因)
                        # allow_custom_value(2026-08-21):探活会把选项改成带「· 可用/暂不可用」
                        # 后缀的,同时在飞的事件若带着旧值回来,Gradio 6 会以"值不在选项里"
                        # 报红;旧值经 _backend_code 剥后缀照样解析到同一个预设,放行无害
                        rn_backend = gr.Dropdown(choices=_backends, label="模型服务",
                                                 allow_custom_value=True)
                with gr.Accordion("更多设置", open=False):
                    rn_cfg = gr.Textbox(label="配置文件(留空=默认)",
                                        placeholder=f"{runner.TOS_ROOT}/…/site.yaml")
                    rn_emb = gr.Textbox(label="机器人型号(数据里没写时填,如 so101)")
                    rn_plots = gr.Radio(list(PLOT_MODES.values()),
                                        value=PLOT_MODES["flagged"],
                                        label="视频-动作同步的证据图")
                    # 三个并发旋钮(2026-08-13 用户要):默认值只进**占位符**,
                    # 不预填 value —— 界面不做第二套默认值(两套默认必然对不上,
                    # 见 _conc_placeholder)。不跑 VLM 的范围下整组置灰。
                    with gr.Accordion("并发(只影响用模型的那几步)",
                                      open=False) as rn_conc_box:
                        # 用 Textbox 不用 Number:gr.Number 把"没填"显示成
                        # **0**,看着像"并发设成 0"(实际是"用配置里的值")。
                        # 占位符能把这句话说清楚,Number 没有占位符。
                        with gr.Row():
                            rn_c_ep = gr.Textbox(
                                elem_classes=["conc-num"],
                                label="episode 并发(同时判定几条)",
                                placeholder=_conc_placeholder(
                                    _conc_defaults.get("ep")))
                            rn_c_fr = gr.Textbox(
                                elem_classes=["conc-num"],
                                label="单条内帧并发(一条里同时问几帧)",
                                placeholder=_conc_placeholder(
                                    _conc_defaults.get("fr")))
                            rn_c_cap = gr.Textbox(
                                elem_classes=["conc-num"],
                                label="打标并发(技能打标同时跑几条)",
                                placeholder=_conc_placeholder(
                                    _conc_defaults.get("cap")))
                        rn_conc_note = gr.Markdown()
                    rn_set = gr.Textbox(label="参数覆盖(一行一条)", lines=2,
                                        placeholder="pipeline.sync_plots=all")
                    with gr.Row():
                        # 「覆盖同名结果」2026-08-14 撤掉:每次跑批各进各的
                        # 时间戳子目录,同名再跑也不会碰上一次的结果,没有可
                        # 覆盖的东西;要清理旧跑批走 `curation prune`(先列
                        # 后删)。覆盖那条路曾把人工裁决一起 rmtree 掉。
                        rn_batch = gr.Checkbox(label="跑根目录下的全部数据集")
                        rn_ro = gr.Checkbox(label="只出报告,不导出数据集")
                with gr.Row():
                    rn_go = gr.Button("开始质检", variant="primary", scale=0)
                # v3 / rrd 数据集的追问面板(默认隐藏)。Gradio 没有原生模态框,
                # 用"默认隐藏的一块 + 两个按钮"代替 —— 语义一样:先问再做。
                # 为什么要问:这两种格式盘上没有逐条视频,不切片则 Episodes 页
                # 打开某条只有提示语没有画面;而切片要重新编码,几分钟到十几分钟,
                # 不该背着用户悄悄花掉。
                with gr.Group(visible=False) as rn_ask:
                    rn_ask_md = gr.Markdown()
                    with gr.Row():
                        rn_yes = gr.Button("一起生成", variant="primary", scale=0)
                        rn_no = gr.Button("这次不用", scale=0)

                # 「执行人工裁决」页签 2026-08-19 整个删掉:决策在报告页、执行在
                # 任务台,两边不通气,几乎每个控件都在让用户把系统已知的上下文再
                # 输一遍。执行入口搬进「质检报告 · 人工裁决」页底部(证据旁边),
                # 作用对象就是当前加载的那份交付与那次运行(2026-08-19 定案)。

                # ② 任务与日志在下(合成一块,分子页签:当前任务 / 历史)
                gr.Markdown("### 任务与日志")
                # Arco line 型页签(全站 button[role=tab] 规则):它就在跑质检面板
                # 下面,两组页签长得不一样比都难看更糟。
                with gr.Tabs(elem_id="task-logtabs"):
                    with gr.Tab("当前任务"):
                        tk_status = gr.HTML(elem_id="tk-status")
                        tk_msg = gr.Markdown()
                        tk_log = gr.Textbox(label="日志", lines=14, max_lines=14,
                                            interactive=False, autoscroll=True,
                                            elem_classes=["mono-log"])
                        with gr.Row():
                            tk_refresh = gr.Button("刷新", scale=0, size="sm",
                                                   elem_id="tk-refresh")
                            # 停止只在有任务时可点(issue #56,2026-08-21):任务已经
                            # 落终态还亮着一个红按钮,点了"没反应"是必然的
                            # 与「刷新」同款(2026-08-21 用户点名:两个按钮一个带边框
                            # 一个不带,不统一);可点/不可点由状态驱动,不靠颜色喊
                            tk_stop = gr.Button("停止", variant="secondary", scale=0,
                                                size="sm", elem_id="tk-stop",
                                                interactive=False)
                    with gr.Tab("历史"):
                        hi_table = gr.Dataframe(headers=runner.HISTORY_HEADERS,
                                                interactive=False, wrap=True)
                        hi_pick = gr.Markdown()
                        hi_log = gr.Textbox(label="这次任务的日志", lines=14,
                                            max_lines=14, interactive=False,
                                            elem_classes=["mono-log"])
                # rn_go 挂在末尾(issue #55):每一条会刷新状态条的路(点击/轮询/
                # 手动刷新/app.load)同时刷新「开始质检」的可点性 —— 单独一条刷新
                # 链早晚和状态条各说各话。
                # tk_stop 同样挂在末尾(issue #56):停止按钮的可点性与状态条同源
                _tk_outs = [tk_status, tk_log, tk_msg, rn_go, tk_stop]

                # ── 回调(输出只落在任务台自己的组件上)────────────────────
                def _tk_mode(mode, picks, how):
                    custom = mode == CUSTOM_SCAN
                    on = _vlm_involved(mode, picks, how)
                    note = "" if on else "*这次不跑用模型的步骤,并发调了也没用。*"
                    return (gr.update(visible=custom), gr.update(visible=custom),
                            gr.update(interactive=on), gr.update(interactive=on),
                            gr.update(interactive=on), note)

                def _ds_hint(ds, batch):
                    """选了几个数据集 → 「交付名」那行说明换一句。

                    多选时交付名的含义从"这份交付叫什么"变成"这批交付放在哪个
                    文件夹下",不明说的话客户会以为几个数据集的结果会互相覆盖。
                    勾了「跑全部」时下拉本来就被忽略,说明也跟着回到单份那句。
                    """
                    many = not batch and len(runner.picked_datasets(ds)) > 1
                    return OUT_NAME_HINT_MANY if many else OUT_NAME_HINT_ONE

                rn_ds.change(_ds_hint, [rn_ds, rn_batch], rn_out_hint)
                rn_batch.change(_ds_hint, [rn_ds, rn_batch], rn_out_hint)

                def _src_datasets(src, multi_pick: bool):
                    """切数据集根目录 → (根那列的说明, 数据集下拉, 数据集那列的
                    说明)。旧选中值清掉(它属于上一个根,留着等于把 A 桶的名字
                    拿去 B 桶跑)。说明两处都要跟着换:端点/挂载是每个根各自的,
                    三态探测结果也是(没挂上 vs 挂了但空,dataset_root_note)。
                    查不到标识就原样不动 —— 下拉的选项本来就出自白名单,查不到
                    只可能是伪造请求。

                    ⚠️ 说明"该为空"时必须返回**空串,不许 None / 不许 gr.update()
                    跳过**:2026-08-17 实机踩过 gradio 把 None 当"这个字段不用改"
                    的坑 —— 切到未配端点的根,仍残留上一个根的端点;切回正常根,
                    仍挂着「⚠️没挂上」。界面拿旧信息冒充当前状态,正是要消灭的
                    那类错。说明如今是独立 Markdown(不再是 info=),空串直接就
                    是"清掉"的写法,单测钉在"返回值就是空串"上防回退。"""
                    try:
                        b = next(x for x in _buckets if x["name"] == str(src or ""))
                    except StopIteration:
                        return gr.update(), gr.update(), gr.update()
                    root = b["datasets_path"]
                    return ("",
                            gr.update(choices=runner.list_datasets(root),
                                      value=[] if multi_pick else None),
                            runner.dataset_root_note(root))

                # 用 .input 不用 .change:深链预选会从后端改这个下拉的值,.change
                # 对程序性赋值也触发,会紧接着把预选好的数据集列表冲掉
                def _root_changed(url, region):
                    """路径框失焦/回车、或直连时切地区 → (根说明, 数据集下拉,
                    数据集说明)。旧选中值清掉(它属于上一个根)。

                    直连列表是网络调用:异常一律变成说明行里的一句话,绝不让
                    Gradio 抛红框(客户看红框等于什么都没看见)。
                    """
                    try:
                        spec = runner.resolve_root_input(url, _buckets)
                    except ValueError as e:
                        return (f"⚠️ {e}", gr.update(choices=[], value=[]), "")
                    if spec["kind"] == "mount":
                        root = spec["path"]
                        return ("",
                                gr.update(choices=runner.list_datasets(root),
                                          value=[]),
                                runner.dataset_root_note(root))
                    if public_catalog.is_public_root(spec["url"]):
                        # 公共镜像桶:下拉显示「全名 · 版本 · 集数」,值仍是目录名
                        try:
                            ch = public_catalog.choices()
                        except Exception as e:  # noqa: BLE001
                            return (f"⚠️ {type(e).__name__}: {str(e)[:160]}",
                                    gr.update(choices=[], value=[]), "")
                        return ("", gr.update(choices=ch, value=[]),
                                "" if ch else "镜像里目前没有 LeRobot 格式的数据集")
                    try:
                        names = runner.tos_list_datasets(
                            spec["url"], str(region or "").strip() or None)
                        note = ("" if names else
                                "该前缀下没有列出任何数据集(前缀打错?或桶里"
                                "确实是空的)")
                    except Exception as e:  # noqa: BLE001 网络/SDK 异常族杂
                        # 404 对 TOS 来说"桶名错"和"地区错"一个样(2026-08-21 真机),
                        # 措辞两头都点到,并到别的地区找一圈,找到了直接说在哪
                        _kind, _detail = tos_store.classify_list_error(e)
                        _bkt = tos_store.parse_tos_url(spec["url"])[0]
                        _rg = str(region or "").strip() or None
                        if _kind == "missing":
                            why = runner.missing_bucket_text(
                                _bkt, _rg, _detail, locate=runner.region_of_bucket)
                        elif _kind == "forbidden":
                            why = f"本实例的密钥没有桶 {_bkt} 的读权限({_detail})"
                        else:
                            why = f"{type(e).__name__}: {str(e)[:120]}"
                        names, note = [], f"⚠️ 列不出该前缀下的数据集:{why}"
                    return (f"TOS 直连:{spec['url']}(直接从桶里读,不落本地盘)",
                            gr.update(choices=names, value=[]), note)

                # blur 与 submit 都接(填完点别处/回车都算"填完了");直连时
                # 切地区也要重列(端点跟着地区走)。用 .input 不用 .change:
                # 深链预填会程序性改值,.change 会紧接着把预选冲掉
                # 没挂载的实例默认值是直连地址(2026-08-20):建页面时列不到
                # 数据集(目录不存在),开门就列一次,让下拉别空着等人按回车
                def _borrow_output(tin, tin_rg, tout, auto):
                    """数据集目录定下来后给交付目录代填(2026-08-21 用户定)。

                    有自己桶的实例:代填自己的桶;没桶的实例:借数据集所在的桶
                    (tos://那个桶/deliveries),**借之前先探一下可写** —— 只读桶
                    不填,留占位符 + 一句原因(先填再弹窗是戏弄人)。用户手改过
                    交付框(auto=False 且非空)一律不碰。
                    """
                    hold = (gr.update(), gr.update(), gr.update(), auto)
                    if not auto and str(tout or "").strip():
                        return hold
                    url = runner.borrowed_output_url(tin, _deliv_root)
                    if not url:
                        return hold
                    if url == _home_out:          # 自己的桶:挂载直写/部署桶,不探
                        return gr.update(value=url), gr.update(), "", True
                    ok, why = runner.writable_verdict(
                        url, str(tin_rg or "").strip() or None,
                        locate=runner.region_of_bucket)
                    if ok:
                        return (gr.update(value=url),
                                gr.update(value=str(tin_rg or "").strip() or None),
                                f"交付目录默认借数据集所在的桶:{url}(可改)"
                                + (f";⚠️ {why}" if why else ""), True)
                    return (gr.update(value=""), gr.update(),
                            f"⚠️ 数据集所在的桶不能当交付目录:{why}。"
                            "请另填一个可写的 tos://桶名/目录", True)

                _bo_in = [rn_tin, rn_tin_rg, rn_tout, rn_tout_auto]
                _bo_out = [rn_tout, rn_tout_rg, rn_tout_note, rn_tout_auto]

                def _in_check(url, region):
                    """数据集目录填完/切地区 → 直连桶探一次可读,读不到就弹窗说原因
                    (桶名错 / 地区错 / 没权限);挂载桶与公共镜像桶不探。"""
                    hide = gr.update(visible=False)
                    try:
                        spec = runner.resolve_root_input(url, _buckets)
                    except ValueError:
                        return hide, ""
                    if spec["kind"] == "mount":
                        # 挂载桶不出网:地区与配置里的端点地区比对即可
                        bname = str(spec["bucket"].get("bucket") or spec["bucket"].get("name") or "")
                        why = runner.mount_region_mismatch(
                            bname, runner.mounted_bucket_region(spec["bucket"]),
                            str(region or "").strip() or None)
                        if not why:
                            return hide, ""
                        return (gr.update(visible=True),
                                f"**地区选错了**\n\n{why}")
                    if public_catalog.is_public_root(spec["url"]):
                        return hide, ""
                    ok, why = runner.readable_verdict(
                        spec["url"], str(region or "").strip() or None,
                        locate=runner.region_of_bucket)
                    if ok:
                        return hide, ""
                    return (gr.update(visible=True),
                            f"**这个数据集目录读不到**\n\n{why}\n\n"
                            "请核对桶名与地区后再试。")

                _ic_out = [in_ask, in_ask_md]
                in_ask_ok.click(lambda: gr.update(visible=False), None, in_ask)

                def _out_changed(tout, tout_rg):
                    """交付目录失焦/回车/切地区 → 真写一下探可写;写不进去立刻弹窗
                    (→ 确定后清空)。本实例交付根(挂载直写)不探。"""
                    hide = gr.update(visible=False)
                    s = str(tout or "").strip()
                    if not s:
                        return hide, gr.update(), ""
                    try:
                        spec = runner.resolve_output_input(s, _deliv_root)
                    except ValueError as e:
                        return hide, gr.update(), f"⚠️ {e}"
                    if spec["kind"] == "mount":
                        why = runner.mount_region_mismatch(
                            os.environ.get("TOS_BUCKET", "").strip() or "本实例",
                            runner.default_tos_region(), str(tout_rg or "").strip() or None)
                        if not why:
                            return hide, gr.update(), ""
                        return (gr.update(visible=True), f"**地区选错了**\n\n{why}",
                                f"⚠️ {why}")
                    ok, why = runner.writable_verdict(
                        spec["url"], str(tout_rg or "").strip() or None,
                        locate=runner.region_of_bucket)
                    if ok:
                        return (hide, gr.update(),
                                f"TOS 直连:跑完上传到 {spec['url']}"
                                + (f"(⚠️ {why})" if why else ""))
                    return (gr.update(visible=True),
                            f"**这个交付目录写不进去**\n\n{why}\n\n"
                            "点「确定」后交付目录会清空,请另填一个可写的桶。",
                            f"⚠️ {why}")

                _oc_out = [out_ask, out_ask_md, rn_tout_note]

                if str(runner.bucket_url(_buckets[0])).startswith("tos://") \
                        and not runner.is_mount_backed(_data_root):
                    app.load(_root_changed, [rn_tin, rn_tin_rg],
                             [rn_src_note, rn_ds, rn_ds_note]
                             ).then(_borrow_output, _bo_in, _bo_out)
                # ⚠️ 三条链排成串行 + 地区链取消在飞的目录链(2026-08-21 实机):点地区下拉
                # 时文本框先失焦触发一条链(旧地区 → "能读 → 关窗"),选完地区又触发一条
                # ("读不到 → 弹窗");两条并行跑,哪条后落地哪条赢,弹窗时有时无。
                # concurrency_id 让它们不并行;cancels 让"选地区"把还在飞的失焦/回车链
                # 整个掐掉 —— 界面状态永远对应用户最后一次操作。
                _ROOT_Q = "rn-root"
                _b1 = rn_tin.blur(_root_changed, [rn_tin, rn_tin_rg],
                                  [rn_src_note, rn_ds, rn_ds_note], concurrency_id=_ROOT_Q)
                _b2 = _b1.then(_borrow_output, _bo_in, _bo_out, concurrency_id=_ROOT_Q)
                _b3 = _b2.then(_in_check, [rn_tin, rn_tin_rg], _ic_out, concurrency_id=_ROOT_Q)
                _s1 = rn_tin.submit(_root_changed, [rn_tin, rn_tin_rg],
                                    [rn_src_note, rn_ds, rn_ds_note], concurrency_id=_ROOT_Q)
                _s2 = _s1.then(_borrow_output, _bo_in, _bo_out, concurrency_id=_ROOT_Q)
                _s3 = _s2.then(_in_check, [rn_tin, rn_tin_rg], _ic_out, concurrency_id=_ROOT_Q)
                _r1 = rn_tin_rg.input(_root_changed, [rn_tin, rn_tin_rg],
                                      [rn_src_note, rn_ds, rn_ds_note], concurrency_id=_ROOT_Q,
                                      cancels=[_b1, _b2, _b3, _s1, _s2, _s3])
                _r2 = _r1.then(_borrow_output, _bo_in, _bo_out, concurrency_id=_ROOT_Q)
                _r2.then(_in_check, [rn_tin, rn_tin_rg], _ic_out, concurrency_id=_ROOT_Q)
                # 用户亲手改交付框 → 以后不再代填(.input 只认用户动作)
                rn_tout.input(lambda: False, None, rn_tout_auto)
                rn_tout.blur(_out_changed, [rn_tout, rn_tout_rg], _oc_out)
                rn_tout.submit(_out_changed, [rn_tout, rn_tout_rg], _oc_out)
                rn_tout_rg.input(_out_changed, [rn_tout, rn_tout_rg], _oc_out)
                out_ask_ok.click(lambda: ("", gr.update(visible=False), ""),
                                 None, [rn_tout, out_ask, rn_tout_note])

                def _pub_changed(src):
                    """来源二选一 → (目录框, 地区, 数据集下拉, 根说明, 数据集说明)。
                    选镜像:目录框/地区填镜像桶并置灰(桶名照样可见,只是不用填),下拉
                    只列清单里 LeRobot 格式的;选私有:恢复本实例默认桶、可编辑、重列。
                    交付目录一概不碰(2026-08-21 用户:切来切去都不该重载它)。"""
                    if src == _src_public and public_catalog.configured():
                        root, rg = public_catalog.root_url(), public_catalog.region()
                        note, ds, ds_note = _root_changed(root, rg)
                        return (gr.update(value=root, interactive=False),
                                gr.update(value=rg or _rg0, interactive=False),
                                ds, note, ds_note)
                    home_url = runner.bucket_url(_buckets[0])
                    note, ds, ds_note = _root_changed(home_url, _rg0)
                    return (gr.update(value=home_url, interactive=True),
                            gr.update(value=_rg0, interactive=True),
                            ds, note, ds_note)

                rn_pub.input(_pub_changed, rn_pub,
                             [rn_tin, rn_tin_rg, rn_ds, rn_src_note, rn_ds_note],
                             concurrency_id=_ROOT_Q)
                # 报告页「执行裁决」的源数据集兜底下拉(ex_src_dd)仍用桶下拉
                # 那套(_src_datasets),接线在那侧组件建出来之后

                _mode_ins = [rn_mode, rn_pick, rn_how]
                _mode_outs = [rn_pick, rn_how, rn_c_ep, rn_c_fr, rn_c_cap,
                              rn_conc_note]
                for _c in _mode_ins:
                    _c.change(_tk_mode, _mode_ins, _mode_outs)

                def _run_go(tin, tin_rg, tout, tout_rg, ds, name, mode, picks,
                            how, max_n, eps, backend, cfg, emb, plots, c_ep,
                            c_fr, c_cap, sets, batch, ro, with_clips=False):
                    # 路径框先解析(2026-08-20 融合):对上配置桶 → 挂载直读
                    # (与之前的下拉白名单同一条安全边界);合法 tos:// 陌生桶
                    # → 直连;本地自由路径在 resolve_root_input 里被一句话拒
                    try:
                        _spec = runner.resolve_root_input(tin, _buckets)
                        _ospec = runner.resolve_output_input(tout, _deliv_root)
                    except ValueError as e:
                        return _tk_view(f"⚠️ {e}")
                    _root = _spec.get("path")            # mount 才有;tos 为 None
                    if _ospec["kind"] == "tos":
                        # 失焦那次探针可能被绕过(粘贴完直接点开始):开跑前再探一次,
                        # 绝不让任务起来后才在上传那步失败
                        _ok, _why = runner.writable_verdict(
                            _ospec["url"], str(tout_rg or "").strip() or None,
                            locate=runner.region_of_bucket)
                        if not _ok:
                            return _tk_view(f"⚠️ 交付目录写不进去:{_why}")
                    if _ospec["kind"] == "mount":
                        _why = runner.mount_region_mismatch(
                            os.environ.get("TOS_BUCKET", "").strip() or "本实例",
                            runner.default_tos_region(), str(tout_rg or "").strip() or None)
                        if _why:
                            return _tk_view(f"⚠️ 交付目录的地区选错了:{_why}")
                    if _spec["kind"] == "mount":
                        _b = _spec["bucket"]
                        _why = runner.mount_region_mismatch(
                            str(_b.get("bucket") or _b.get("name") or ""),
                            runner.mounted_bucket_region(_b), str(tin_rg or "").strip() or None)
                        if _why:
                            return _tk_view(f"⚠️ 数据集目录的地区选错了:{_why}")
                    if _spec["kind"] == "tos" and not public_catalog.is_public_root(_spec["url"]):
                        # 读侧同样开跑前再探一次:桶名/地区对不上,任务不该起来才在列清单那步炸
                        _ok, _why = runner.readable_verdict(
                            _spec["url"], str(tin_rg or "").strip() or None,
                            locate=runner.region_of_bucket)
                        if not _ok:
                            return _tk_view(f"⚠️ 数据集目录读不到:{_why}")
                    if str(backend or '').endswith(BACKEND_BAD):
                        return _tk_view('⚠️ 选中的模型服务当前不可用(原因见选项后的括号),换一个,或把它修好后切回本页自动重检')
                    # 多选下拉默认一个都没选 → 必须先拦(否则空选会一路走到
                    # resolve_under(root, "") = 拿整个数据集根当一份数据跑)
                    _no_ds = runner.dataset_selection_error(ds, bool(batch))
                    if _no_ds:
                        return _tk_view(f"⚠️ {_no_ds}")
                    chosen_pre = runner.picked_datasets(ds)
                    _tos_all = False
                    if _spec["kind"] == "tos" and batch:
                        # 直连桶的「跑全部」(2026-08-21):CLI --batch 不收 tos://,
                        # 这里把前缀下的数据集列出来,按多选的作业表逐个跑 ——
                        # 落盘形状与挂载的 --batch 一致(交付名当父目录)
                        try:
                            chosen_pre = runner.tos_list_datasets(
                                _spec["url"], str(tin_rg or "").strip() or None)
                        except Exception as e:  # noqa: BLE001
                            return _tk_view(f"⚠️ 列不出该前缀下的数据集:"
                                            f"{type(e).__name__}: {str(e)[:120]}")
                        if not chosen_pre:
                            return _tk_view("⚠️ 该前缀下没有列出任何数据集")
                        _tos_all, batch = True, False
                    # 交付名撞上老布局交付:点按钮之前就判得出来,绝不让任务起来
                    # 再以「未完成(退出码 3)」收场逼用户翻日志(2026-08-14 实见);
                    # 消息说「交付名」不说 --output,那是 CLI 的话。
                    # 只有写到本实例交付根才判得了(陌生桶远端撞名靠时间戳批次
                    # 天然隔开,不冒充能判)
                    if _ospec["kind"] == "mount":
                        _bad_name = runner.delivery_name_error(
                            _deliv_root, name or "", ds, bool(batch))
                        if _bad_name:
                            return _tk_view(f"⚠️ {_bad_name}")
                    elif not runner.safe_name(name or ""):
                        return _tk_view("⚠️ 交付名只收字母数字与 . _ -,"
                                        "它要当目录名用")
                    only = skip = None
                    if mode == CUSTOM_SCAN and picks:
                        joined = ",".join(picks)
                        only, skip = ((joined, None) if how == "只跑选中"
                                      else (None, joined))
                    chosen = chosen_pre
                    # 跑批目录名 = 这次任务编号的时间戳部分:结果目录与任务/日志天然
                    # 对得上号("哪次跑批产生了这份结果"不必再翻日志)。多数据集时几份
                    # 交付共用同一个名字,一次点击的产物在各自交付里也对得上。
                    run_id = runner.new_run_id(_runs_root, "run")
                    common = dict(lite=mode == QUICK_SCAN, only=only, skip=skip,
                                  max_episodes=int(max_n) if max_n else None,
                                  episodes=eps or None,
                                  vlm_backend=_backend_code(backend),
                                  embodiment_id=emb or None,
                                  run_name=run_name_of_run_id(run_id),
                                  report_only=bool(ro),
                                  set_overrides=_sets(plots, c_ep, c_fr, c_cap, sets))
                    # 输出侧的两条路(2026-08-20):挂载 → resolve_under 老路;
                    # 陌生桶 → `tos://…/<交付名>` 交给 CLI stage_out,并把地区
                    # 一起传(时间戳批次子目录由管道自己建,两条路布局一致)
                    if _ospec["kind"] == "tos":
                        common["output_region"] = (str(tout_rg or "").strip()
                                                   or None)
                    if _spec["kind"] == "tos":
                        common["input_region"] = (str(tin_rg or "").strip()
                                                  or None)
                    try:
                        cfg = runner.resolve_tos_path(cfg) if str(cfg or "").strip() else None
                        # 勾了「跑全部」就忽略下拉的选择(既有行为,别改坏);其余
                        # 情况下选了几个跑几个,选一个 = 一直以来的那条路径。
                        # 多选顺序跑(jobs 装配):两侧各自可以是挂载或桶
                        # (2026-08-21 读端会说 tos:// 之后,作业表两侧都收 URL);
                        # 切片只在挂载输入上串(片段站按本地数据集名建目录)
                        if (not batch and len(chosen) > 1) or _tos_all:
                            # 答了「一起生成」就给**每个需要的**数据集都串上切片:
                            # 追问只问一次,覆盖的是全部选中项(2026-08-14 用户定)
                            clips = (runner.datasets_needing_clips(
                                _root or _spec["url"], chosen) if with_clips else [])
                            jobs = runner.build_dataset_jobs(
                                _root or _spec["url"],
                                _deliv_root if _ospec["kind"] == "mount" else _ospec["url"],
                                chosen, name or "",
                                clips_root=review_dir, clips_for=clips,
                                config=cfg, **common)
                            return _tk_start(
                                "run", f"质检 {len(jobs)} 个数据集 → {name}"
                                + (f"(含 {len(clips)} 份视频片段)" if clips else ""),
                                jobs=jobs, run_id=run_id)
                        if _spec["kind"] == "tos":
                            inp = (_spec["url"].rstrip("/") + "/"
                                   + (chosen[0] if chosen else ""))
                        else:
                            inp = (_root if batch else
                                   runner.resolve_under(_root,
                                                        chosen[0] if chosen else ""))
                        out = (_ospec["url"].rstrip("/") + "/" + (name or "")
                               if _ospec["kind"] == "tos" else
                               runner.resolve_under(_deliv_root, name or ""))
                        then_argv = None
                        if with_clips and review_dir and not batch:
                            # 切片作为同一任务的第二步:一条日志、一个结果,用户不必
                            # 知道我们内部跑了两条命令。直连输入同样可切
                            # (2026-08-21:审片站读桶里的数据集与读挂载一样)
                            then_argv = runner.build_argv(
                                "review-page", input=inp,
                                output=runner.resolve_under(
                                    review_dir, os.path.basename(out)))
                    except ValueError as e:
                        return _tk_view(f"⚠️ {e}")
                    return _tk_start(
                        "run",
                        f"质检 {os.path.basename(inp)} → "
                        f"{os.path.basename(out.rstrip('/'))}"
                        + ("(含视频片段)" if then_argv else ""),
                        then_argv=then_argv, run_id=run_id,
                        input=inp, output=out, config=cfg, batch=bool(batch),
                        **common)

                rn_args = gr.State({})          # 预检时把这次的参数存下,答完照原样开跑

                def _run_preflight(tin, tin_rg, tout, tout_rg, ds, name,
                                   mode, picks, how, max_n, eps, backend, cfg,
                                   emb, plots, c_ep, c_fr, c_cap, sets, batch,
                                   ro):
                    """开跑前先看数据格式:v3/rrd 要先切片才有画面可看,问一句再决定。

                    只在**真需要**时才问(格式认得出、且本实例配了片段目录),其余一律
                    直接开跑 —— 不拿一个可有可无的对话框挡在客户面前。

                    多选也问(2026-08-14 用户定):此前多选直接跳过不问,于是多选跑出来
                    的 v3/rrd 交付在 Episodes 页全是"没有画面",而用户压根没被问过。
                    做法是**问一次、覆盖全部** —— 统计选中项里有几个需要切片,答"一起
                    生成"就给每个需要的都串上,绝不逐个弹窗。
                    """
                    args = dict(tin=tin, tin_rg=tin_rg, tout=tout,
                                tout_rg=tout_rg, ds=ds, name=name, mode=mode,
                                picks=picks, how=how, max_n=max_n, eps=eps,
                                backend=backend, cfg=cfg, emb=emb, plots=plots,
                                c_ep=c_ep, c_fr=c_fr, c_cap=c_cap, sets=sets,
                                batch=batch, ro=ro)
                    try:
                        _spec = runner.resolve_root_input(tin, _buckets)
                    except ValueError as e:
                        return (*_tk_view(f"⚠️ {e}"), args,
                                gr.update(visible=False), "")
                    _root = _spec.get("path")
                    chosen = runner.picked_datasets(ds)
                    # 勾了「跑全部」时下拉本来就被忽略,跑的是根目录下的全部数据集,
                    # 交付目录由 CLI 自己定 —— 那条路径不在本次范围里,维持不问。
                    # 直连输入也问(2026-08-21 读端会说 tos://):切片站读桶里的
                    # 数据集与读挂载一样,不必再等"本地缓存路径"
                    _src_root = _root or _spec.get("url")
                    needing = ([] if batch else
                               runner.datasets_needing_clips(_src_root, chosen))
                    if needing and review_dir:
                        fmt = (runner.dataset_format(
                            runner.under(_src_root, chosen[0]))
                            if len(chosen) == 1 else None)
                        return (*_tk_view(""), args, gr.update(visible=True),
                                runner.clips_prompt(needing, fmt))
                    return (*_run_go(**args), args, gr.update(visible=False), "")

                def _run_after_ask(args, with_clips):
                    """答完追问:选了就把切片作为同一个任务的第二步串上去。"""
                    return (*_run_go(**args, with_clips=bool(with_clips)),
                            gr.update(visible=False), "")

                _ask_outs = _tk_outs + [rn_args, rn_ask, rn_ask_md]
                rn_go.click(_run_preflight,
                            [rn_tin, rn_tin_rg, rn_tout, rn_tout_rg, rn_ds,
                             rn_out, rn_mode, rn_pick, rn_how,
                             rn_max, rn_eps, rn_backend, rn_cfg, rn_emb, rn_plots,
                             rn_c_ep, rn_c_fr, rn_c_cap, rn_set, rn_batch, rn_ro],
                            _ask_outs)
                rn_yes.click(lambda a: _run_after_ask(a, True), rn_args,
                             _tk_outs + [rn_ask, rn_ask_md])
                rn_no.click(lambda a: _run_after_ask(a, False), rn_args,
                            _tk_outs + [rn_ask, rn_ask_md])

                def _do_probe(cur_run, cur_ex):
                    """探活一次 → 两个下拉都缀上可用性(它们指的是同一批服务)。
                    2026-08-21 起没有按钮了:挂在 app.load 与两个页签的 select 上自动跑
                    (60s 缓存,切页不反复出网);不再回提示语,结果就在选项上。

                    ⚠️ 探活**不走任务通道**:`curation backends` 的原始输出里全是内部
                    预设代号(a30-8b 之类),贴进日志就等于把代号摆到客户面前。这里
                    直接调探活函数,只把「人话标签 · 可用/暂不可用」写回下拉。
                    结果不落盘,是当下这一刻的实况 —— 服务起没起随时会变。
                    顺便**重读一遍配置**:新加的服务不必重启 UI 才看得见
                    (重启会杀掉正在跑的批)。判断都在 _reprobe_options 里。
                    """
                    old = dict(_backend_map)
                    _backend_map.clear()
                    _backend_map.update(runner.vlm_backend_labels(config_path))
                    ch, vals, _msg = _reprobe_options(
                        old, _backend_map, _backend_status(), [cur_run, cur_ex])
                    return (gr.update(choices=ch, value=vals[0]),
                            gr.update(choices=ch, value=vals[1]))

                # 两个「检测可用性」按钮(跑质检侧 + 报告页执行裁决侧)的接线在
                # 报告页那侧的组件建出来之后统一挂(见「人工裁决」页底部)——
                # 一次探活要同时刷新两处下拉,另一处不刷就挂着陈旧的可用性撒谎。

                def _hi_rows():
                    return runner.history_rows(runner.list_runs(_runs_root, limit=50))

                def _hi_open(evt):
                    """点历史某一行 → 回看那次任务的日志(行序与 list_runs 一致)。"""
                    runs = runner.list_runs(_runs_root, limit=50)
                    idx = (evt.index[0] if evt and getattr(evt, "index", None)
                           else 0) or 0
                    if idx >= len(runs):
                        return "(这一行对应的任务已不在列表里)", ""
                    r = runs[idx]
                    state = runner.STATE_STYLES.get(r.get("state"), ("",))[0]
                    return (f"**{r.get('label') or r.get('command')}** · {state}",
                            runner.tail_log(_runs_root, r["run_id"]))

                _hi_open.__annotations__ = {"evt": gr.SelectData}
                hi_table.select(_hi_open, None, [hi_pick, hi_log])

                def _tk_stop_click():
                    act = runner.active_run(_runs_root)
                    if not act:
                        return _tk_view("没有正在跑的任务")
                    runner.stop(_runs_root, act["run_id"])
                    return _tk_view("已请求停止。中途停下的结果目录不完整,"
                                    "重跑时请勾选「覆盖同名结果」")

                tk_stop.click(_tk_stop_click, None, _tk_outs)
                # 轮询刷新:2 秒一次,只读两个小文件 + 日志尾部,开销可忽略。
                # 历史表跟着一起刷(任务跑完就该出现在历史里,不该等人点)。
                def _tk_tick(msg):
                    """刷新的那一跳(手点「刷新」与页面脚本的自动刷新走同一条):刷新
                    状态与日志,**当前那句提示原样带回去**,历史表顺手一起刷。

                    2026-08-13 实测:原来这里传的是空串,于是「还没选数据集」这类
                    校验提示活不过两秒就被下一跳抹掉 —— 用户点了按钮什么也没看见,
                    和静默失败没有区别。提示由下一次真正的动作覆盖,不由刷新清。

                    为什么不用 gr.Timer(2026-08-21,issue #57「一直停在运行中」):
                    Timer 在这套部署里不可靠(active 却不跳,报告页 2026-08-13 就实测过),
                    状态条就停在最后一次手动刷新的样子。改成页面脚本 _TASK_POLL_JS
                    每 2 秒点一次「刷新」按钮 —— 走的是按钮那条真事件,页面不可见时
                    不点,任务不在跑时降到 10 秒一次。
                    """
                    return (*_tk_view(msg or ""), _hi_rows())

                # show_progress="hidden"(2026-08-22 用户实见):跑任务时每 2 秒刷一次,Gradio 默认
                # 先把日志框/状态卡打成"加载中"再填结果,一次刷新闪两下,看着像一秒一刷。
                # 关掉加载态,数据照更新、界面不闪。
                tk_refresh.click(_tk_tick, tk_msg, _tk_outs + [hi_table],
                                 show_progress="hidden")
                app.load(lambda: _tk_view(""), None, _tk_outs)
                app.load(_hi_rows, None, hi_table)

                # 深链预填(2026-08-14 rerun 联动;2026-08-17 重做):rerun viewer 的
                # 「Diagnose」按钮带 ?dataset= / ?dataset_url= / ?url= 跳过来,这里把
                # 「跑质检」的数据集根目录+数据集预选上,用户点「开始质检」即可 ——
                # **只预填不自动开跑**(自动开跑 = 刷新一次页面就重复拉起任务)。
                # 裸名字与完整 tos:// URL 都吃;解析/对表/措辞全在 runner.prefill_plan
                # (纯函数,可单测),这里只渲染。三条铁律见那边 docstring,其中最硬
                # 的一条:**桶不认识绝不回落到默认桶里找同名的**(两个桶各有一个同名
                # 数据集时,静默跑错数据是最坏的一类 bug)。键出现了就必有下文
                # (预选成功 or 逐条警告),不许静默无动作。
                # 参数值仍只拿来与配置白名单/list_datasets 扫出的名字**对表**,
                # 不当路径用 —— 「面板不接受任意路径输入」的边界一个字不破。
                def _prefill_from_query(request):
                    qp = getattr(request, "query_params", None) or {}
                    wanted, present = runner.deeplink_values(qp)
                    hold = (gr.update(),) * 6
                    # ?source=public[&dataset=名字]:公共数据集深链(2026-08-21)。
                    # 裸名字补成镜像桶里的完整地址,之后与 tos:// 深链同一条路
                    _src_q = str((qp.get("source") if hasattr(qp, "get") else "")
                                 or "").strip().lower()
                    if _src_q == "public" and public_catalog.configured():
                        present = True
                        wanted = [w if str(w).startswith("tos://")
                                  else public_catalog.dataset_url(w)
                                  for w in wanted if str(w).strip()]
                        if not wanted:
                            wanted = [public_catalog.root_url() + "/"]
                    if not present:
                        return hold
                    # 新契约(rerun PR#29,2026-08-19 起线上在发):
                    # ?dataset=tos://桶/前缀/数据集名&region=地区。region 是
                    # URL 来的不可信输入,deeplink_region 只认地区字符集,
                    # 过不了当没给 —— 它只预选下拉,绝不进请求路径。
                    region, _rg_present = runner.deeplink_region(qp)
                    # 旧契约兼容:endpoint/tos_endpoint 消毒后只进提示文案
                    ep_host, ep_present = runner.deeplink_endpoint(qp)
                    if ep_present and not ep_host:
                        gr.Warning("链接里的端点参数看不懂,已忽略(不影响预选)")
                    urls = [w for w in wanted if str(w).startswith("tos://")]
                    bare = [w for w in wanted if not str(w).startswith("tos://")]
                    if urls:
                        # 末段规则拆「根前缀 + 数据集名」,根填路径框、名预选下拉
                        try:
                            root, _n0 = runner.split_dataset_url(urls[0])
                        except ValueError as e:
                            gr.Warning(f"链接里的数据集地址解析不了:{e}")
                            return hold
                        _root_only = (_src_q == "public" and urls[0].rstrip("/")
                                      == public_catalog.root_url())
                        if _root_only:
                            root, _n0 = public_catalog.root_url(), ""   # 只切来源不预选
                        names = []
                        for u in ([] if _root_only else urls):
                            try:
                                r2, n2 = runner.split_dataset_url(u)
                            except ValueError:
                                gr.Warning(f"链接里的地址解析不了,已忽略:{u}")
                                continue
                            if r2 == root and n2 and n2 not in names:
                                names.append(n2)
                            elif r2 != root:
                                gr.Warning(f"链接带了多个不同前缀,只认第一个"
                                           f"({root});已忽略 {u}")
                        try:
                            spec = runner.resolve_root_input(root, _buckets)
                        except ValueError as e:
                            gr.Warning(f"链接里的数据集地址不可用:{e}")
                            return hold
                        is_pub = public_catalog.is_public_root(root)
                        if is_pub:
                            # 公共镜像桶:按清单过滤、带全名/版本/集数的显示串
                            try:
                                choices = public_catalog.choices()
                                ds_note = ("" if choices else
                                           "镜像里目前没有 LeRobot 格式的数据集")
                            except Exception as e:  # noqa: BLE001
                                choices = []
                                ds_note = f"⚠️ {type(e).__name__}: {str(e)[:120]}"
                            src_note = ""
                            region = public_catalog.region() or region
                        elif spec["kind"] == "mount":
                            choices = runner.list_datasets(spec["path"])
                            src_note = ""
                            ds_note = runner.dataset_root_note(spec["path"])
                            # 旧契约的端点矛盾提示照发(链接的端点 vs 本实例
                            # 配置的端点地域对不上要点名;链接值只进提示,
                            # 绝不进说明行 —— 那行回答"我们从哪儿读")
                            if ep_host:
                                _c = runner._endpoint_conflict_note(
                                    spec["bucket"], ep_host)
                                if _c:
                                    gr.Warning(_c)
                        else:
                            try:
                                choices = runner.tos_list_datasets(root, region)
                                ds_note = ("" if choices else
                                           "该前缀下没有列出任何数据集")
                            except Exception as e:  # noqa: BLE001
                                choices = []
                                ds_note = (f"⚠️ 列不出该前缀下的数据集:"
                                           f"{type(e).__name__}: {str(e)[:120]}")
                            src_note = f"TOS 直连:{root}"
                        # 预选与选项**同一次回调里算**:value 不在 choices 里
                        # 会被 Gradio 静默丢弃,分两跳必踩
                        _vals = [c[1] if isinstance(c, (tuple, list)) else c
                                 for c in choices]
                        sel = [n for n in names if n in _vals]
                        for n in names:
                            if n not in _vals:
                                gr.Warning(f"链接指的数据集「{n}」在该前缀下"
                                           "没找到 —— 前缀或名字可能变了")
                        if sel:
                            gr.Info("已按链接预选:" + ", ".join(sel)
                                    + " —— 点「开始质检」即可")
                        return (gr.update(value=root, interactive=not is_pub),
                                gr.update(choices=choices, value=sel),
                                (gr.update(value=region, interactive=not is_pub)
                                 if region else gr.update(interactive=not is_pub)),
                                src_note, ds_note,
                                gr.update(value=_src_public if is_pub else SRC_PRIVATE))
                    # 旧契约(裸名字):对表预选,行为与 2026-08-17 版一致 ——
                    # **桶不认识绝不回落到默认桶里找同名的**
                    plan = runner.prefill_plan(bare, _buckets,
                                               link_endpoint=ep_host)
                    for note in plan["notices"]:
                        gr.Warning(note)
                    if not plan["datasets"]:
                        return hold
                    gr.Info(plan["info"])
                    _b = next((x for x in _buckets
                               if x["name"] == plan["source"]), None)
                    return ((gr.update(value=runner.bucket_url(_b))
                             if _b else gr.update()),
                            gr.update(choices=plan["choices"],
                                      value=plan["datasets"]),
                            gr.update(),
                            "" if _b else gr.update(),
                            runner.dataset_root_note(_b["datasets_path"])
                            if _b else gr.update(),
                            gr.update(value=SRC_PRIVATE))

                # gr.Request 靠注解注入;`from __future__ import annotations` 下字符串
                # 注解会在 gradio 里被 eval,而 `gr` 只在函数内可见 → 直接挂真对象
                # (同 _hi_open 的手法)。
                _prefill_from_query.__annotations__ = {"request": gr.Request}
                _prefill_evt = app.load(
                    _prefill_from_query, None,
                    [rn_tin, rn_ds, rn_tin_rg, rn_src_note, rn_ds_note, rn_pub])
                # 深链带来的数据集桶 → 交付目录也借它(没自己桶的实例)
                _prefill_evt.then(_borrow_output, _bo_in, _bo_out)
            # 报告页装在**可提前收口**的嵌套栈里:它的内容有六百行,不可能塞进
            # 一个 with 缩进;而「终端」要排在它右边,就必须在它收口之后再建。
            # 交给 shell 托管 ⇒ 中途抛异常也不会漏关。
            report_ctx = shell.enter_context(contextlib.ExitStack())
            report_tab = report_ctx.enter_context(gr.Tab("质检报告", id="report"))
            # ── 交付根直连(2026-08-20 阶段4,纯新增一行;用户拍板:交付在哪个
            #    桶,报告页就在哪看 —— 报告和交付不焊死在本实例的桶上)。默认
            #    预填本实例交付根的 tos:// 写法 → 默认体验与之前逐字节等价
            #    (挂载直读);改填陌生桶 → 列交付/批次,打开时懒镜像到本地缓存。
            #    ⚠️ 红线自查:报告页那套子页签零改动,这行在页签外的选择区。──
            _rp_src = {"region": ""}     # 直连地区,懒镜像与列表共用(闭包态)
            with gr.Row():
                rp_root = gr.Textbox(label="交付目录", scale=4,
                                     value=_out_default,   # 同跑质检页:没桶留空
                                     placeholder="tos://桶名/目录")
                # 地区列 scale=2(2026-08-21 用户实见:scale=1 时「华北2(北京) (cn-beijing)」
                # 被下拉箭头压住末尾);说明列相应让出 1 份,总份数不变
                rp_rg = gr.Dropdown(choices=runner.tos_region_choices(),
                                    value=runner.default_tos_region(),
                                    label="地区", scale=2, min_width=240,
                                    allow_custom_value=True, interactive=True)
                with gr.Column(scale=6, min_width=160):
                    rp_note = gr.Markdown("", elem_classes=["field-note"])
            with gr.Row():
                # 文案一句话就够(2026-08-13 用户:"这种文字根本不应该给客户看")。
                # 「重新加载」按钮已撤:切到本页就重扫一次盘(见下面的 select),
                # 让用户自己点刷新是上个时代的做法。
                picker = gr.Dropdown(choices=delivery_choices(delivery, choices),
                                     value=choices[0], label="交付名",
                                     scale=1, interactive=True, allow_custom_value=True,
                                     info="新跑完的交付会自动出现在这里")
                # 「运行」= 这份交付跑过的哪一次(2026-08-14 布局变更:每次跑批各进
                # 各的时间戳子目录,互不覆盖)。默认选中 latest 记的那次 **只是省一次
                # 点击** —— 它不是"推荐用这份",条目上也只写事实(时间/本次处理条数/
                # 有没有导出数据集),不写"抽查""完整"这种替客户下的判断。
                run_pick = gr.Dropdown(choices=run_choices(choices[0]),
                                       value=resolve_run(choices[0]), label="运行",
                                       scale=1, interactive=True,
                                       info="每次跑批各存一份,默认打开最近一次")
            # 「有裁决尚未应用」提醒(2026-08-16 纯新增):裁决 CSV 跨跑批共用,
            # 跑完新一批忘了执行 rejudge,看到的就全是机器结论而人毫不知情。
            # 无未应用裁决时是空串,Markdown 不占位。
            pending_banner = gr.Markdown()
            state = gr.State()

            # 2026-08-13 重做:上半部那几行 bullet 与下半部的表在说同一批数字
            # (而且「不合格拦截」同名不同义),用户原话"表格没有显示正确的质检
            # 结果信息"。现在只剩一张表 + 表下一行口径小字;顶上的 Markdown 只留
            # 身份行与一句导航,一个数字都不说。表本身不再顶标题——页签已经写着
            # 「质检总览」,再顶一个「漏斗」既重复又是行话。
            with gr.Tab("质检总览"):
                ov_md = gr.Markdown()
                ov_table = gr.Dataframe(headers=OVERVIEW_HEADERS, show_label=False,
                                        interactive=False)
                ov_note = gr.Markdown()

            # ── Episodes 页(2026-08-11 整页改版):顶部三桶 + 左清单右详情。
            #    客户只关心"哪些过了/被拒/待人工",所以三桶就是主导航;骨架照
            #    lerobot visualize_dataset 的左导航右详情,视频是详情的主角。
            #    桶口径与清单文案全在 manifest 的纯函数里,这里只摆组件。
            with gr.Tab("Episodes"):
                # 选项(带计数)由 _load 现算填入:交付一换,计数就得跟着换
                ep_bucket = gr.Radio([], label="", elem_id="ep-buckets",
                                     container=False)
                with gr.Row():
                    with gr.Column(scale=1, min_width=250):
                        ep_pick = gr.Radio([], label="episode", elem_id="ep-list",
                                           interactive=True, container=True)
                        # 分页(2026-08-11):两百行单选框一次渲染就到极限。翻页件
                        # 一页放得下时整排隐藏(与同步曲线页同一手法)
                        with gr.Row():
                            ep_prev = gr.Button("← 上一页", scale=1, visible=False,
                                                size="sm")
                            ep_pos = gr.Markdown("")
                            ep_next = gr.Button("下一页 →", scale=1, visible=False,
                                                size="sm")
                        ep_page = gr.State(0)
                        # 当前正在看的那条:清单翻页后它可能不在本页,右侧详情
                        # 照旧显示它,翻回来仍是选中态
                        ep_sel = gr.State(None)
                    with gr.Column(scale=3):
                        ep_card = gr.HTML()
                        ep_video = gr.HTML()
                        ep_hint = gr.HTML()
                        # 明细默认折叠(2026-08-11 用户定):看片的人九成不需要读数,
                        # 但要读时一点就开——不是删掉,是让路
                        with gr.Accordion("检查明细(逐维读数)", open=False):
                            ep_checks = gr.HTML()
                            ep_sync = gr.HTML()
                            # 同步曲线是超宽长图,给整幅宽度(整页也有专门的曲线页)
                            ep_plot = gr.Image(label="视频-动作同步曲线(右上角可全屏放大)",
                                               visible=False, interactive=False,
                                               buttons=["fullscreen", "download"],
                                               height=380)

            # ── 人工裁决页(2026-08-06):把"人要做决定"的事全收到一处。
            #    位置放在 Episodes 与技能画像之间 = 看完数据紧接着做决定的自然工序。
            #    2026-08-16 重构成两个子页签:「待你裁决」(标注 + 成败按 episode
            #    合并,一条一张卡)与「被拒复议」(性质不同 —— 它不属于质检流水线,
            #    是推翻机器结论的入口,原样搬入,功能一个字不变)。
            with gr.Tab("人工裁决"):
                with gr.Tabs():
                    # ── 待你裁决(2026-08-16 用户定名,别改成行话「待裁决」):
                    #    一条 episode 一张卡,视频看一遍、该答的问题一次答完。
                    #    droid-200-new 实测 7 条同时在两个队列里 —— 分区制让用户
                    #    在两张卡片里各找一次、各看一遍视频,这次改掉。
                    with gr.Tab("待你裁决"):
                        # 「怎么用这一页」流程条已删(2026-08-21 用户点名:UI 要简洁)
                        mg_filter = gr.Radio([], label="筛选(重叠条目在两个单项档里都出现)")
                        mg_hint = gr.Markdown()
                        # 两个队列**并列成一个区块**(2026-08-19 用户拍板,明确
                        # 授权改这一页的布局 —— 与「报告页布局不许动」那条长期
                        # 红线冲突时以本次口头授权为准)。
                        # 为什么并列:它们是**同一件事的两类问题**(标注有分歧 /
                        # 成败弃权),竖着排会让人以为是先后两步,滚到第二张表时
                        # 已经忘了第一张;更糟的是往下滚正好撞上「执行裁决」,
                        # 用户被引导着去点了一个本该最后才点的按钮。
                        # 并列 + 区块头 + 下面执行区换配色 = 一眼能分出"这里是
                        # 记决定"和"那里是落地执行"。
                        gr.HTML(_adj_section_html(
                            "", "待裁决队列", "",     # 副标题已删(2026-08-21 用户点名多余)
                            "#86909C", "#1D2129"))
                        # 单表(2026-08-23 用户拍板):分歧 × 弃权按 episode 合一,
                        # 一条一行;点任意一格跳到下方卡片。列删到只剩能回答
                        # "哪条、什么问题、标注、系统看到什么、裁了没"的五列。
                        qu_table = gr.Dataframe(
                            headers=QUEUE_HEADERS, show_label=False,
                            interactive=False, elem_id="audit-queue",
                            max_height=420,  # 容器内滚动(表头吸顶),条数多不挤爆页面
                            wrap=True,       # 长文本换行显示全文,不截断
                            column_widths=["8%", "11%", "34%", "34%", "13%"])
                        # ── 合并裁决卡片(逐条翻页,翻页按 episode 走不再按队列走):
                        #    看一遍视频 → ① 标注问题(有分歧才出现)→ ② 成败问题
                        #    (显隐档位见 manifest.success_block_mode)。
                        #    UI 只记录裁决(human-decisions/ 三张 CSV);
                        #    执行 = 本页底部的「执行裁决」(确认框是自产自证的断路器)。
                        # 默认展开(2026-08-05 用户定:折叠着没人知道能点开)
                        with gr.Accordion("逐条裁决",
                                          open=True):
                            mg_idx = gr.State(0)
                            with gr.Row():
                                mg_prev = gr.Button("← 上一条", scale=1)
                                mg_pos = gr.Markdown("", elem_id="mg-pos")
                                mg_next = gr.Button("下一条 →", scale=1)
                            mg_info = gr.Markdown()
                            # 「同时播放」按钮**单独占一行**,不进视频那一行:塞进去会把
                            # 三个播放器挤窄(用户 2026-08-14:"视频 window 大小别变")。
                            # 按钮靠 elem_id 找视频,不需要把两者包进同一个容器 ——
                            # 少一层容器 = 视频区的宽度与间距一个像素都不动。
                            gr.HTML(play_all_button_html("三路机位从头一起播,播完即停(不循环)",
                                                         zone="mg-vids"))
                            with gr.Row(elem_id="mg-vids"):
                                mg_vids = [gr.Video(label=f"机位 {i+1}", interactive=False,
                                                    autoplay=False, loop=False, scale=1)
                                           for i in range(3)]
                            # ①② **左右并排**(2026-08-19 用户拍板):它们是同一条
                            # episode 的两类问题,上下排会被当成先后两步;更糟的是
                            # 往下滚正好撞上「执行裁决」,人被引导着点了本该最后
                            # 才点的按钮。并排之后两块结构也对称了 —— 各自两个结论
                            # 加一个「拿不准」,同样位置的按钮语义一样。
                            with gr.Row(elem_id="adj-blocks"):
                             # ① 标注问题:该条在分歧队列里才渲染
                             with gr.Column(visible=False) as mg_au_block:
                                 gr.HTML(_adj_section_html("1", "标注问题",
                                                           "数据自带的标注 vs 系统看画面写的描述",
                                                           "#FF7D00", "#D25F00"))
                                 au_info = gr.Markdown()
                                 au_origlab = gr.Textbox(label="原始标注(只读)", interactive=False)
                                 au_newlab = gr.Textbox(label="修正后标注(可编辑;预填 VLM 建议描述,"
                                                              "仅「采纳改标」使用)")
                                 au_note = gr.Textbox(label="备注(可选)")
                                 with gr.Row():
                                     # 三键同色待选、选中变色(见 _au_btns);语义靠图标与文字。
                                     # 2026-08-19 结构对齐成败块:两个结论 + 一个「拿不准」。
                                     # 「弃用该条」提出去做卡片级操作(见下面 mg_kill)——
                                     # 用户点破的逻辑错:"这条数据要不要"和"标注 vs caption
                                     # 谁错"是两个正交的维度,弃用根本不是这个问题的第三个答案。
                                     au_adopt = gr.Button("✅ 采纳改标", variant="secondary")
                                     au_keep = gr.Button("↩️ 维持原标注", variant="secondary")
                                     au_hold = gr.Button("🤔 拿不准", variant="secondary")
                                 au_status = gr.Markdown()
                             # ② 成败问题:机器弃权时必答;「采纳改标」后可选(留空=
                             #    交给机器重判);「弃用该条」时不可用(矛盾拦截)——
                             #    档位判定在 manifest.success_block_mode,这里只渲染。
                             with gr.Column(visible=False) as mg_tv_block:
                                 gr.HTML(_adj_section_html("2", "成败问题",
                                                           "这条任务到底完成了没有",
                                                           "#165DFF", "#165DFF"))
                                 tv_mode_note = gr.Markdown()
                                 tv_info = gr.Markdown()
                                 tv_readings = gr.Markdown()
                                 tv_note = gr.Textbox(label="备注(可选;写清依据,复盘时是唯一线索)")
                                 with gr.Row():
                                     # 顺序与 VERDICT_CHOICES 严格对应(按序点亮)
                                     tv_pass = gr.Button("✅ 判成功", variant="secondary")
                                     tv_fail = gr.Button("❌ 判失败", variant="secondary")
                                     tv_hold = gr.Button("🤔 拿不准", variant="secondary")
                                 tv_status = gr.Markdown()

                            # 卡片级「整条弃用」(2026-08-19 用户拍板):它不隶属于
                            # 上面任何一个问题 —— "这条数据要不要"和"标注 vs
                            # caption 谁错"是两个正交维度(用户点破我的逻辑错)。
                            # 措辞用「其它原因」而不是「看不清」:弃用的原因不止一种
                            # (录漏、机械臂撞了、任务根本没做完),而**弃用原因会写进
                            # reject.json 和报告**,是"这条为什么被扔掉"的证据 ——
                            # 把某一个原因焊死在按钮上,记录下来的证据就会说谎。
                            # 🔴 语义:点了它,**不管上面两块点了什么,这条都弃用**
                            # (优先级拦截在 rejudge.run_rejudge,见那处注释)。
                            with gr.Row(elem_id="adj-kill"):
                                mg_kill = gr.Button("其它原因-整条弃用",
                                                    variant="secondary", scale=0)
                            mg_kill_status = gr.Markdown()

                    # ── 被拒复议(2026-08-11;2026-08-16 原样搬进子页签):任务成败
                    #    判定**杀掉**的条目在这里可看、可捞回 —— 判定从"拿不准就转
                    #    人工"升级成"证据够就杀"之后,这一区就是保险丝。整区在没有
                    #    可复议条目时**不渲染**(visible=False),但子页签本身藏不掉,
                    #    空态时由 ap_empty 顶一句说明,免得像页面坏了。
                    #    物理与结构硬门拒掉的条目进不来(准入判据在 decisions.py)。
                    # 页签名自带范围(2026-08-16 用户定):叫「任务失败复议」,
                    # "只管任务成败判定拒掉的"这层意思由名字承担,正文不再解释一遍。
                    # 那句解释没删掉,搬进下面**默认收起**的折叠条 —— 用户找不到自己
                    # 那条被拒数据时,答案还在一次点击之外,而平时不占版面。
                    with gr.Tab("任务失败复议"):
                        ap_empty = gr.Markdown("_本次没有判为任务失败的条目。_",
                                               visible=False)
                        with gr.Column(visible=False) as ap_block:
                            # 文案 2026-08-16 用户逐字给定;只有末句的**时机**由我
                            # 改准:用户原文是"其将自动重新加入交付",而这一页的按钮
                            # 全是**记草稿**,要点了「执行裁决」才真的生效(整页顶上
                            # 就写着"记草稿,可随时改")。写成"自动"会让人以为点完就
                            # 完事 —— 那正是我们刚加"有裁决尚未应用"提醒要防的事。
                            gr.HTML(_adj_section_html("", "任务失败复议",
                                                      "系统判为任务执行失败的条目。本页为可选复核:"
                                                      "不处理不影响交付结果。但如认为某条判定有误,"
                                                      "可在此将该条恢复为可用 —— 执行裁决后,"
                                                      "它会自动重新加入交付(含交付数据集)。",
                                                      "#CB272D", "#CB272D"))
                            with gr.Accordion("为什么有的被拒条目不在这里?", open=False):
                                gr.Markdown(
                                    "只有「任务成败判定」拒掉的条目会出现在这一页。"
                                    "时间戳、残段、运动学、视频动作同步这类**测量得出**的"
                                    "问题是终局判定,不进入复议。")
                            ap_hint = gr.Markdown()
                            ap_table = gr.Dataframe(headers=APPEAL_HEADERS,
                                                    label="被拒条目(点任意一行 → "
                                                          "下方复议卡片跳到该条)",
                                                    interactive=False, elem_id="appeal-queue",
                                                    max_height=420, wrap=True,
                                                    column_widths=["8%", "12%", "45%",
                                                                   "22%", "13%"])
                            with gr.Accordion("复议被拒条目(记草稿,可随时改)", open=True):
                                ap_idx = gr.State(0)
                                with gr.Row():
                                    ap_prev = gr.Button("← 上一条", scale=1)
                                    ap_pos = gr.Markdown("", elem_id="ap-pos")
                                    ap_next = gr.Button("下一条 →", scale=1)
                                ap_info = gr.Markdown()
                                ap_readings = gr.Markdown()
                                # 视频走 Episodes 页那条来源链(审片站 → 交付数据集 →
                                # 提示语),不另起一套:被拒条目往往没有裁决片段,
                                # 只有那条链找得到画面。
                                ap_video = gr.HTML()
                                ap_note = gr.Textbox(label="备注(可选;写清依据,复盘时是唯一线索)")
                                with gr.Row():
                                    # 顺序与 APPEAL_CHOICES 严格对应(_ap_btns 按序点亮)
                                    ap_keep = gr.Button("❌ 维持拒绝", variant="secondary")
                                    # ⚠️ 只改按钮上的字。落进 reject_appeals.csv 的值
                                    # 仍是 "捞回" —— 那是数据契约(老交付的裁决记录、
                                    # rejudge 的匹配、幂等判据都认它),改了会静默失配。
                                    ap_back = gr.Button("🛟 恢复为可用", variant="secondary")
                                ap_status = gr.Markdown()

                # ── 执行裁决(2026-08-19 定案):执行入口搬到
                #    证据旁边,任务台那个「执行人工裁决」页签整个删掉。作用对象
                #    = 本报告页**当前加载**的那份交付与那一次运行(state 里的 m),
                #    不再让用户到别处把系统已知的上下文再选一遍。
                #    位置在两个子页签之下 = 两条裁决线共用同一个执行出口。
                #    ⚠️ 红线:上面「待你裁决」「任务失败复议」的功能与布局一个字
                #    不动,本区是纯新增。──
                # 2026-08-19 用户点名:信息行(执行对象/原始数据集/裁决计数)
                # **都是废话** —— 你就在这份交付的页面上,再说一遍是噪音;而
                # 删完之后这里只剩一个按钮,**一个按钮不需要单独成块**,所以那个
                # 橙色分节头也一起去掉。
                # ex_info 组件保留但恒为空串(它在 _load 的输出槽里;删组件要同步
                # 改 outs,而输出槽对不齐会让八条测试一起红 —— 今天刚踩过)。
                # 空串的 gr.Markdown 在前端渲染零高度,不占版面。
                ex_info = gr.Markdown()
                # 源数据集兜底:交付没记「源数据集路径」(2026-08-13 之前的老交付)
                # 时才露面让用户选,绝不按名字猜(同名不同库会重判错数据);记了就
                # 一个字都不问。显隐/说明的做法与跑质检侧同款(说明不走 info=,
                # elem_id 沿用 rj-* —— 它们仍是 rejudge 专属的说明行)。
                ex_src_dd = gr.Dropdown(choices=_bkt_choices, value=_bkt_ids[0],
                                        label="数据集根目录", visible=False,
                                        interactive=True)
                ex_src_note = gr.Markdown(runner.bucket_info_line(_buckets[0]),
                                          line_breaks=True, visible=False,
                                          elem_id="rj-src-note",
                                          elem_classes=["field-note"])
                ex_ds = gr.Dropdown(choices=runner.list_datasets(_data_root),
                                    label="原始数据集", visible=False,
                                    interactive=True)
                ex_ds_note = gr.Markdown(runner.dataset_root_note(_data_root),
                                         visible=False, elem_id="rj-ds-note",
                                         elem_classes=["field-note"])
                # 「默认值就好,不要求用户动」(用户拍板)—— 模型服务与配置文件
                # 收进折叠区,留空就用生效配置里的值。
                with gr.Accordion("更多设置", open=False):
                    with gr.Row():
                        ex_backend = gr.Dropdown(choices=_backends, label="模型服务", allow_custom_value=True)
                        pass   # 「检测可用性」按钮已撤(自动探活,见 _do_probe)
                    ex_cfg = gr.Textbox(label="配置文件(留空=默认)",
                                        placeholder=f"{runner.TOS_ROOT}/…/site.yaml")
                with gr.Row():
                    ex_go = gr.Button("执行裁决", variant="primary", scale=0)
                ex_msg = gr.Markdown()
                # 确认框:**真模态对话框**(2026-08-19 用户点名 ——「我说过要跳出来
                # 成一个对话框,你怎么跳出来成了一个平铺框?」)。
                # Gradio 没有原生模态框,但"模态"这件事不需要它原生支持:一个
                # position:fixed 居中的块 + 一层遮罩就是模态。做法全在 CSS 的
                # `.modal-dialog`(见 _ARCO_CSS),Python 这边只挂一个 class ——
                # 组件树、事件接线、显隐逻辑一个字不用变。
                # 为什么必须是模态而不是平铺:这个动作**会改写交付**。平铺框出现在
                # 页面下方,用户很可能没滚到、或者没意识到那是在问他;模态把页面
                # 其余部分挡住,答一句才能继续 —— 这正是"当面问一句"的意思。
                # (它替换掉旧页签那个「我确认:这会改写该交付的内容」勾选框:
                #  勾选框太容易顺手勾过去。)
                # ⚠️ 容器必须是 Column 不能是 Group(2026-08-19 真 DOM 实测):
                # gradio 6.9 渲染 gr.Group 时把 elem_id/elem_classes **整个丢掉**
                # (页面上其它组件的 elem_id 全落地,唯独 .gr-group 的 id/class 是空),
                # modal-dialog 挂上去等于没挂,用户看到的仍是平铺框。
                # 组件树里 class 挂没挂,单测查得到;前端渲染丢没丢,只有浏览器里
                # 查 DOM 才看得见 —— 同族教训见 delivery_target_choices 那单。
                with gr.Column(visible=False, elem_id="ex-ask",
                               elem_classes=["modal-dialog"]) as ex_ask:
                    ex_ask_md = gr.Markdown()
                    # 按钮行居中(2026-08-19 用户点名"两个按钮没居中"):scale=0 只管
                    # 不抢伸缩,Row 默认 flex-start + 按钮自带 min-width 仍是左对齐
                    # 大宽条 —— 居中与定宽在 CSS #ex-ask-btns(见 _ARCO_CSS)。
                    with gr.Row(elem_id="ex-ask-btns"):
                        ex_yes = gr.Button("确定", variant="primary", scale=0,
                                           elem_id="ex-yes")
                        ex_no = gr.Button("取消", scale=0)

            with gr.Tab("技能分布"):
                # 图在表上方(2026-07-30):先看分布(谁多谁少、长尾有多长),
                # 再下去查具体判据。表格原样不动。
                sk_html = gr.HTML()
                sk_table = gr.Dataframe(headers=SKILL_HEADERS, label="两级技能体系",
                                        interactive=False)
                # 分歧队列与裁决卡片 2026-08-06 整体搬去「人工裁决」页,这里只留指路:
                # 画像页是"看数据"的,裁决是"做决定"的,混在一页两边都做不好。
                sk_audit_note = gr.Markdown()

            # ── 同步曲线页(2026-08-07 新增):details/plots/<ep>_sync.png 的画廊。
            #    与「Stuck 时间线」相邻 = 两页都是图形化诊断(一个看时间轴、一个看
            #    相关曲线),放一起找得到。分页而不是懒加载:曲线是宽长图,一屏 24 张
            #    已经够翻,且分页零 JS(本 UI 一贯做法)。
            # 明细页(2026-08-13 用户定):顶层只留五个,诊断与排障类收进这里当子页,
            # 报告首屏不再一次糊上来九个页签。子页的内容与回调**逐字未动**,
            # 只是多包了一层 gr.Tabs()。
            with gr.Tab("明细"):
                with gr.Tabs():
                    with gr.Tab("动作打分明细"):
                        dt_pick = gr.Dropdown(label="选择明细表(本次跑批 details/ 下的 CSV)",
                                              interactive=True)
                        dt_note = gr.Markdown()
                        dt_table = gr.Dataframe(label="明细", interactive=False,
                                                wrap=False, max_height=560)

                    with gr.Tab("视频打分明细"):
                        # 单独成页(2026-08-13 用户):逐相机的视觉质量原先混在上一页
                        # 那个下拉里,客户根本翻不到 —— 而"每台相机拍得清不清楚"是
                        # 判断这份数据能不能用的直接证据。下拉里那一条同时撤掉:
                        # 同一份数据不留两个入口(见 manifest.VIDEO_DETAIL_TABLE)。
                        vd_note = gr.Markdown()
                        vd_table = gr.Dataframe(label="明细", interactive=False,
                                                wrap=False, max_height=560)

                    with gr.Tab("视频-动作同步"):
                        # 结论先行(2026-08-07 用户点名:光有图没有提示)——建议原先埋在
                        # 整页曲线之后,滚不到等于没有。现在顶部先说"这份数据同步得怎么样、
                        # 该怎么办",曲线退居证据位。
                        sy_conclusion = gr.HTML()
                        # 全库逐相机概览紧跟结论(2026-08-07 用户:"建议放到页面开头")——
                        # 它是结论的量化底稿,原先压在整页曲线之后,谁也滚不到。
                        sy_health = gr.HTML()
                        with gr.Accordion("怎么看这些图(展开)", open=False):
                            gr.Markdown(SYNC_HOWTO)
                        sy_filter = gr.Radio(SYNC_FILTERS, value=SYNC_FILTER_ALL,
                                             label="筛选")
                        with gr.Row():
                            # 平铺优先:一页放得下就整排隐藏翻页件(2026-08-07 用户定),
                            # 只有图多到一页塞不下时才露出来兜底
                            sy_prev = gr.Button("← 上一页", scale=1, visible=False)
                            sy_pos = gr.Markdown("")
                            sy_next = gr.Button("下一页 →", scale=1, visible=False)
                        sy_note = gr.Markdown()
                        # 一张一行、整幅宽度(不用 Gallery:宽幅长图塞进方格必然上下留白,
                        # 中间两张图周围全是空——2026-08-07 用户实见)。固定槽位 + visible
                        # 开关,数量随分页变化;每张自带全屏/下载按钮,点开即原尺寸。
                        sy_cards = gr.HTML()
                        sy_page = gr.State(0)

                    with gr.Tab("卡顿动作时间线"):
                        # 筛选与排序(2026-08-13 用户定):默认「stuck + idle」+「episode
                        # 序号」—— 先按录制顺序看,与原始数据条目对得上;要当复查队列
                        # 用再切「卡顿时长」把最该看的顶上来。说明文字用户点名删了。
                        with gr.Row():
                            tl_show = gr.Radio(list(TL_FILTERS.values()),
                                               value=TL_FILTERS["both"], label="显示")
                            tl_sort = gr.Radio(list(TL_SORTS.values()),
                                               value=TL_SORTS["episode"], label="排序")
                        tl_note = gr.Markdown()
                        tl_html = gr.HTML()

                    # 从质检总览底部搬过来的(2026-08-13):总览要收敛成一张表,
                    # 而这份快照是"日后复核这份报告按什么标准出的"的底稿,属于明细。
                    # 页签名不写文件里的键名 config_effective —— 那是我们的字段名。
                    with gr.Tab("本次运行配置"):
                        gr.Markdown(
                            "这次跑批**实际生效**的全部设置(出厂默认 + 站点配置 + "
                            "本次界面选项合并之后的结果)。只读,用于日后复核这份"
                            "报告是按什么标准出的。")
                        ov_cfg = gr.Code(language="yaml")

            # 性能剖析(2026-08-14 用户点名提回顶层):它回答的是"这批为什么慢、
            # 用的什么服务",是**跑批本身的账**,不是某一维的明细 —— 收进「明细」
            # 之后客户找不到它。位置钉在「明细」右边。
            with gr.Tab("性能剖析"):
                # 三块(2026-07-30):① 这次用的什么服务/什么硬件 ② 管线自己跑在
                # 什么容器里 ③ 时间花在哪一类 VLM 调用上。数据全部来自交付记录,
                # **界面不出现任何后端预设代号**(那是机房黑话,见 manifest 顶部红线)。
                perf_backend = gr.Markdown()
                perf_env = gr.Markdown()
                gr.Markdown("### 延时剖析")
                perf_note = gr.Markdown()
                perf_table = gr.Dataframe(headers=LATENCY_HEADERS, label="分类延时",
                                          interactive=False)
                # 两段常量说明(与交付无关,不进 _load 的输出列表):分位数怎么读、
                # 四类调用各是干什么的。第二轮反馈:光有语义化名字客户仍读不懂。
                gr.Markdown(LATENCY_PCTL_NOTE)
                gr.Markdown(LATENCY_KIND_NOTE)
                perf_bar = gr.HTML()

            # ⚠️ 顺序必须与 _load 的返回值逐项对齐(错位是运行期才炸的接线错误,
            #    有测试直接比 len(_load(...)) == len(outputs) 钉住)。
            # Episodes 详情的六个槽(判决卡 / 视频区 / 待人工指路 / 检查表 /
            # 逐相机同步 / 同步曲线):_detail 与 _ep_bucket_change 都按这个顺序装配。
            _ep_outs = [ep_card, ep_video, ep_hint, ep_checks, ep_sync, ep_plot]
            # 左清单的六个槽(页码 / 选中项 / 单选框 / 页码文字 / 两个翻页键):
            # _ep_list 按这个顺序装配
            _ep_list_outs = [ep_page, ep_sel, ep_pick, ep_pos, ep_prev, ep_next]
            _sy_outs = [sy_page, sy_note, sy_pos, sy_prev, sy_next, sy_cards]
            # 复议卡片的七个槽(_ap_render 按这个顺序装配;两个按钮的顺序 =
            # APPEAL_CHOICES 的顺序)
            _ap_outs = [ap_idx, ap_pos, ap_info, ap_readings, ap_video,
                        ap_keep, ap_back]
            # 合并裁决卡片的 22 个槽(_mg_render 按这个顺序装配):卡头三件 +
            # 三路视频 + ① 区五件三键 + ② 区五件三键
            _mg_outs = [mg_idx, mg_pos, mg_info, *mg_vids,
                        mg_au_block, au_info, au_origlab, au_newlab, au_note,
                        au_adopt, au_keep, au_hold,
                        mg_tv_block, tv_mode_note, tv_info, tv_readings, tv_note,
                        tv_pass, tv_fail, tv_hold]

            outs = [state, ov_md, ov_table, ov_note, ov_cfg, ep_bucket, *_ep_list_outs,
                    sk_html, sk_table, sk_audit_note,
                    mg_filter, mg_hint, qu_table,
                    *_mg_outs,
                    ap_block, ap_empty, ap_hint, ap_table, *_ap_outs,
                    *_ep_outs, dt_pick, dt_note, dt_table, vd_note, vd_table,
                    sy_filter, *_sy_outs, sy_conclusion, sy_health,
                    tl_show, tl_sort, tl_note, tl_html,
                    perf_backend, perf_env, perf_note, perf_table, perf_bar,
                    pending_banner,
                    ex_info, ex_ds, ex_src_dd, ex_src_note, ex_ds_note]

            def _pick_delivery(path):
                """换交付 → 重列该交付的历次跑批 + 打开其中一次。

                默认打开 latest 记的那次(见 resolve_run):只是省一次点击。老布局的
                交付(三件套直接在交付目录里)在这里只会得到一项,照常打得开 —— 这条
                是 2026-08-14 改布局时的硬要求,别人的旧交付不许因为我们改了目录形状
                就打不开。
                """
                if str(path or "").startswith("tos://"):
                    # 直连交付(2026-08-20 阶段4):批次列表走 tos 一层列,
                    # 值 = 完整 tos:// URL,_load 收到后自己懒镜像
                    try:
                        rc = runner.tos_run_choices(
                            str(path), _rp_src["region"] or None)
                    except Exception as e:  # noqa: BLE001
                        gr.Warning(f"列不出该交付下的批次:"
                                   f"{type(e).__name__}: {str(e)[:120]}")
                        rc = []
                    sel = rc[0][1] if rc else ""
                    return (gr.update(choices=rc, value=sel or None),
                            *_load(sel))
                d = resolve_delivery(path, discover_deliveries(delivery))
                rc = run_choices(d)
                sel = resolve_run(d)
                if rc and sel not in [v for _lab, v in rc]:
                    sel = rc[0][1]
                return (gr.update(choices=rc, value=sel), *_load(sel))

            # 用 .input 不用 .change(2026-08-12 二次踩的一课):回填下拉值会经
            # .change 级联再跑一次 _load,与另一批更新并发改同一排组件,翻页按钮的
            # visible 补丁偶尔被冲掉。.input 只认用户动作,单写者,竞态整类消失。
            picker.input(_pick_delivery, picker, [run_pick, *outs])
            run_pick.input(_load, run_pick, outs)

            def _rp_root_changed(url, region):
                """交付根失焦/回车/切地区 → (交付下拉, 说明)。

                本实例交付根(挂载)→ 现有扫描,行为与之前逐字节等价;陌生桶 →
                tos 一层列交付。**只换选项不自动加载**(与 _picker_tick 同一条
                纪律:内容重载会与用户当下的点击并发打架),用户点中哪份再镜像。
                """
                _rp_src["region"] = str(region or "").strip()
                s = str(url or "").strip().rstrip("/")
                home = runner.home_output_url(_deliv_root).rstrip("/")
                # 默认地址只在**挂载承载**时才等于本地交付根;没挂载的实例
                # (2026-08-20)默认地址是桶里的直连前缀,得真去桶里列
                if not s or s == str(_deliv_root).rstrip("/") \
                        or (s == home and runner.is_mount_backed(_deliv_root)):
                    fresh = discover_deliveries(delivery)
                    return (gr.update(choices=delivery_choices(delivery, fresh),
                                      value=None), "")
                if not s.startswith("tos://"):
                    return (gr.update(), "⚠️ 只认 tos://桶/前缀 形式的地址"
                            "(或本实例的交付根)")
                err = runner.tos_url_error(s, "交付目录")
                if err:
                    return gr.update(), f"⚠️ {err}"
                try:
                    names = runner.tos_list_deliveries(
                        s, _rp_src["region"] or None)
                except Exception as e:  # noqa: BLE001 网络/SDK 异常族杂
                    return (gr.update(), f"⚠️ 列不出该前缀下的交付:"
                            f"{type(e).__name__}: {str(e)[:120]}")
                if not names:
                    return (gr.update(choices=[], value=None),
                            "该前缀下没有列出任何交付(前缀打错?或桶里确实是空的)")
                return (gr.update(choices=[(n, s + "/" + n) for n in names],
                                  value=None),
                        f"TOS 直连:{s}(打开一份交付时把报告与明细镜像到本地,"
                        "数据集本体不下载)")

            if not runner.is_mount_backed(_deliv_root) \
                    and runner.home_output_url(_deliv_root).startswith("tos://"):
                # 没挂载的实例:交付默认在桶里,开门先把桶里的交付列出来
                app.load(_rp_root_changed, [rp_root, rp_rg], [picker, rp_note])
            rp_root.blur(_rp_root_changed, [rp_root, rp_rg], [picker, rp_note])
            rp_root.submit(_rp_root_changed, [rp_root, rp_rg], [picker, rp_note])
            rp_rg.input(_rp_root_changed, [rp_root, rp_rg], [picker, rp_note])

            def _borrow_report_root(tin, tin_rg, cur):
                """没桶的实例收到深链:报告页的交付目录也借数据集所在的桶,并顺手
                列一次交付。报告页是**读**的入口,只读桶照样要能看 —— 这里不探可写
                (2026-08-21 用户定:写的检查只在人工裁决记录那一刻)。"""
                if str(cur or "").strip():
                    return gr.update(), gr.update(), gr.update(), gr.update()
                url = runner.borrowed_output_url(tin, _deliv_root)
                if not url or url == runner.home_output_url(_deliv_root):
                    return gr.update(), gr.update(), gr.update(), gr.update()
                rg = str(tin_rg or "").strip() or None
                pk, note = _rp_root_changed(url, rg)
                return gr.update(value=url), gr.update(value=rg), pk, note

            _prefill_evt.then(_borrow_report_root, [rn_tin, rn_tin_rg, rp_root],
                              [rp_root, rp_rg, picker, rp_note])

            def _rp_is_direct(url) -> bool:
                """交付根框当前指向的是不是直连桶(而非本实例交付根)。"""
                s = str(url or "").strip().rstrip("/")
                if not s.startswith("tos://"):
                    return False
                home = runner.home_output_url(_deliv_root).rstrip("/")
                return not (s == home and runner.is_mount_backed(_deliv_root))

            def _picker_tick(cur, url=None, region=None):
                """重扫交付列表(**只换选项、不重载内容**)。

                只换选项是刻意的:内容重载会与用户当下的点击并发打架 —— 交付下拉的
                联动就为此修过一次(6bb28b5:两批更新改同一排组件,翻页按钮偶尔被
                冲掉)。新交付自动出现在列表里,点它即可查看,不必再点什么"刷新"。
                手输的自定义路径原样保留在选项里,不会被刷掉。

                交付根指向直连桶时(2026-08-20 7862 模拟实例真机抓到):这里必须
                去**桶里**重列,不能扫本地 —— 否则切一次页签就把桶清单盖回本地的
                占位交付。网络失败原样不动(gr.update()),别把已有清单清空。
                """
                if _rp_is_direct(url):
                    s = str(url).strip().rstrip("/")
                    try:
                        names = runner.tos_list_deliveries(
                            s, str(region or "").strip() or None)
                    except Exception:  # noqa: BLE001 网络/SDK 异常:保留旧清单
                        return gr.update()
                    items = [(n, s + "/" + n) for n in names]
                    if cur and cur not in [v for _l, v in items]:
                        items = items + [(str(cur), cur)]
                    return gr.update(choices=items, value=cur)
                fresh = discover_deliveries(delivery)
                items = delivery_choices(delivery, fresh)
                if cur and cur not in fresh:
                    items = items + [(str(cur), cur)]
                return gr.update(choices=items, value=cur)

            # ⚠️ 这一跳**必须真的接上**:_picker_tick 从写出来那天起就没被 tick 过
            # (2026-08-14 用户实见:跑完一批,报告页的交付下拉里死活没有它,
            # 只能刷新整页)。以前"跑完 → 刷新页面 → 看报告"的用法掩盖了它;
            # 现在跑批就在同一个页面里发起,不接就是必现。
            # 只换选项、不改选中值:用户正在看别的交付时,列表在背后更新是好事,
            # 页面被拽走则是坏事 —— 所以**不自动跳到新交付**。
            #
            # **主路径是页签切换,不是计时器**(2026-08-14 实测教训):给 picker 挂上
            # 10 秒 Timer 之后,`window.gradio_config` 里那个 timer 明明 active,函数体里
            # 的日志却一行都没打出来 —— 定时器在这层压根没跳。而"跑完 → 点到报告页"
            # 是必然发生的动作,`Tab.select` 是用户真点出来的事件,不依赖任何计时。
            # 计时器留着当兜底:它若能跳,用户停在报告页也能看到新交付冒出来。
            report_tab.select(_picker_tick, [picker, rp_root, rp_rg], picker)
            if hasattr(gr, "Timer"):
                gr.Timer(10.0).tick(_picker_tick, [picker, rp_root, rp_rg], picker)

            def _ep_select(m, eid):
                """点清单某条 → 记住它 + 换右侧详情。"""
                return (eid, *_detail(m, eid))

            # 用 .input 不用 .change:后端回填清单(翻页/换桶/换交付)也会改 value,
            # 走 .change 就会被自己的回填触发一遍——翻页时把详情冲掉,正是"跨页
            # 保持选中"要防的事。.input 只认用户点击。
            ep_pick.input(_ep_select, [state, ep_pick], [ep_sel, *_ep_outs])

            def _ep_bucket_change(m, bucket):
                """点桶 → 左清单换成该桶的条目(回第 1 页),详情跳到该桶第一条。

                清单与详情必须一起换:选中项停在被筛掉的旧 eid 上,右边就成了
                "清单里看不见的那条"的详情——实测最容易骗人。
                """
                ids = bucket_ids(m or {}, bucket or BUCKET_ALL)
                first = ids[0] if ids else None
                return (*_ep_list(m, bucket, 0, first), *_detail(m, first))

            ep_bucket.input(_ep_bucket_change, [state, ep_bucket],
                            [*_ep_list_outs, *_ep_outs])

            # 翻页只动清单,不动右侧详情(正在看的那条翻页时不该被冲掉)
            ep_prev.click(lambda m, b, p, s: _ep_list(m, b, (p or 0) - 1, s),
                          [state, ep_bucket, ep_page, ep_sel], _ep_list_outs)
            ep_next.click(lambda m, b, p, s: _ep_list(m, b, (p or 0) + 1, s),
                          [state, ep_bucket, ep_page, ep_sel], _ep_list_outs)

            # ── 同步曲线页:筛选换档回第 0 页,翻页越界回绕(与裁决卡片同款) ──
            sy_filter.change(lambda m, mode: _sync_view(m, mode, 0),
                             [state, sy_filter], _sy_outs)
            sy_prev.click(lambda m, mode, p: _sync_view(m, mode, (p or 0) - 1),
                          [state, sy_filter, sy_page], _sy_outs)
            sy_next.click(lambda m, mode, p: _sync_view(m, mode, (p or 0) + 1),
                          [state, sy_filter, sy_page], _sy_outs)

            def _table_change(m, label):
                name = {v: k for k, v in DETAIL_LABELS.items()}.get(label)
                note, headers, rows = _detail_table_md(m, name)
                return note, gr.update(value=rows, headers=headers)

            dt_pick.change(_table_change, [state, dt_pick], [dt_note, dt_table])

            # ── 「待你裁决」合并卡片:筛选 / 翻页(按 episode 走)/ 点行跳转 ──

            # 用 .input 不用 .change(与交付下拉同一课):点行跳转会回填筛选值,
            # 走 .change 就会被自己的回填再触发一遍渲染,把跳到的卡片冲回第 0 条
            mg_filter.input(lambda m, f: _mg_render(m, f, 0),
                            [state, mg_filter], _mg_outs)
            mg_prev.click(lambda m, f, i: _mg_render(m, f, (i or 0) - 1),
                          [state, mg_filter, mg_idx], _mg_outs)
            mg_next.click(lambda m, f, i: _mg_render(m, f, (i or 0) + 1),
                          [state, mg_filter, mg_idx], _mg_outs)

            def _mg_jump_to(m, filt, eid):
                """卡片跳到指定 episode。当前筛选档看不见它(筛着「只看成败」点了
                标注表)就切回「全部」再跳 —— 绝不在看不见的档里静默定位。"""
                q = merged_queue_view(m or {}, merge_filter_mode(filt))
                ids = [it["id"] for it in q]
                if eid in ids:
                    return (gr.update(), *_mg_render(m, filt, ids.index(eid)))
                all_label = merged_filter_choices(m or {})[0]
                ids_all = [it["id"] for it in
                           merged_queue_view(m or {}, MERGE_FILTER_ALL)]
                i = ids_all.index(eid) if eid in ids_all else 0
                return (gr.update(value=all_label), *_mg_render(m, all_label, i))

            def _q_jump(m, filt, evt):
                """点待裁决队列表任意一行 → 合并卡片跳到该 episode(行序与
                merged_review_queue 一致,按下标对号)。"""
                q = merged_review_queue(m or {})
                row = evt.index[0]
                eid = q[row]["id"] if row < len(q) else ""
                return _mg_jump_to(m, filt, eid)

            # gradio 靠注解识别"要注入 SelectData";本文件开了 future annotations,
            # 字符串注解会在模块全局被 eval(gr 是函数内导入)→ NameError。
            # 塞真实类对象绕开字符串求值。
            _q_jump.__annotations__ = {"evt": gr.SelectData}

            _mg_jump_outs = [mg_filter, *_mg_outs]
            qu_table.select(_q_jump, [state, mg_filter], _mg_jump_outs)

            def _mg_item(m, filt, idx):
                """当前卡片指着的合并队列条目(空队列返回 None)。"""
                q = merged_queue_view(m or {}, merge_filter_mode(filt))
                return q[(idx or 0) % len(q)] if q else None

            def _mg_au_decide(m, filt, idx, newlab, note, decision):
                """① 落一条标注裁决。不整卡重渲染:重渲染会把用户手改的「修正后
                标注」冲回预填值、把视频重载 —— 只更新受影响的槽位,②的显隐
                跟着 ① 的新裁决即时联动(采纳→展开可选;弃用→矛盾拦截)。"""
                it = _mg_item(m, filt, idx)
                if it is None:
                    return ("⚠️ 无条目可裁决", *[gr.update()] * 15)
                if readonly_block_msg(m):
                    return (readonly_block_msg(m), *[gr.update()] * 15)
                if it["audit"] is None:
                    return ("⚠️ 这条没有标注问题(只有成败问题)", *[gr.update()] * 15)
                msg = record_label_decision(m["path"], it["id"], decision,
                                            newlab or "", note or "")
                # 镜像交付(2026-08-20 阶段4):裁决 CSV 即时写回源桶 —— 留在
                # 本地缓存等于丢(缓存可清),失败也要说出来,不许静默
                if msg.startswith("✅"):
                    msg += " " + runner.push_decisions(m["path"])
                if msg.startswith("✅"):
                    msg = msg.replace("✅ 已记录:", "✅ 已记录(草稿,可随时改判):")
                    btns = _au_btns(decision)                # 记录成功才点亮所选键
                    dec_now = decision
                else:
                    btns = [gr.update()] * 3                 # 校验失败:按钮不动
                    dec_now = load_label_decisions(m).get(it["id"], {}) \
                        .get("decision", "")
                tv_sec = _mg_tv_section(m, it, dec_now)
                # 顺带刷新进度提示(裁完最后一条,催办语就该消失)
                # 卡头跟着刷(2026-08-21):采纳改标后第二行立刻换成新标注
                return (msg, gr.update(value=merged_queue_rows(m)), _mg_au_info(m, it),
                        *btns, *tv_sec,
                        readonly_banner_md(m) + (merged_hint_md(m) or ""),
                        audit_note_md(m), _mg_head(m, it))

            _dec_outs = [au_status, qu_table, au_info, au_adopt, au_keep, au_hold,
                         mg_tv_block, tv_mode_note, tv_info, tv_readings,
                         tv_pass, tv_fail, tv_hold,
                         mg_hint, sk_audit_note, mg_info]
            au_adopt.click(lambda m, f, i, nl, nt: _mg_au_decide(m, f, i, nl, nt, "采纳建议改标"),
                           [state, mg_filter, mg_idx, au_newlab, au_note], _dec_outs)
            au_keep.click(lambda m, f, i, nl, nt: _mg_au_decide(m, f, i, nl, nt, "维持原标注"),
                          [state, mg_filter, mg_idx, au_newlab, au_note], _dec_outs)
            au_hold.click(lambda m, f, i, nl, nt: _mg_au_decide(m, f, i, nl, nt, _HOLD),
                          [state, mg_filter, mg_idx, au_newlab, au_note], _dec_outs)
            # 卡片级弃用:落的仍是标注线的「弃用该条」(后端语义与溯源一个字没动),
            # 只是入口从标注块里提到了卡片级 —— 见按钮定义处的理由。
            mg_kill.click(lambda m, f, i, nl, nt: _mg_au_decide(m, f, i, nl, nt, "弃用该条"),
                          [state, mg_filter, mg_idx, au_newlab, au_note], _dec_outs)

            def _mg_tv_decide(m, filt, idx, note, verdict):
                """② 落一条成败裁决。矛盾拦截两道门:按钮禁用(渲染层)之外,
                record_task_verdict_checked 落盘前还会再查一次 ① 的「弃用该条」。"""
                it = _mg_item(m, filt, idx)
                if it is None:
                    return ("⚠️ 无条目可裁决", *[gr.update()] * 7)
                if readonly_block_msg(m):
                    return (readonly_block_msg(m), *[gr.update()] * 7)
                dec = load_label_decisions(m).get(it["id"], {}).get("decision", "")
                if success_block_mode(it, dec) == "hidden":
                    return ("⚠️ 这条现在没有成败问题要答", *[gr.update()] * 7)
                msg = record_task_verdict_checked(m, it["id"], verdict, note or "")
                if msg.startswith("✅"):
                    msg += " " + runner.push_decisions(m["path"])
                if msg.startswith("✅"):
                    msg = msg.replace("✅ 已记录:", "✅ 已记录(草稿,可随时改判):")
                    btns = _tv_btns(verdict)
                else:
                    btns = [gr.update()] * 3
                tv_sec = _mg_tv_section(m, it, dec)
                return (msg, gr.update(value=merged_queue_rows(m)),
                        tv_sec[2], tv_sec[3], *btns,
                        readonly_banner_md(m) + (merged_hint_md(m) or ""))

            _tv_dec_outs = [tv_status, qu_table, tv_info, tv_readings,
                            tv_pass, tv_fail, tv_hold, mg_hint]
            tv_pass.click(lambda m, f, i, nt: _mg_tv_decide(m, f, i, nt, "判成功"),
                          [state, mg_filter, mg_idx, tv_note], _tv_dec_outs)
            tv_fail.click(lambda m, f, i, nt: _mg_tv_decide(m, f, i, nt, "判失败"),
                          [state, mg_filter, mg_idx, tv_note], _tv_dec_outs)
            tv_hold.click(lambda m, f, i, nt: _mg_tv_decide(m, f, i, nt, _HOLD),
                          [state, mg_filter, mg_idx, tv_note], _tv_dec_outs)

            # ── ③ 被拒复议:翻页 / 点行跳转 / 两键复议 ──
            ap_prev.click(lambda m, i: _ap_render(m, (i or 0) - 1),
                          [state, ap_idx], _ap_outs)
            ap_next.click(lambda m, i: _ap_render(m, (i or 0) + 1),
                          [state, ap_idx], _ap_outs)

            def _ap_jump(m, evt):
                return _ap_render(m, evt.index[0])

            _ap_jump.__annotations__ = {"evt": gr.SelectData}
            ap_table.select(_ap_jump, state, _ap_outs)

            def _ap_decide(m, idx, note, appeal):
                q = (m or {}).get("reject_appeal") or []
                if not q:
                    return ("⚠️ 无可复议的条目", gr.update(), gr.update(),
                            *[gr.update()] * 2)
                a = q[(idx or 0) % len(q)]
                if readonly_block_msg(m):
                    return (readonly_block_msg(m), gr.update(), gr.update(),
                            *[gr.update()] * 2)
                msg = record_reject_appeal(m["path"], a.get("id", ""), appeal,
                                           note or "")
                if msg.startswith("✅"):
                    msg += " " + runner.push_decisions(m["path"])
                if msg.startswith("✅"):
                    msg = msg.replace("✅ 已记录:", "✅ 已记录(草稿,可随时改判):")
                    btns = _ap_btns(appeal)
                else:
                    btns = [gr.update()] * 2
                info = _ap_render(m, idx)[2]                 # 卡片头同步"已复议"状态
                return msg, gr.update(value=appeal_rows(m)), info, *btns

            _ap_dec_outs = [ap_status, ap_table, ap_info, ap_keep, ap_back]
            ap_keep.click(lambda m, i, nt: _ap_decide(m, i, nt, "维持拒绝"),
                          [state, ap_idx, ap_note], _ap_dec_outs)
            ap_back.click(lambda m, i, nt: _ap_decide(m, i, nt, "捞回"),
                          [state, ap_idx, ap_note], _ap_dec_outs)

            # ── 执行裁决(人工裁决页底部,2026-08-19):作用对象 = 当前加载的
            #    那份交付与那一次运行(state 里的 m),不是任何下拉的值 ──

            def _ex_view(m):
                """执行入口的状态行 + 源数据集兜底的显隐(装配顺序 = _ex_outs)。

                随 _load 一起刷新:换交付/换运行,这里跟着换,用户不用再选一遍。
                老交付没记「源数据集路径」时才露出选择器,绝不按名字猜。
                四个 update 各建各的:gradio 处理 update dict 时会就地 pop 键,
                共用同一个实例会让后面的输出拿到被掏空的壳。
                """
                path = (m or {}).get("path") or ""
                if not path or (m or {}).get("load_error"):
                    return ("(先在上方打开一份能读到的交付)",
                            *(gr.update(visible=False) for _ in range(4)))
                # 2026-08-19 用户点名「都是废话,删除」:执行对象(交付/运行)、
                # 原始数据集路径、裁决计数 —— 你就站在这份交付的页面上,再复述
                # 一遍是噪音。**唯一还要说话的情形**:交付没记原始数据集,那时得
                # 让用户选,不说清楚他不知道下面那两个下拉是干什么的。
                src = runner.source_dataset_of(path)
                text = "" if src else "这份交付没记原始数据集,请选:"
                return (text, *(gr.update(visible=not src) for _ in range(4)))

            _ex_outs = [ex_info, ex_ds, ex_src_dd, ex_src_note, ex_ds_note]

            def _ex_busy_note(act):
                """有任务在跑时的那句话:明说在等谁 —— 不发起、不排队也不报失败。

                拦本身是对的(rejudge 改写交付,与正在写它的跑批撞车是灾难),
                但必须说出来,不能让按钮看起来像坏了。
                """
                who = (act.get("label") or act.get("command")
                       or act.get("run_id") or "一个任务")
                return (f"⚠️ 当前有任务在跑({who}),等它结束后再执行 —— "
                        "执行裁决会改写这份交付,不与其它任务同时跑")

            def _ex_hold(m):
                """点「执行裁决」→ 只弹确认块,**绝不**在这里起任务(防"按钮即
                执行");顺手把状态行刷新一遍(裁决计数可能刚变过)。

                确认块替换掉旧页签那个确认勾选框:勾选框太容易顺手勾过去,而这个
                动作会改写交付,必须当面问一句。
                """
                fresh = _ex_view(m)
                path = (m or {}).get("path") or ""
                if not path or (m or {}).get("load_error"):
                    return (gr.update(visible=False), "",
                            "⚠️ 先在上方打开一份能读到的交付", *fresh)
                act = runner.active_run(_runs_root)
                if act:
                    return (gr.update(visible=False), "", _ex_busy_note(act),
                            *fresh)
                deliv = os.path.basename(delivery_root_of(path))
                run = os.path.basename(str(path).rstrip("/"))
                target = f"**{deliv}**" + (f" / {run}" if run != deliv else "")
                return (gr.update(visible=True),
                        f"人工裁决结果会**改写这份交付的内容**({target}),要继续吗?",
                        "", *fresh)

            ex_go.click(_ex_hold, state, [ex_ask, ex_ask_md, ex_msg, *_ex_outs])

            def _ex_run(m, src_name, ds, backend, cfg):
                """「确定」→ 起 rejudge 任务。

                🔴 不另造进度浮层(2026-08-19 定案):走 runner.start,与跑质检
                同一套机器 —— 状态落 .runs/、完成态看退出码文件、停止走任务台的
                「停止」(killpg + /proc 判活,rejudge 一并适用),关掉页面再打开
                进度还在。起动成功就把视图切到「跑质检 · 当前任务」看进度:进度
                数据源 = 任务台自己,天然同源。
                """
                path = (m or {}).get("path") or ""
                hide = gr.update(visible=False)
                hold = [gr.update() for _ in range(5)]   # _tk_outs 含 rn_go + tk_stop
                if not path or (m or {}).get("load_error"):
                    return (gr.update(), hide,
                            "⚠️ 先在上方打开一份能读到的交付", *hold)
                act = runner.active_run(_runs_root)
                if act:
                    return (gr.update(), hide, _ex_busy_note(act), *hold)
                if str(backend or "").endswith(BACKEND_BAD):
                    return (gr.update(), hide,
                            "⚠️ 选中的模型服务当前不可用,换一个,或把那台服务"
                            "修好后切回本页自动重检", *hold)
                src = runner.source_dataset_of(path)
                try:
                    if not src:
                        # 交付没记源数据集才用兜底选择器(bucket_path 白名单
                        # 查表,与跑质检那侧同一条安全边界)
                        src = runner.resolve_under(
                            runner.bucket_path(_buckets, src_name), ds or "")
                    cfg = (runner.resolve_tos_path(cfg)
                           if str(cfg or "").strip() else None)
                except ValueError as e:
                    return (gr.update(), hide, f"⚠️ {e}", *hold)
                deliv = os.path.basename(delivery_root_of(path))
                run = os.path.basename(str(path).rstrip("/"))
                # 镜像交付(2026-08-21):本地只是报告页的部分镜像,裁决要改交付数据集
                # → 把桶里的批次地址交给 CLI,它自己全量镜像、执行、写回
                _origin = runner.tos_origin_of(path)
                _extra = {}
                if _origin:
                    path = f"{_origin['delivery_url']}/{run}"
                    if _origin.get("region"):
                        _extra["delivery_region"] = _origin["region"]
                if str(src or "").startswith("tos://"):
                    _rg = runner.source_region_of((m or {}).get("path") or "")
                    if _rg:
                        _extra["input_region"] = _rg
                view = _tk_start("rejudge",
                                 "执行裁决 " + deliv
                                 + (f" / {run}" if run != deliv else ""),
                                 delivery=path, input=src, config=cfg,
                                 vlm_backend=_backend_code(backend), **_extra)
                if str(view[2] or "").startswith("⚠️"):
                    # 没起来(拼参失败/被别的任务抢了先手):留在本页把话说清,
                    # 不切视图 —— 切过去却什么都没开始,比不动更迷惑
                    return (gr.update(), hide, view[2], *view)
                return (gr.Tabs(selected="console"), hide,
                        "已开始执行,进度在「跑质检 · 当前任务」"
                        "(可停止;关掉页面不影响执行)", *view)

            ex_yes.click(_ex_run, [state, ex_src_dd, ex_ds, ex_backend, ex_cfg],
                         [topnav, ex_ask, ex_msg, *_tk_outs])
            ex_no.click(lambda: gr.update(visible=False), None, ex_ask)

            def _ex_btn_state():
                """「执行裁决」按钮的可点性(issue #55 同族):有任务在跑就置灰。

                刷新时机只有两个**真事件**:app.load 和回到报告页(Tab.select)——
                报告页没有任务台那条 2 秒轮询,也不为一个按钮造一条(gr.Timer 在
                这层实测不跳,见 _picker_tick 的注释)。这意味着停在本页时状态
                不会自己变——够用:主流程是「确定→自动切去跑质检页看进度→回来时
                select 重算」;而"别的任务在跑还点了"那条路,点击本身会拿到
                _ex_busy_note 那句"在等谁"(按钮没锁死,fail-open 同 _run_btn)。
                """
                try:
                    busy = runner.is_busy_state(runner.active_run(_runs_root))
                except Exception:  # noqa: BLE001  读不到状态=按不忙算(fail-open)
                    busy = False
                if busy:
                    return gr.update(interactive=False, value="有任务在跑")
                return gr.update(interactive=True, value="执行裁决")

            report_tab.select(_ex_btn_state, None, ex_go)
            app.load(_ex_btn_state, None, ex_go)
            ex_src_dd.input(lambda s: _src_datasets(s, False), ex_src_dd,
                            [ex_src_note, ex_ds, ex_ds_note])
            # 自动探活(2026-08-21 用户:可用性该系统自己查,不该让人点按钮):
            # 开页一次 + 切到跑质检/报告页各一次(真事件),同时刷新两处下拉 ——
            # 它们指的是同一批服务,只刷一处另一处就挂着陈旧状态撒谎。60s 缓存。
            app.load(_do_probe, [rn_backend, ex_backend], [rn_backend, ex_backend])
            console_tab.select(_do_probe, [rn_backend, ex_backend], [rn_backend, ex_backend])
            report_tab.select(_do_probe, [rn_backend, ex_backend], [rn_backend, ex_backend])
            # 筛选/排序都只是重画同一份数据(load_timeline 读的是交付里的
            # episodes_timeline.json),不重算任何指标。
            def _tl_view(m, show_label, sort_label):
                show = _label_key(TL_FILTERS, show_label, "both")
                srt = _label_key(TL_SORTS, sort_label, "episode")
                return timeline_html(load_timeline(m), show=show, sort=srt)

            for _c in (tl_show, tl_sort):
                _c.change(_tl_view, [state, tl_show, tl_sort], tl_html)
            app.load(_pick_delivery, picker, [run_pick, *outs])

            report_ctx.close()          # 报告页到此为止,下面的页签是它的兄弟
            if terminal:
                # 内嵌终端(2026-07-29 U4,替代 ttyd iframe):xterm.js 画屏 +
                # 本服务的 /ws/term(forkpty 起 bash)。装配全在 term.js 里,
                # 这里只放它要挂载的容器 div;term.js 等这个 div **可见**才连,
                # 所以不点终端页签就不会在服务端 fork 出 shell。
                # 位置:**最右**(2026-08-13 用户定)—— 它是我们排障用的,
                # 不该在客户第一眼看到的位置;默认落地页也从报告改成了任务台。
                with gr.Tab("终端", id="term"):
                    gr.HTML('<div id="curation-term-screen"></div>')
    return app


def normalize_root_path(root_path: str | None) -> str:
    """挂载前缀归一:"" 或 "/xx"(无尾斜杠),别的写法("curation"、"/curation/")都收拢。

    归一后的值可以直接 f"{root}/term-static" 拼路径 —— 根路径("")拼出来
    就是老写法,带前缀时拼出来不重斜杠。
    """
    s = (root_path or "").strip().strip("/")
    return f"/{s}" if s else ""


class _ImmutableAssetsMiddleware:
    """纯 ASGI 中间件:静态资产(路径前缀匹配、200 响应)加
    `Cache-Control: public, max-age=31536000, immutable`。

    只认 /assets/(gradio 前端分片,文件名含内容哈希,改内容必改名);其余路径
    一个头都不加 —— 动态内容被缓存是另一类事故。不用 BaseHTTPMiddleware:它会
    把 SSE 流整个缓冲起来。
    """

    def __init__(self, app, prefix: str = "/assets/"):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message.get("type") == "http.response.start" and message.get("status") == 200:
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() != b"cache-control"]
                headers.append((b"cache-control",
                                b"public, max-age=31536000, immutable"))
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, _send)


def create_asgi_app(delivery: str, config_path: str | None = None,
                    probe_timeout: float = 5.0, terminal: bool = False,
                    review_dir: str | None = None, data_root: str | None = None,
                    root_path: str | None = None):
    """→ FastAPI 应用(gradio 挂在 `{root}/`,自定义路由挂在它前面)。

    为什么不再用 `blocks.launch()`:launch() 自己造 FastAPI + 自己跑 uvicorn,拿不到
    那个 app 的引用,也就挂不上 `/ws/term`。改成我们造 app、gradio 往上挂,单端口
    同时提供 UI + 终端 + 静态资产 + 健康检查。

    路由注册顺序有讲究:starlette 按注册顺序匹配,gradio 的挂载是 catch-all mount,
    必须最后挂,否则它会吃掉 `/ws/term` 和 `/term-static/*`。

    root_path(2026-08-14,与 rerun 同域名分流):UI 要住在网关的一个路径前缀下
    (`/curation` → 本服务,`/` → rerun viewer),而 APIG 分流**不剥前缀**,请求原样
    带着 `/curation/...` 打过来 —— 所以是把全部路由注册在前缀下,而不是设 ASGI
    root_path(那是给"网关剥前缀"的场景准备的,两者相反)。
    """
    import gradio as gr
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    from . import auth

    root = normalize_root_path(root_path)
    blocks = build_app(delivery, config_path, probe_timeout, terminal=terminal,
                       review_dir=review_dir, data_root=data_root)
    api = FastAPI()

    # 探针端点(鉴权豁免,见 auth.EXEMPT_PATHS):k8s readinessProbe 现在指 /(整页
    # 渲染),配了 Basic 之后会被 401 打红 → 留一个不设防的轻量端点给探针用。
    # 带前缀部署时两个路径都留:探针直连容器端口用 /healthz 就好,网关侧健康检查
    # 只能带前缀进来。
    @api.get("/healthz", response_class=PlainTextResponse)
    def _healthz() -> str:                     # noqa: ANN202
        return "ok"

    if root:
        api.add_api_route(f"{root}/healthz", _healthz, methods=["GET"],
                          response_class=PlainTextResponse)

    if terminal:
        from . import terminal as term
        api.mount(f"{root}/term-static", StaticFiles(directory=STATIC_DIR),
                  name="term-static")
        api.add_api_websocket_route(f"{root}/ws/term", term.term_endpoint)
        log.info("终端:已开启(%s/ws/term,shell=%s,cwd=%s)",
                 root, term.resolve_shell(), term.resolve_workdir())

    # 静态审片站(curation review-page 的产出):挂在 gradio catch-all 之前。
    # html=True → /review 直接出 index.html;目录缺失只警告不拦启动(先起 UI 后生成站点
    # 是合法顺序,StaticFiles 每次请求现场解析路径,后补的目录立即可用)。
    if review_dir:
        if not os.path.isdir(review_dir):
            log.warning("审片站目录尚不存在:%s(生成后无需重启即可访问)", review_dir)
            os.makedirs(review_dir, exist_ok=True)
        api.mount(f"{root}/review", StaticFiles(directory=review_dir, html=True),
                  name="review")
        log.info("审片站:已挂 %s/review → %s", root, review_dir)

    auth.apply(api, terminal_enabled=terminal,
               extra_exempt=(f"{root}/healthz",) if root else ())
    # ── 公网链路的两道送分题(2026-08-20 APIG 实测:HTML 532 KB + config 259 KB
    #    **未压缩**穿网关,139 个 JS 分片没有缓存头)──
    # ① gzip:动态响应(/ 与 /config 占 780 KB)压到 ~86 KB。starlette 0.52 的
    #    GZipMiddleware 默认排除 text/event-stream,gradio 的 SSE 事件流不受
    #    影响(压缩缓冲会把事件憋住,那是绝不能碰的)。
    # ② /assets/* 文件名带内容哈希,永不变 → immutable 一年:再次打开一个分片
    #    请求都不发。只盖静态资产,动态路由一律不缓存。
    from starlette.middleware.gzip import GZipMiddleware
    api.add_middleware(GZipMiddleware, minimum_size=1024)
    api.add_middleware(_ImmutableAssetsMiddleware, prefix=f"{root}/assets/")
    # allowed_paths:允许页面直读交付目录下的证据文件(gradio 默认只许临时目录);
    # 审片站目录同样要放行——Episodes 页的视频第一来源就在那儿,不放行会 403。
    allowed = [delivery] + ([review_dir] if review_dir else [])
    # footer_links=[]:整排页脚(Use via API / Built with Gradio / Settings)去掉。
    # 头一个会把本服务的接口文档摆给任何打开页面的人看,另两个对客户毫无用处。
    # 用 gradio 自己的开关而不是 CSS 藏 —— 藏起来的链接照样可点、照样在 DOM 里。
    return gr.mount_gradio_app(api, blocks, path=root or "/", allowed_paths=allowed,
                               footer_links=[], **presentation(terminal, root))


def launch(delivery: str, config_path: str | None = None, host: str = "0.0.0.0",
           port: int = 7860, probe_timeout: float = 5.0,
           terminal: bool = False, review_dir: str | None = None,
           data_root: str | None = None, root_path: str | None = None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_asgi_app(delivery, config_path, probe_timeout, terminal=terminal,
                          review_dir=review_dir, data_root=data_root,
                          root_path=root_path)
    root = normalize_root_path(root_path)
    log.info("质检台 UI 监听 http://%s:%s%s/(交付根目录 %s)",
             host, port, root, delivery)
    uvicorn.run(app, host=host, port=port)
