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

from ..delivery import (delivery_root_of, resolve_run, run_choices,
                        run_name_of_run_id)
from .manifest import (APPEAL_CHOICES, APPEAL_HEADERS, AUDIT_HEADERS,
                       BUCKET_ALL, DECISION_CHOICES,
                       DETAIL_LABELS, TASK_REVIEW_HEADERS, VERDICT_CHOICES,
                       WORKFLOW_GUIDE, appeal_hint_md, appeal_reason_text,
                       appeal_rows, audit_clip_paths, audit_note_md,
                       bucket_choices, bucket_ids,
                       load_label_decisions, load_reject_appeals,
                       load_task_verdicts, play_all_button_html,
                       record_label_decision, record_reject_appeal,
                       record_task_verdict,
                       AUDIT_TERM, LATENCY_HEADERS,
                       LATENCY_KIND_NOTE, LATENCY_NOTE, LATENCY_PCTL_NOTE,
                       SKILL_HEADERS, SYNC_FILTER_ALL, SYNC_FILTERS,
                       TL_FILTERS, TL_SORTS,
                       audit_rows, check_table_html, delivery_choices,
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
                       task_review_hint_md, task_review_rows, timeline_html,
                       video_detail_view)
from . import runner            # 任务台执行层(纯新增,报告页那套一个字不动)

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

#: 终端页签的前端装配:资产从**本服务**取(pod 内无 CDN 通路),只在开了终端时注入。
_TERMINAL_HEAD = """
<link rel="stylesheet" href="/term-static/xterm.css" />
<script src="/term-static/xterm.js"></script>
<script src="/term-static/addon-fit.js"></script>
<script src="/term-static/term.js"></script>
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
QUICK_SCAN = "快速质检"
CUSTOM_SCAN = "自选模块"   # 曾叫「自定义模块」——听着像"自己定义模块里查什么"
                          # (那是以后的事),其实是"从现成模块里挑几个跑"

#: 「交付名」下面那行说明。选一个与选多个,这个名字的含义**不一样**,不说清楚
#: 客户会以为三个数据集的结果会互相覆盖(2026-08-13 用户提多选时点名要说明白)。
#: 多选时的落盘形状与 CLI `--batch` 一致(`<交付名>/<数据集名>/`),报告页的递归
#: 发现本来就找得到。
OUT_NAME_HINT_ONE = ("*结果放进交付根下这个名字的目录里,本次跑批各自成一个"
                     "时间戳子目录 —— 同名再跑不会覆盖上一次。*")
OUT_NAME_HINT_MANY = ("*选了多个数据集:这个名字当**父文件夹**,每个数据集各出一份"
                      "子交付 `<交付名>/<数据集名>/`,按选中的顺序一个接一个跑;"
                      "中间某个没跑成,后面的照跑。*")

#: 「快速质检」旁边那个问号里的话。**按实际跑什么写**:--lite 只是跳过要 VLM 的
#: 三步(任务成败判定 / 打标 / 技能画像),其余检查一步不少。
QUICK_SCAN_TIP = ("跑不需要模型的那几项:视觉质量、运动质量、运动学极限、"
                  "视频-动作同步、时间戳与精确去重。"
                  "不做任务成败判定、打标与技能分布画像 —— 这三项要调 VLM。")

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

  function inject() {
    var box = document.getElementById('qc-scope');
    if (!box || box.querySelector('.qc-tip')) return;
    var labels = box.querySelectorAll('label');
    for (var i = 0; i < labels.length; i++) {
      var span = labels[i].querySelector('span');
      if (!span || span.textContent.trim() !== '__NAME__') continue;
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
        box.textContent = '__TIP__';
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
      return;
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
/* 内层页签(任务台的跑质检/执行人工裁决、明细的四个子页)在大卡里面,
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
"""


# 顶层导航按钮样式(2026-07-29 用户定:大、明显、立体)。只作用于 elem_id=topnav
# 的外层两页签,内层六个报告 tab 不受影响。立体感=渐变+外阴影(凸起),选中态=
# 橙色渐变+内阴影(按下)。选中类名在 gradio 版本间摇摆,selected/aria-selected 双保。
_TOPNAV_CSS = """
/* 顶层导航(任务台 / 质检报告 / 终端)。2026-08-13 改按 Arco:去掉橙色渐变与
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
    for name, b in sorted((cfg.get("vlm_backends") or {}).items()):
        try:
            models = list_models(b["endpoint"], b.get("api_key_env"), timeout_s=timeout)
            shown = ", ".join(models[:3]) + (f" …(共{len(models)}个)" if len(models) > 3 else "")
            rows.append([name, "✅在线", shown])
        except Exception as e:  # noqa: BLE001
            rows.append([name, "❌不可达", type(e).__name__])
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

#audit-queue tbody tr, #task-queue tbody tr { cursor: pointer; }
#audit-queue tbody tr:hover td,
#task-queue tbody tr:hover td { background: rgba(255, 140, 0, 0.10) !important; }

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
    """下拉选中项 → 标签本体(容忍带「· 可用/暂不可用」后缀)。"""
    s = str(choice or "")
    for suffix in (BACKEND_OK, BACKEND_BAD):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def _backend_options(labels: dict, status: dict | None = None) -> list:
    """{标签: 预设代号} + {代号: 是否在线} → 下拉选项。未检测的只给标签。"""
    return [label + ("" if status is None or code not in status
                     else (BACKEND_OK if status[code] else BACKEND_BAD))
            for label, code in labels.items()]


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
    n_ok = sum(1 for v in status.values() if v)
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


def presentation(terminal: bool = False) -> dict:
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
                 + _TIP_JS.replace("__NAME__", QUICK_SCAN)
                          .replace("__TIP__", QUICK_SCAN_TIP)
                 + (_TERMINAL_HEAD if terminal else "")),
        # 标签页图标:不设就是 Gradio 自带的橘色 logo(用户 2026-08-13 点名)。
        # 换成 Arco 蓝圆角方块 + 白色漩涡(照 Daft 那枚的手感重画,底色主色化 ⇒
        # 既认得出这套系统的出身,又和整页的 Arco 蓝一致)。生成脚本
        # scripts/make_favicon.py,纯 stdlib 画的,改形状重跑即可。
        # gradio 只收 .png/.gif/.ico(不吃 svg),所以资产是张 64px PNG。
        "favicon_path": FAVICON,
    }


# 「人工裁决」页的视觉件(2026-08-07 用户反馈:页面太平淡,引导和区块头要一眼看到)。
# 全部内联样式:不依赖主题变量,浅色页面直出;步骤链和区块头是"人要按顺序干活"
# 的导航件,值得比正文重一个视觉量级。
# 2026-08-13 用户定:引导框改 Arco 蓝(arcoblue-1 底 / -2 边 / -6 主色 / -7 深字)。
# 原来那句"这一页只记录你的裁决…"用户点名删掉 —— 步骤链本身已经说明白了。
_ADJ_GUIDE_HTML = ("""
<div style="background:#E8F3FF;border:1px solid #BEDAFF;border-left:3px solid #165DFF;
            border-radius:4px;padding:12px 16px;margin:2px 0 6px">
  <div style="font-weight:600;color:#0E42D2;margin-bottom:8px">建议按这个顺序做</div>
  <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;color:#4E5969">
    <span style="background:#fff;border:1px solid #BEDAFF;border-radius:4px;padding:2px 10px">
      <b style="color:#165DFF">1</b> 裁「__AUDIT__」</span><span style="color:#86909C">→</span>
    <span style="background:#fff;border:1px solid #BEDAFF;border-radius:4px;padding:2px 10px">
      <b style="color:#165DFF">2</b> 到「任务台 · 执行人工裁决」执行一次
      <span style="color:#86909C">(部分弃权会自动解决)</span></span><span style="color:#86909C">→</span>
    <span style="background:#fff;border:1px solid #BEDAFF;border-radius:4px;padding:2px 10px">
      <b style="color:#165DFF">3</b> 裁剩下的「任务成败弃权」</span><span style="color:#86909C">→</span>
    <span style="background:#fff;border:1px solid #BEDAFF;border-radius:4px;padding:2px 10px">
      <b style="color:#165DFF">4</b> 再执行一次</span>
  </div>
