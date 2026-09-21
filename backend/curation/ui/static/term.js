/* 质检台「终端」页签的前端(2026-07-29 U4,自研;同目录其余 js/css 是 vendored 的 xterm.js)。
 *
 * 线协议与同事在运的 lerobot-agent-console 逐字一致(那边 aiohttp,我们 starlette,
 * 只换了服务端框架,帧格式没动):
 *   浏览器 → 服务端:JSON **文本**帧
 *       {"type":"input","data":"<键盘输入>"}
 *       {"type":"resize","cols":<列>,"rows":<行>}
 *   服务端 → 浏览器:**二进制**帧 = PTY 原始字节(直接喂给 xterm)
 *
 * 与参考实现的两点有意差异:
 *   ① 那边是多标签页终端(每个 tab 一条 /ws/term = 一个独立 shell),我们只要一个——
 *      质检台的终端是"看一眼跑批/翻交付目录"的辅助窗,不是主工作区;
 *   ② 那边页面一加载就连;我们**等「终端」页签真被点开**才连(gradio 是 SPA,页签
 *      只是显隐)。理由:不点终端的人(客户看报告)不该在服务端白 fork 一个 bash。
 */
(() => {
  "use strict";

  const SCREEN_ID = "curation-term-screen";
  // 挂载前缀由服务端注入(_terminal_head 里的 window.CURATION_ROOT):UI 住在
  // /curation 这类前缀下时,WS 路由也在前缀下,写死 /ws/term 会打到别的后端。
  const WS_PATH = (window.CURATION_ROOT || "") + "/ws/term";
  const wsURL = (p) =>
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + p;

  const TERM_OPTS = {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    // 2026-07-30 用户反馈"字太淡完全看不清":13.5px + 灰蓝前景在黑底上确实弱。
    // 提字号 + 前景提到近白 + 半粗,黑底终端的可读性以"隔着会议室投影也能看清"为准。
    fontSize: 15,
    fontWeight: "500",
    // 配色跟 Arco 走(2026-08-13):底 = Arco 暗色中性 #17171A、光标与选区用
    // 主色系(arcoblue-6 / -7),前景仍是近白 —— 终端可读性优先,不改浅底。
    theme: { background: "#17171A", foreground: "#F2F3F5", cursor: "#165DFF",
             selectionBackground: "#0E42D2" },
    cursorBlink: true,
    convertEol: true,
  };

  let term = null, fit = null, ws = null, disposed = false;

  function sendResize() {
    if (ws && ws.readyState === 1 && term)
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }

  function refit() {
    if (!fit) return;
    try { fit.fit(); sendResize(); } catch (_) { /* 容器还没尺寸,下一次再说 */ }
  }

  function connect() {
    ws = new WebSocket(wsURL(WS_PATH));
    ws.binaryType = "arraybuffer";
    ws.onopen = () => sendResize();
    ws.onmessage = (e) =>
      term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
    ws.onclose = () => {
      if (disposed) return;
      term.write("\r\n\x1b[33m[终端连接断开 — 正在重连…]\x1b[0m\r\n");
      setTimeout(() => { if (!disposed) connect(); }, 1500);
    };
  }

  function boot(el) {
    term = new Terminal(TERM_OPTS);
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(el);
    term.onData((d) => {
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data: d }));
    });
    // mac 惯用快捷键(2026-08-27 用户要求):Cmd+←/→ 行首行尾、Opt+←/→ 按词跳、
    // Cmd+Backspace 删到行首、Opt+Backspace 删一个词、Cmd+K 清屏。发的是 readline
    // 控制序列,光标动作由 bash 完成。preventDefault 必须做:Cmd+← 的浏览器默认
    // 动作是"后退",不拦会直接离开质检平台页面。
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== "keydown") return true;
      const cmd = ev.metaKey && !ev.ctrlKey && !ev.altKey;
      const opt = ev.altKey && !ev.ctrlKey && !ev.metaKey;
      if (cmd && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault(); term.clear(); return false;
      }
      const seq =
        cmd && ev.key === "ArrowLeft"  ? "\x01"  :   // 行首(Ctrl+A)
        cmd && ev.key === "ArrowRight" ? "\x05"  :   // 行尾(Ctrl+E)
        opt && ev.key === "ArrowLeft"  ? "\x1bb" :   // 左跳一词(ESC b)
        opt && ev.key === "ArrowRight" ? "\x1bf" :   // 右跳一词(ESC f)
        cmd && ev.key === "Backspace"  ? "\x15"  :   // 删到行首(Ctrl+U)
        opt && ev.key === "Backspace"  ? "\x17"  :   // 删一个词(Ctrl+W)
        null;
      if (seq !== null) {
        ev.preventDefault();
        if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data: seq }));
        return false;
      }
      return true;
    });
    connect();
    window.addEventListener("resize", refit);
    // 页签来回切:容器宽度 0 ↔ N 的变化由 ResizeObserver 捕获 → 重新 fit + 上报尺寸
    if (window.ResizeObserver) new ResizeObserver(refit).observe(el);
    setTimeout(refit, 0);
    window.addEventListener("beforeunload", () => {
      disposed = true;
      try { ws && ws.close(); } catch (_) {}
    });
  }

  // gradio 的 DOM 是前端异步渲染的,拿不到"挂载完成"事件 → 轮询等容器出现;
  // 且要等它**可见**(clientWidth > 0,即用户点开了「终端」页签)才起 shell。
  const timer = setInterval(() => {
    const el = document.getElementById(SCREEN_ID);
    if (!el || !el.clientWidth) return;
    clearInterval(timer);
    boot(el);
  }, 300);
})();