</div>""".replace("__AUDIT__", AUDIT_TERM))


def _adj_section_html(num: str, title: str, subtitle: str, color: str, dark: str) -> str:
    """区块头:色块序号 + 加粗标题 + 弱化副题,底部同色粗线把区块"框"出来。"""
    return (f'<div style="display:flex;align-items:baseline;gap:10px;margin:20px 0 4px;'
            f'padding-bottom:7px;border-bottom:3px solid {color}">'
            f'<span style="background:{color};color:#fff;font-weight:800;border-radius:8px;'
            f'padding:2px 13px;font-size:1.05rem">{num}</span>'
            f'<span style="font-size:1.18rem;font-weight:800;color:{dark}">{title}</span>'
            f'<span style="color:#86909C;font-size:.9rem">{subtitle}</span></div>')


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

    data_root = 数据集根目录(「任务台」页签只列这个根下的数据集)。
    ⚠️ 任务台是 2026-08-13 **纯新增**的页签,放在全部页签的最后:现有那套质检报告
    页签(质检总览/Episodes/人工裁决/技能画像/同步曲线/Stuck 时间线/明细/性能剖析/
    后端状态)的顺序、默认落地页、组件与回调**一律不动**(用户红线)。它也不共用
    `state` 与 `outs`——Episodes 那个"重载级联并发吞掉翻页按钮"的 bug(6bb28b5)
    就是共享输出列表惹的祸,不重演。
    """
    import gradio as gr

    choices = discover_deliveries(delivery)
    if not choices:
        raise SystemExit(f"目录里找不到任何交付(既无跑批子目录也无 passed.json):{delivery}")

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
        return (episode_card_html(m, eid), episode_video_html(m, eid, review_dir),
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

    def _au_btns(decision):
        return _btns(DECISION_CHOICES, decision)

    def _tv_btns(verdict):
        return _btns(VERDICT_CHOICES, verdict)

    def _ap_btns(appeal):
        return _btns(APPEAL_CHOICES, appeal)

    def _vids(m, eid):
        """三路视频槽:有几路给几路;一路都没有时保留一个可见占位(不然卡片塌掉)。"""
        clips = audit_clip_paths(m, eid) if eid else []
        return [gr.update(value=(clips[i] if i < len(clips) else None),
                          visible=(i < len(clips) or not clips)) for i in range(3)]

    def _au_render(m, idx):
        """渲染第 idx 条标注分歧卡片(越界回绕)。装配顺序 = _au_outs。"""
        q = (m or {}).get("audit_queue") or []
        if not q:
            return (idx, "(无分歧条目)", "", "", "", "",
                    *[gr.update(value=None, visible=False)] * 3, *_au_btns(None))
        idx = idx % len(q)
        a = q[idx]
        dec = load_label_decisions(m).get(a.get("id", ""), {})
        info = (f"**{a.get('id','')}** · 档位 **{a.get('priority','参考')}** · "
                f"成败线判定:{a.get('task_verdict') or '—'}"
                + (f" · 已裁决:**{dec['decision']}**" if dec.get("decision") else "")
                + f"\n\n分歧说明:{a.get('reason','')}")
        return (idx, f"第 {idx + 1} / {len(q)} 条", info, a.get("label", ""),
                a.get("caption", ""), "", *_vids(m, a.get("id", "")),
                *_au_btns(dec.get("decision")))

    def _tv_render(m, idx):
        """渲染第 idx 条任务成败弃权卡片(越界回绕)。装配顺序 = _tv_outs。"""
        q = (m or {}).get("task_review") or []
        if not q:
            return (idx, "(无待裁决的任务成败弃权条目)", "", "",
                    *[gr.update(value=None, visible=False)] * 3, *_tv_btns(None))
        idx = idx % len(q)
        t = q[idx]
        v = load_task_verdicts(m).get(t.get("id", ""), {})
        info = (f"**{t.get('id','')}** · 当前判决:{t.get('current','?')}"
                + (f" · 已裁决:**{v['verdict']}**" if v.get("verdict") else "")
                + f"\n\n**系统弃权原因**:{t.get('reason') or '未注明'}")
        readings = f"关键读数:{readings_text(t.get('readings') or {})}"
        return (idx, f"第 {idx + 1} / {len(q)} 条", info, readings,
                *_vids(m, t.get("id", "")), *_tv_btns(v.get("verdict")))

    def _ap_render(m, idx):
        """渲染第 idx 条被拒复议卡片(越界回绕)。装配顺序 = _ap_outs。"""
        q = (m or {}).get("reject_appeal") or []
        if not q:
            return (idx, "(无可复议的被拒条目)", "", "", "", *_ap_btns(None))
        idx = idx % len(q)
        a = q[idx]
        eid = a.get("id", "")
        d = load_reject_appeals(m).get(eid, {})
        info = (f"**{eid}** · 系统判决:拒绝"
                + (f" · 已复议:**{d['appeal']}**" if d.get("appeal") else "")
                + f"\n\n**拒绝原因**:{appeal_reason_text(m, eid) or '未注明'}")
        readings = f"关键读数:{readings_text(a.get('readings') or {})}"
        return (idx, f"第 {idx + 1} / {len(q)} 条", info, readings,
                episode_video_html(m, eid, review_dir), *_ap_btns(d.get("appeal")))

    def _sync_view(m, mode, page):
        """同步曲线页的一屏(装配顺序 = _sy_outs)。分页/筛选逻辑全在 manifest。"""
        v = sync_view(m or {}, mode or SYNC_FILTER_ALL, page or 0)
        # items = [(路径, 标题)];逐槽位填充,多出来的槽位隐藏(不留空框)
        multi = gr.update(visible=v["pages"] > 1)
        return v["page"], v["note"], v["pos"], multi, multi, v["cards"]

    def _load(path):
        # 先把下拉里的值还原成真正的目录(手输半截字的情形,见 resolve_delivery);
        # 还原不了的照旧交给 load_delivery,它会挂 load_error 让整页明说读不到
        m = load_delivery(resolve_delivery(path, discover_deliveries(delivery)))
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
        # 详情面板随交付切换一起刷新:换目录后选中 eid 若恰好同名(ep000000 常见),
        # Dropdown 值不变→change 不触发→详情停留在上一份交付的陈旧渲染(实测踩过)
        return (m, overview_markdown(m), overview_rows(m), overview_note_md(m),
                _config_yaml(m),
                # 桶随交付切换复位到「全部」:停在「拒绝」而新交付一条都没被拒,
                # 看到的是空清单 + 一个还亮着的桶,等于骗人
                gr.update(choices=bucket_choices(m), value=BUCKET_ALL),
                *_ep_list(m, BUCKET_ALL, 0, first),
                skill_bar_html(m), skill_rows(m), audit_note_md(m),
                # 两块裁决面板都从第 0 条重新起(换交付不复位 = 停在上一份交付的
                # 条目上,按钮状态还是旧的,实测踩过)
                audit_rows(m), *_au_render(m, 0),
                task_review_hint_md(m), task_review_rows(m), *_tv_render(m, 0),
                # 被拒复议整区:没有可复议条目就整块不渲染
                gr.update(visible=bool(m.get("reject_appeal"))),
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
                latency_rows(perf), latency_bar_html(perf))

    # theme/css/head 不在这里传:gradio 6 把它们从 Blocks() 挪到了 launch()/
    # mount_gradio_app()(传给 Blocks 只换来一条 UserWarning,值被丢掉)。见 presentation()。
    with gr.Blocks(title="Robot Data Curation") as app:
        gr.Markdown("# 机器人数据 Curation 质检台")
        # 双层导航:顶层从左到右 =「任务台 / 质检报告 / 终端」,默认落在**任务台**
        # (2026-08-13 用户定:客户进来先看到能干活的面板;终端是排障用的,靠最右)。
        # terminal 关闭时终端页签整块不建 → 客户部署里看不到终端入口。
        with contextlib.ExitStack() as shell:
            # 顶层导航(2026-08-13 起**总是**渲染):「任务台」与「质检报告」并列,
            # 「终端」仍由 --terminal 控制。用户定:面板是面向客户的那张脸,压在
            # 报告页第十个子页签里等于没做。默认落地页仍是质检报告(selected=report),
            # 报告页那套子页签的顺序与内容一个字没动。
            shell.enter_context(gr.Tabs(selected="console", elem_id="topnav"))
            # ── 任务台(2026-08-13;布局与文案按用户当日反馈重排)──────────
            # 上半部 = 控制面板(客户来这里干活),下半部 = 任务与日志(干完看这里)。
            # 界面上**不写**"安全边界""并发配额"这类内部考量:那是我们的实现细节,
            # 客户只需要知道能点什么(用户点名删掉整段说明文字)。
            _data_root = data_root or os.environ.get("CURATION_DATA_ROOT") or DEFAULT_DATA_ROOT
            _deliv_root = runner.deliveries_root_of(delivery)
            _runs_root = runner.runs_root_of(_deliv_root)
            # {人话标签: 内部代号}。**原地更新**(不重新绑名字):探活时会重读配置
            # 刷新它,下面那几个闭包要跟着一起看见新的表。
            _backend_map = runner.vlm_backend_labels(config_path)
            _backends = list(_backend_map)
            _conc_defaults = runner.concurrency_defaults(config_path)

            def _backend_status() -> dict:
                """{预设代号: True/False}(探活一次,给下拉标可用性用)。"""
                return {name: ("在线" in state)
                        for name, state, _ in _probe_backends(config_path, probe_timeout)}

            def _backend_choices(status: dict | None = None) -> list:
                """下拉选项:未检测时只给名字;检测过就把状态缀在后面。"""
                return _backend_options(_backend_map, status)

            def _backend_code(choice: str):
                """下拉选中项 → 预设代号(容忍带「· 可用/暂不可用」后缀)。"""
                return _backend_map.get(_backend_label_of(choice))

            def _tk_view(msg: str = ""):
                """当前任务(没有在跑的就显示最近一个)→ 状态条 + 日志尾部 + 提示。"""
                st = (runner.active_run(_runs_root)
                      or next(iter(runner.list_runs(_runs_root, limit=1)), None))
                if not st:
                    return runner.status_html(None), "", msg
                logtxt = runner.tail_log(_runs_root, st["run_id"])
                # 累积进度(2026-08-13 用户):跑完的阶段留在原地,新阶段追加一根条 ——
                # 只画最后一条时,阶段一换就归零重来,等着的人看不出"已经过了几关"
                return (runner.status_html(st, runner.parse_progress_all(logtxt)),
                        logtxt, msg)

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

            with gr.Tab("任务台", id="console"):
                # ① 控制面板在上
                with gr.Tabs():
                    with gr.Tab("跑质检"):
                        with gr.Row():
                            # 多选(2026-08-13 用户):此前只有"一个"或"父目录下全部"
                            # 两档,想跑其中三个得排三轮队(任务台同一时刻只许一个
                            # 任务在跑)。多选 = 一次点击顺序跑选中的这几个。
                            rn_ds = gr.Dropdown(choices=runner.list_datasets(_data_root),
                                                label="数据集", scale=4,
                                                multiselect=True, interactive=True)
                            rn_out = gr.Textbox(label="交付名", scale=4,
                                                placeholder="给这次结果起个名字")
                        rn_out_hint = gr.Markdown(OUT_NAME_HINT_ONE)
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
                                rn_backend = gr.Dropdown(choices=_backends, label="模型服务")
                                rn_probe = gr.Button("检测可用性", size="sm", scale=0)
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

                    with gr.Tab("执行人工裁决"):
                        with gr.Row():
                            rj_deliv = gr.Dropdown(
                                choices=delivery_choices(delivery, choices),
                                value=choices[0], label="要执行的交付", scale=4)
                            with gr.Column(scale=2):
                                rj_backend = gr.Dropdown(choices=_backends, label="模型服务")
                                rj_probe = gr.Button("检测可用性", size="sm", scale=0)
                        # 裁决作用在**某一次跑批**上(它改的是那一次的三件套与交付
                        # 数据集);裁决记录本身住在交付根的 human-decisions/,跨跑批
                        # 累积。默认预选 latest 那次 = 省一次点击,不是"该选这份"。
                        rj_run = gr.Dropdown(choices=run_choices(choices[0]),
                                             value=resolve_run(choices[0]),
                                             label="哪一次运行", interactive=True)
                        rj_src = gr.Markdown()
                        rj_ds = gr.Dropdown(choices=runner.list_datasets(_data_root),
                                            label="原始数据集", visible=False,
                                            interactive=True)
                        with gr.Accordion("更多设置", open=False):
                            rj_cfg = gr.Textbox(label="配置文件(留空=默认)",
                                                placeholder=f"{runner.TOS_ROOT}/…/site.yaml")
                        rj_ok = gr.Checkbox(label="我确认:这会改写该交付的内容")
                        with gr.Row():
                            rj_go = gr.Button("执行裁决", variant="primary", scale=0)

                # ② 任务与日志在下(合成一块,分子页签:当前任务 / 历史)
                gr.Markdown("### 任务与日志")
                with gr.Tabs():
                    with gr.Tab("当前任务"):
                        tk_status = gr.HTML()
                        tk_msg = gr.Markdown()
                        tk_log = gr.Textbox(label="日志", lines=14, max_lines=14,
                                            interactive=False, autoscroll=True,
                                            elem_classes=["mono-log"])
                        with gr.Row():
                            tk_refresh = gr.Button("刷新", scale=0, size="sm")
                            tk_stop = gr.Button("停止", variant="stop", scale=0,
                                                size="sm")
                    with gr.Tab("历史"):
                        hi_table = gr.Dataframe(headers=runner.HISTORY_HEADERS,
                                                interactive=False, wrap=True)
                        hi_pick = gr.Markdown()
                        hi_log = gr.Textbox(label="这次任务的日志", lines=14,
                                            max_lines=14, interactive=False,
                                            elem_classes=["mono-log"])
                _tk_outs = [tk_status, tk_log, tk_msg]

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

                _mode_ins = [rn_mode, rn_pick, rn_how]
                _mode_outs = [rn_pick, rn_how, rn_c_ep, rn_c_fr, rn_c_cap,
                              rn_conc_note]
                for _c in _mode_ins:
                    _c.change(_tk_mode, _mode_ins, _mode_outs)

                def _run_go(ds, name, mode, picks, how, max_n, eps, backend,
                            cfg, emb, plots, c_ep, c_fr, c_cap, sets, batch, ro,
                            with_clips=False):
                    if str(backend or '').endswith(BACKEND_BAD):
                        return _tk_view('⚠️ 选中的模型服务当前不可用,换一个,或把那台服务起起来后点「检测可用性」')
                    # 多选下拉默认一个都没选 → 必须先拦(否则空选会一路走到
                    # resolve_under(root, "") = 拿整个数据集根当一份数据跑)
                    _no_ds = runner.dataset_selection_error(ds, bool(batch))
                    if _no_ds:
                        return _tk_view(f"⚠️ {_no_ds}")
                    only = skip = None
                    if mode == CUSTOM_SCAN and picks:
                        joined = ",".join(picks)
                        only, skip = ((joined, None) if how == "只跑选中"
                                      else (None, joined))
                    chosen = runner.picked_datasets(ds)
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
                    try:
                        cfg = runner.resolve_tos_path(cfg) if str(cfg or "").strip() else None
                        # 勾了「跑全部」就忽略下拉的选择(既有行为,别改坏);其余
                        # 情况下选了几个跑几个,选一个 = 一直以来的那条路径
                        if not batch and len(chosen) > 1:
                            # 答了「一起生成」就给**每个需要的**数据集都串上切片:
                            # 追问只问一次,覆盖的是全部选中项(2026-08-14 用户定)
                            clips = (runner.datasets_needing_clips(_data_root, chosen)
                                     if with_clips else [])
                            jobs = runner.build_dataset_jobs(
                                _data_root, _deliv_root, chosen, name or "",
                                clips_root=review_dir, clips_for=clips,
                                config=cfg, **common)
                            return _tk_start(
                                "run", f"质检 {len(jobs)} 个数据集 → {name}"
                                + (f"(含 {len(clips)} 份视频片段)" if clips else ""),
                                jobs=jobs, run_id=run_id)
                        inp = (_data_root if batch else
                               runner.resolve_under(_data_root,
                                                    chosen[0] if chosen else ""))
                        out = runner.resolve_under(_deliv_root, name or "")
                        then_argv = None
                        if with_clips and review_dir and not batch:
                            # 切片作为同一任务的第二步:一条日志、一个结果,用户不必
                            # 知道我们内部跑了两条命令
                            then_argv = runner.build_argv(
                                "review-page", input=inp,
                                output=runner.resolve_under(
                                    review_dir, os.path.basename(out)))
                    except ValueError as e:
                        return _tk_view(f"⚠️ {e}")
                    return _tk_start(
                        "run",
                        f"质检 {os.path.basename(inp)} → {os.path.basename(out)}"
                        + ("(含视频片段)" if then_argv else ""),
                        then_argv=then_argv, run_id=run_id,
                        input=inp, output=out, config=cfg, batch=bool(batch),
                        **common)

                rn_args = gr.State({})          # 预检时把这次的参数存下,答完照原样开跑

                def _run_preflight(ds, name, mode, picks, how, max_n, eps, backend,
                                   cfg, emb, plots, c_ep, c_fr, c_cap, sets,
                                   batch, ro):
                    """开跑前先看数据格式:v3/rrd 要先切片才有画面可看,问一句再决定。

                    只在**真需要**时才问(格式认得出、且本实例配了片段目录),其余一律
                    直接开跑 —— 不拿一个可有可无的对话框挡在客户面前。

                    多选也问(2026-08-14 用户定):此前多选直接跳过不问,于是多选跑出来
                    的 v3/rrd 交付在 Episodes 页全是"没有画面",而用户压根没被问过。
                    做法是**问一次、覆盖全部** —— 统计选中项里有几个需要切片,答"一起
                    生成"就给每个需要的都串上,绝不逐个弹窗。
                    """
                    args = dict(ds=ds, name=name, mode=mode, picks=picks, how=how,
                                max_n=max_n, eps=eps, backend=backend, cfg=cfg,
                                emb=emb, plots=plots, c_ep=c_ep, c_fr=c_fr,
                                c_cap=c_cap, sets=sets, batch=batch, ro=ro)
                    chosen = runner.picked_datasets(ds)
                    # 勾了「跑全部」时下拉本来就被忽略,跑的是根目录下的全部数据集,
                    # 交付目录由 CLI 自己定 —— 那条路径不在本次范围里,维持不问。
                    needing = ([] if batch else
                               runner.datasets_needing_clips(_data_root, chosen))
                    if needing and review_dir:
                        fmt = (runner.dataset_format(
                            runner.resolve_under(_data_root, chosen[0]))
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
                            [rn_ds, rn_out, rn_mode, rn_pick, rn_how, rn_max, rn_eps,
                             rn_backend, rn_cfg, rn_emb, rn_plots, rn_c_ep, rn_c_fr,
                             rn_c_cap, rn_set, rn_batch, rn_ro],
                            _ask_outs)
                rn_yes.click(lambda a: _run_after_ask(a, True), rn_args,
                             _tk_outs + [rn_ask, rn_ask_md])
                rn_no.click(lambda a: _run_after_ask(a, False), rn_args,
                            _tk_outs + [rn_ask, rn_ask_md])

                def _rj_src(path):
                    """选中的那一次跑批里记了原始数据集就自动带出,没记就让用户选。

                    老交付(2026-08-13 之前)没有这个字段 —— 那就老实说没记,让人
                    自己选,绝不按名字猜(同名不同库会重判错数据)。
                    """
                    src = runner.source_dataset_of(path or "")
                    if src:
                        return f"原始数据集:`{src}`", gr.update(visible=False)
                    return "这份交付没记原始数据集,请选:", gr.update(visible=True)

                def _rj_pick(path):
                    """换交付 → 重列它的历次跑批,预选 latest 那次,再带出源数据集。"""
                    rc = run_choices(path or "")
                    sel = resolve_run(path or "")
                    if rc and sel not in [v for _lab, v in rc]:
                        sel = rc[0][1]
                    return (gr.update(choices=rc, value=sel), *_rj_src(sel))

                rj_deliv.input(_rj_pick, rj_deliv, [rj_run, rj_src, rj_ds])
                rj_run.input(_rj_src, rj_run, [rj_src, rj_ds])

                def _rj_go(path, ds, backend, cfg, ok):
                    if str(backend or '').endswith(BACKEND_BAD):
                        return _tk_view('⚠️ 选中的模型服务当前不可用,换一个,或把那台服务起起来后点「检测可用性」')
                    if not ok:
                        return _tk_view("⚠️ 请先勾选确认")
                    src = runner.source_dataset_of(path or "")
                    try:
                        if not src:
                            src = runner.resolve_under(_data_root, ds or "")
                        cfg = runner.resolve_tos_path(cfg) if str(cfg or "").strip() else None
                    except ValueError as e:
                        return _tk_view(f"⚠️ {e}")
                    _deliv_name = os.path.basename(delivery_root_of(path or ""))
                    _run_name = os.path.basename(str(path or "").rstrip("/"))
                    return _tk_start("rejudge",
                                     f"执行裁决 {_deliv_name}"
                                     + (f" / {_run_name}" if _run_name != _deliv_name
                                        else ""),
                                     delivery=path, input=src, config=cfg,
                                     vlm_backend=_backend_code(backend))

                rj_go.click(_rj_go, [rj_run, rj_ds, rj_backend, rj_cfg, rj_ok],
                            _tk_outs)

                def _do_probe(cur_run, cur_rj):
                    """探活一次 → 两个下拉都缀上可用性(它们指的是同一批服务)。

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
                    ch, vals, msg = _reprobe_options(
                        old, _backend_map, _backend_status(), [cur_run, cur_rj])
                    return (gr.update(choices=ch, value=vals[0]),
                            gr.update(choices=ch, value=vals[1]), msg)

                for _btn in (rn_probe, rj_probe):
                    _btn.click(_do_probe, [rn_backend, rj_backend],
                               [rn_backend, rj_backend, tk_msg])

                tk_refresh.click(lambda: _tk_view(""), None, _tk_outs)

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
                    """轮询的那一跳:刷新状态与日志,**当前那句提示原样带回去**。

                    2026-08-13 实测:原来这里传的是空串,于是「还没选数据集」这类
                    校验提示活不过两秒就被下一跳抹掉 —— 用户点了按钮什么也没看见,
                    和静默失败没有区别。提示由下一次真正的动作覆盖,不由计时器清。
                    """
                    return _tk_view(msg or "")

                if hasattr(gr, "Timer"):
                    gr.Timer(2.0).tick(_tk_tick, tk_msg, _tk_outs)
                    gr.Timer(10.0).tick(_hi_rows, None, hi_table)
                app.load(lambda: _tk_view(""), None, _tk_outs)
                app.load(_hi_rows, None, hi_table)
            # 报告页装在**可提前收口**的嵌套栈里:它的内容有六百行,不可能塞进
            # 一个 with 缩进;而「终端」要排在它右边,就必须在它收口之后再建。
            # 交给 shell 托管 ⇒ 中途抛异常也不会漏关。
            report_ctx = shell.enter_context(contextlib.ExitStack())
            report_tab = report_ctx.enter_context(gr.Tab("质检报告", id="report"))
            with gr.Row():
                # 文案一句话就够(2026-08-13 用户:"这种文字根本不应该给客户看")。
                # 「重新加载」按钮已撤:切到本页就重扫一次盘(见下面的 select),
                # 让用户自己点刷新是上个时代的做法。
                picker = gr.Dropdown(choices=delivery_choices(delivery, choices),
                                     value=choices[0], label="交付",
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
            #    此前分歧裁决藏在技能画像页底部,而任务成败弃权只能在 Episodes 页
            #    看见"待裁决"三个字、没有任何下手的地方。位置放在 Episodes 与
            #    技能画像之间 = 看完数据紧接着做决定的自然工序。
            with gr.Tab("人工裁决"):
                # 页签已写「人工裁决」,页内不再重复大标题(2026-08-07 用户定)
                gr.HTML(_ADJ_GUIDE_HTML)

                gr.HTML(_adj_section_html("1", AUDIT_TERM,
                                          "数据自带的标注 vs 系统看画面写的描述",
                                          "#FF7D00", "#D25F00"))
                au_table = gr.Dataframe(headers=AUDIT_HEADERS,
                                        label=f"{AUDIT_TERM}的条目(重点档排最前;"
                                              "点任意一行 → 下方裁决卡片跳到该条)",
                                        interactive=False, elem_id="audit-queue",
                                        max_height=420,     # 容器内滚动(表头吸顶),条数多不挤爆页面
                                        wrap=True,          # 长文本换行显示全文(原始标注/分歧说明不截断)
                                        column_widths=["7%", "6%", "9%", "24%", "19%",
                                                       "9%", "18%", "8%"])
                # ── 裁决标注分歧(逐条翻页卡片,参考 7862 人工审片的形态):
                #    看视频 → 对照原始标注/建议描述 → 三按钮直接裁决。
                #    UI 只记录裁决(details/label_decisions.csv);
                #    重判 = 命令行 curation rejudge(人工确认是自产自证的断路器)。
                # 默认展开(2026-08-05 用户定:折叠着没人知道能点开)——有分歧队列
                # 的交付,裁决面板就是这个页签的主工作区,不该藏
                with gr.Accordion(f"逐条裁决「{AUDIT_TERM}」(记草稿,可随时改)",
                                  open=True):
                    au_idx = gr.State(0)
                    with gr.Row():
                        au_prev = gr.Button("← 上一条", scale=1)
                        au_pos = gr.Markdown("", elem_id="au-pos")
                        au_next = gr.Button("下一条 →", scale=1)
                    au_info = gr.Markdown()
                    # 「同时播放」按钮**单独占一行**,不进视频那一行:塞进去会把三个
                    # 播放器挤窄(用户 2026-08-14:"视频 window 大小别变")。按钮靠
                    # elem_id 找视频,不需要把两者包进同一个容器 —— 少一层容器 =
                    # 视频区的宽度与间距一个像素都不动。
                    gr.HTML(play_all_button_html("三路机位从头一起播,播完即停(不循环)",
                                                 zone="au-vids"))
                    with gr.Row(elem_id="au-vids"):
                        au_vids = [gr.Video(label=f"机位 {i+1}", interactive=False,
                                            autoplay=False, loop=False, scale=1)
                                   for i in range(3)]
                    au_origlab = gr.Textbox(label="原始标注(只读)", interactive=False)
                    au_newlab = gr.Textbox(label="修正后标注(可编辑;预填 VLM 建议描述,"
                                                 "仅「采纳改标」使用)")
                    au_note = gr.Textbox(label="备注(可选)")
                    with gr.Row():
                        # 三键同色待选、选中变色(见 _au_btns);语义靠图标与文字
                        au_adopt = gr.Button("✅ 采纳改标", variant="secondary")
                        au_keep = gr.Button("↩️ 维持原标注", variant="secondary")
                        au_drop = gr.Button("🗑 弃用该条", variant="secondary")
                    au_status = gr.Markdown()

                # ── ② 任务成败弃权:系统诚实说"我判不了"的条目。这里**不重判**,
                #    人看视频直接给结论(判成功/判失败/搁置),rejudge 只负责搬交付。
                gr.HTML(_adj_section_html("2", "任务成败弃权",
                                          "系统弃权,需人工审核",
                                          "#165DFF", "#165DFF"))
                tv_hint = gr.Markdown()
                tv_table = gr.Dataframe(headers=TASK_REVIEW_HEADERS,
                                        label="任务成败待裁决队列(点任意一行 → "
                                              "下方裁决卡片跳到该条)",
                                        interactive=False, elem_id="task-queue",
                                        max_height=420, wrap=True,
                                        column_widths=["7%", "11%", "10%", "38%",
                                                       "22%", "12%"])
                with gr.Accordion("裁决任务成败(记草稿,可随时改)", open=True):
                    tv_idx = gr.State(0)
                    with gr.Row():
                        tv_prev = gr.Button("← 上一条", scale=1)
                        tv_pos = gr.Markdown("", elem_id="tv-pos")
                        tv_next = gr.Button("下一条 →", scale=1)
                    tv_info = gr.Markdown()
                    tv_readings = gr.Markdown()
                    gr.HTML(play_all_button_html("三路机位从头一起播,播完即停(不循环)",
                                                 zone="tv-vids"))
                    with gr.Row(elem_id="tv-vids"):
                        tv_vids = [gr.Video(label=f"机位 {i+1}", interactive=False,
                                            autoplay=False, loop=False, scale=1)
                                   for i in range(3)]
                    tv_note = gr.Textbox(label="备注(可选;写清依据,复盘时是唯一线索)")
                    with gr.Row():
                        # 顺序与 VERDICT_CHOICES 严格对应(_tv_btns 按序点亮)
                        tv_pass = gr.Button("✅ 判成功", variant="secondary")
                        tv_fail = gr.Button("❌ 判失败", variant="secondary")
                        tv_hold = gr.Button("⏸ 搁置", variant="secondary")
                    tv_status = gr.Markdown()

                # ── ③ 被拒复议(2026-08-11):任务成败判定**杀掉**的条目在这里
                #    可看、可捞回 —— 判定从"拿不准就转人工"升级成"证据够就杀"
                #    之后,这一区就是保险丝。整区在没有可复议条目时**不渲染**
                #    (visible=False):空区块占着位置只会让人以为自己漏看了什么。
                #    物理与结构硬门拒掉的条目进不来(准入判据在 decisions.py)。
                with gr.Column(visible=False) as ap_block:
                    gr.HTML(_adj_section_html("3", "被拒复议",
                                              "系统判为任务未完成的条目,人看完可捞回",
                                              "#CB272D", "#CB272D"))
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
                        # 视频走 Episodes 页那条来源链(审片站 → 交付数据集 → 提示语),
                        # 不另起一套:被拒条目往往没有裁决片段,只有那条链找得到画面。
                        ap_video = gr.HTML()
                        ap_note = gr.Textbox(label="备注(可选;写清依据,复盘时是唯一线索)")
                        with gr.Row():
                            # 顺序与 APPEAL_CHOICES 严格对应(_ap_btns 按序点亮)
                            ap_keep = gr.Button("❌ 维持拒绝", variant="secondary")
                            ap_back = gr.Button("🛟 捞回(判为可用)", variant="secondary")
                        ap_status = gr.Markdown()

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

            outs = [state, ov_md, ov_table, ov_note, ov_cfg, ep_bucket, *_ep_list_outs,
                    sk_html, sk_table, sk_audit_note,
                    au_table,
                    au_idx, au_pos, au_info, au_origlab, au_newlab, au_note, *au_vids,
                    au_adopt, au_keep, au_drop,
                    tv_hint, tv_table,
                    tv_idx, tv_pos, tv_info, tv_readings, *tv_vids,
                    tv_pass, tv_fail, tv_hold,
                    ap_block, ap_hint, ap_table, *_ap_outs,
                    *_ep_outs, dt_pick, dt_note, dt_table, vd_note, vd_table,
                    sy_filter, *_sy_outs, sy_conclusion, sy_health,
                    tl_show, tl_sort, tl_note, tl_html,
                    perf_backend, perf_env, perf_note, perf_table, perf_bar]

            def _pick_delivery(path):
                """换交付 → 重列该交付的历次跑批 + 打开其中一次。

                默认打开 latest 记的那次(见 resolve_run):只是省一次点击。老布局的
                交付(三件套直接在交付目录里)在这里只会得到一项,照常打得开 —— 这条
                是 2026-08-14 改布局时的硬要求,别人的旧交付不许因为我们改了目录形状
                就打不开。
                """
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

            def _picker_tick(cur):
                """重扫交付列表(**只换选项、不重载内容**)。

                只换选项是刻意的:内容重载会与用户当下的点击并发打架 —— 交付下拉的
                联动就为此修过一次(6bb28b5:两批更新改同一排组件,翻页按钮偶尔被
                冲掉)。新交付自动出现在列表里,点它即可查看,不必再点什么"刷新"。
                手输的自定义路径原样保留在选项里,不会被刷掉。
                """
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
            report_tab.select(_picker_tick, picker, picker)
            if hasattr(gr, "Timer"):
                gr.Timer(10.0).tick(_picker_tick, picker, picker)

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

            # ── ① 标注分歧:翻页 / 点行跳转 / 三键裁决 ──
            _au_outs = [au_idx, au_pos, au_info, au_origlab, au_newlab, au_note, *au_vids,
                        au_adopt, au_keep, au_drop]

            au_prev.click(lambda m, i: _au_render(m, (i or 0) - 1),
                          [state, au_idx], _au_outs)
            au_next.click(lambda m, i: _au_render(m, (i or 0) + 1),
                          [state, au_idx], _au_outs)

            def _au_jump(m, evt):
                """点队列表任意一行 → 卡片跳到该条(挑着裁/回头重看都靠它)。"""
                return _au_render(m, evt.index[0])

            def _tv_jump(m, evt):
                return _tv_render(m, evt.index[0])

            # gradio 靠注解识别"要注入 SelectData";本文件开了 future annotations,
            # 字符串注解会在模块全局被 eval(gr 是函数内导入)→ NameError。
            # 塞真实类对象绕开字符串求值。
            _au_jump.__annotations__ = {"evt": gr.SelectData}
            _tv_jump.__annotations__ = {"evt": gr.SelectData}

            au_table.select(_au_jump, state, _au_outs)

            def _au_decide(m, idx, newlab, note, decision):
                q = (m or {}).get("audit_queue") or []
                if not q:
                    return ("⚠️ 无条目可裁决", gr.update(), gr.update(),
                            *[gr.update()] * 3, gr.update(), gr.update())
                a = q[(idx or 0) % len(q)]
                msg = record_label_decision(m["path"], a.get("id", ""), decision,
                                            newlab or "", note or "")
                if msg.startswith("✅"):
                    msg = msg.replace("✅ 已记录:", "✅ 已记录(草稿,可随时改判):")
                    btns = _au_btns(decision)                # 记录成功才点亮所选键
                else:
                    btns = [gr.update()] * 3                 # 校验失败:按钮不动
                info = _au_render(m, idx)[2]                 # 卡片头同步"已裁决"状态
                # 顺带刷新两处"还剩几条没裁"的提示(裁完最后一条,催办语就该消失)
                return (msg, gr.update(value=audit_rows(m)), info, *btns,
                        task_review_hint_md(m), audit_note_md(m))

            _dec_outs = [au_status, au_table, au_info, au_adopt, au_keep, au_drop,
                         tv_hint, sk_audit_note]
            au_adopt.click(lambda m, i, nl, nt: _au_decide(m, i, nl, nt, "采纳建议改标"),
                           [state, au_idx, au_newlab, au_note], _dec_outs)
            au_keep.click(lambda m, i, nl, nt: _au_decide(m, i, nl, nt, "维持原标注"),
                          [state, au_idx, au_newlab, au_note], _dec_outs)
            au_drop.click(lambda m, i, nl, nt: _au_decide(m, i, nl, nt, "弃用该条"),
                          [state, au_idx, au_newlab, au_note], _dec_outs)

            # ── ② 任务成败弃权:同一套形态(翻页 / 点行跳转 / 三键裁决)──
            _tv_outs = [tv_idx, tv_pos, tv_info, tv_readings, *tv_vids,
                        tv_pass, tv_fail, tv_hold]

            tv_prev.click(lambda m, i: _tv_render(m, (i or 0) - 1),
                          [state, tv_idx], _tv_outs)
            tv_next.click(lambda m, i: _tv_render(m, (i or 0) + 1),
                          [state, tv_idx], _tv_outs)
            tv_table.select(_tv_jump, state, _tv_outs)

            def _tv_decide(m, idx, note, verdict):
                q = (m or {}).get("task_review") or []
                if not q:
                    return ("⚠️ 无条目可裁决", gr.update(), gr.update(),
                            *[gr.update()] * 3)
                t = q[(idx or 0) % len(q)]
                msg = record_task_verdict(m["path"], t.get("id", ""), verdict,
                                          note or "")
                if msg.startswith("✅"):
                    msg = msg.replace("✅ 已记录:", "✅ 已记录(草稿,可随时改判):")
                    btns = _tv_btns(verdict)
                else:
                    btns = [gr.update()] * 3
                info = _tv_render(m, idx)[2]
                return msg, gr.update(value=task_review_rows(m)), info, *btns

            _tv_dec_outs = [tv_status, tv_table, tv_info, tv_pass, tv_fail, tv_hold]
            tv_pass.click(lambda m, i, nt: _tv_decide(m, i, nt, "判成功"),
                          [state, tv_idx, tv_note], _tv_dec_outs)
            tv_fail.click(lambda m, i, nt: _tv_decide(m, i, nt, "判失败"),
                          [state, tv_idx, tv_note], _tv_dec_outs)
            tv_hold.click(lambda m, i, nt: _tv_decide(m, i, nt, "搁置"),
                          [state, tv_idx, tv_note], _tv_dec_outs)

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
                msg = record_reject_appeal(m["path"], a.get("id", ""), appeal,
                                           note or "")
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


def create_asgi_app(delivery: str, config_path: str | None = None,
                    probe_timeout: float = 5.0, terminal: bool = False,
                    review_dir: str | None = None, data_root: str | None = None):
    """→ FastAPI 应用(gradio 挂在 `/`,自定义路由挂在它前面)。

    为什么不再用 `blocks.launch()`:launch() 自己造 FastAPI + 自己跑 uvicorn,拿不到
    那个 app 的引用,也就挂不上 `/ws/term`。改成我们造 app、gradio 往上挂,单端口
    同时提供 UI + 终端 + 静态资产 + 健康检查。

    路由注册顺序有讲究:starlette 按注册顺序匹配,gradio 的 `/` 是 catch-all mount,
    必须最后挂,否则它会吃掉 `/ws/term` 和 `/term-static/*`。
    """
    import gradio as gr
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    from . import auth

    blocks = build_app(delivery, config_path, probe_timeout, terminal=terminal,
                       review_dir=review_dir, data_root=data_root)
    api = FastAPI()

    # 探针端点(鉴权豁免,见 auth.EXEMPT_PATHS):k8s readinessProbe 现在指 /(整页
    # 渲染),配了 Basic 之后会被 401 打红 → 留一个不设防的轻量端点给探针用。
    @api.get("/healthz", response_class=PlainTextResponse)
    def _healthz() -> str:                     # noqa: ANN202
        return "ok"

    if terminal:
        from . import terminal as term
        api.mount("/term-static", StaticFiles(directory=STATIC_DIR), name="term-static")
        api.add_api_websocket_route("/ws/term", term.term_endpoint)
        log.info("终端:已开启(/ws/term,shell=%s,cwd=%s)",
                 term.resolve_shell(), term.resolve_workdir())

    # 静态审片站(curation review-page 的产出):挂在 gradio catch-all 之前。
    # html=True → /review 直接出 index.html;目录缺失只警告不拦启动(先起 UI 后生成站点
    # 是合法顺序,StaticFiles 每次请求现场解析路径,后补的目录立即可用)。
    if review_dir:
        if not os.path.isdir(review_dir):
            log.warning("审片站目录尚不存在:%s(生成后无需重启即可访问)", review_dir)
            os.makedirs(review_dir, exist_ok=True)
        api.mount("/review", StaticFiles(directory=review_dir, html=True), name="review")
        log.info("审片站:已挂 /review → %s", review_dir)

    auth.apply(api, terminal_enabled=terminal)
    # allowed_paths:允许页面直读交付目录下的证据文件(gradio 默认只许临时目录);
    # 审片站目录同样要放行——Episodes 页的视频第一来源就在那儿,不放行会 403。
    allowed = [delivery] + ([review_dir] if review_dir else [])
    # footer_links=[]:整排页脚(Use via API / Built with Gradio / Settings)去掉。
    # 头一个会把本服务的接口文档摆给任何打开页面的人看,另两个对客户毫无用处。
    # 用 gradio 自己的开关而不是 CSS 藏 —— 藏起来的链接照样可点、照样在 DOM 里。
    return gr.mount_gradio_app(api, blocks, path="/", allowed_paths=allowed,
                               footer_links=[], **presentation(terminal))


def launch(delivery: str, config_path: str | None = None, host: str = "0.0.0.0",
           port: int = 7860, probe_timeout: float = 5.0,
           terminal: bool = False, review_dir: str | None = None,
           data_root: str | None = None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_asgi_app(delivery, config_path, probe_timeout, terminal=terminal,
                          review_dir=review_dir, data_root=data_root)
    log.info("质检台 UI 监听 http://%s:%s(交付根目录 %s)", host, port, delivery)
    uvicorn.run(app, host=host, port=port)
