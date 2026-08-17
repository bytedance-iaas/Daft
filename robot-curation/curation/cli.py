"""curation 命令行入口。

用法(P3.6 漏斗装配后接通):
    python -m curation.cli run --config default.yaml --input <数据集目录> --output <交付目录>

`--output` 是**交付目录**,一次跑批的结果落在 `<交付目录>/<时间戳>/`(2026-08-14
布局变更,理由见 curation/delivery.py);清理历次跑批走 `curation prune`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Daft 引擎自带的终端动画(🗡️🐟 进度条)与 Query ID 行默认关闭(2026-07-28 用户反馈:
# 交互式终端里它们把本产品的语义化进度行搅成花屏;非 TTY 下 Daft 本就不画,所以
# 一发直达从未见过)。用 setdefault:要看引擎内部细节,环境变量置 1 即可强制打开。
os.environ.setdefault("DAFT_PROGRESS_BAR", "0")
os.environ.setdefault("DAFT_SHOW_QUERY_ID", "0")


def _env_flag(name: str) -> bool:
    """布尔开关的环境变量缺省值:`CURATION_TERMINAL=1` 与命令行 `--terminal` 等价。

    "假"的写法容忍 0/false/no/off/空(YAML 里手滑写成 "false" 是最常见的一脚)。
    """
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="curation",
        description="机器人数据 curation 流水线:质检/清洗/组织,交付干净数据集+质检报告+技能画像",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="端到端跑一遍 curation")
    run.add_argument("--config", default=None, help="流水线 YAML 配置(缺省用 default.yaml)")
    run.add_argument("--input", required=True, help="输入数据集目录(LeRobot 格式)")
    run.add_argument("--output", required=True,
                     help="交付目录;本次结果落在 <交付目录>/<时间戳>/ 里"
                          "(每次跑批各进各的子目录,永不覆盖上一次)")
    run.add_argument("--embodiment-id", default=None,
                     help="人工指定机器人型号(数据集 robot_type 缺失/unknown 时)")
    run.add_argument("--max-episodes", type=int, default=None, help="只处理前 N 条(调试)")
    run.add_argument("--episodes", default=None, metavar="表达式",
                     help="只跑指定 episode(调试/复现单条):单条 34;多条 34,56,78;"
                          "区间 10-20;可混用 3,10-12。与 --max-episodes 同时给时先选本参数再截断")
    run.add_argument("--only", default=None,
                     help="只跑这些模块(逗号分隔,如 visual_quality,motion_quality;"
                          "含数据集级模块 skill_profile(技能画像)/dedup(精确去重))")
    run.add_argument("--skip", default=None, help="跳过这些模块(逗号分隔,与 --only 互斥)")
    run.add_argument("--run-name", default=None, metavar="子目录名",
                     help="本次跑批的子目录名(缺省按本地时间生成 YYYYMMDD-HHMMSS);"
                          "任务台发起时传的是那次任务的编号,好让结果与任务对得上")
    run.add_argument("--batch", action="store_true",
                     help="批处理:--input 指向含多个数据集的父目录,"
                          "逐个处理到 --output/<数据集名>/<时间戳>/")
    run.add_argument("--lite", action="store_true",
                     help="精简版:跳过 VLM 环节(任务判定/caption画像),不碰 GPU,秒级出报告")
    run.add_argument("--set", action="append", dest="set_overrides", metavar="路径=值",
                     help="临时覆盖单个配置值(可重复),如 --set pipeline.sync_plots=all "
                          "--set checks.visual_quality.params.blur_ref_var=80;"
                          "免为一个开关复制整份 yaml")
    run.add_argument("--vlm-endpoint", default=os.environ.get("CURATION_VLM_ENDPOINT"),
                     metavar="URL",
                     help="VLM 服务直连地址(OpenAI 兼容,如 http://10.0.0.5:8000/v1);"
                          "免起别名的正门。缺省读环境变量 CURATION_VLM_ENDPOINT")
    run.add_argument("--vlm-model", default=os.environ.get("CURATION_VLM_MODEL"),
                     metavar="模型名",
                     help="VLM 模型名(如 Qwen/Qwen2.5-VL-32B-Instruct)。"
                          "缺省读环境变量 CURATION_VLM_MODEL")
    run.add_argument("--vlm-api-key-env", default=os.environ.get("CURATION_VLM_API_KEY_ENV"),
                     metavar="环境变量名",
                     help="存放 API Key 的环境变量**名**(托管端点用;密钥本身绝不进命令行)。"
                          "缺省读 CURATION_VLM_API_KEY_ENV")
    run.add_argument("--vlm-backend", default=None, metavar="预设名",
                     help="一键切换 VLM 后端(端点/模型/密钥三元组整组换),预设定义在 "
                          "default.yaml 的 vlm_backends 段(如 ark / h20-8b);"
                          "仍可 --set checks.task_success.vlm.* 微调(--set 后应用,赢)")
    run.add_argument("--report-only", action="store_true",
                     help="只出报告,不导出数据集(单模块快查时省去重编码视频的时间)")

    rj = sub.add_parser("rejudge",
                        help="按人工裁决更新交付(三条线一起消化):标注分歧"
                             "(human-decisions/label_decisions.csv,采纳改标的条目用新标注"
                             "重跑任务成败检测)+ 任务成败裁决"
                             "(human-decisions/task_verdicts.csv,不重判,以人的结论为准)"
                             "+ 被拒复议(human-decisions/reject_appeals.csv,人判可用的"
                             "条目从拒绝翻回通过并补回交付数据集)")
    rj.add_argument("--delivery", required=True,
                    help="要更新的那一次跑批目录(<交付目录>/<时间戳>/);"
                         "只给交付目录时按 latest 记的那次执行")
    rj.add_argument("--input", required=True, help="原始数据集目录(重判需重新解码视频)")
    rj.add_argument("--config", default=None, help="流水线 YAML(缺省 default.yaml,须与原 run 一致)")
    rj.add_argument("--vlm-backend", default=None, metavar="预设名",
                    help="重判用的 VLM 后端预设(同 run,如 ark / h20-32b);缺省跟随配置")

    rpf = sub.add_parser("reprofile",
                         help="按「标注优先」方针重算一份交付的技能画像:归类文本改为"
                              "原始标注优先(instruction 优先,无标注才用自产 caption),"
                              "对交付里**已有的**技能体系纯文本重新分配。"
                              "不重跑任何 VLM caption、不重新归纳体系、"
                              "不碰任何成败判定结论;连跑两次第二次应报 0 条变化")
    rpf.add_argument("--delivery", required=True,
                     help="要重算的那一次跑批目录(<交付目录>/<时间戳>/);"
                          "只给交付目录时按 latest 记的那次执行(同 rejudge)")
    rpf.add_argument("--config", default=None,
                     help="流水线 YAML(仅用于「归不进体系时问一次 LLM 补漏」;"
                          "没配 VLM 端点就诚实留「未归类」)")
    rpf.add_argument("--vlm-backend", default=None, metavar="预设名",
                     help="补漏用的 LLM 后端预设(同 run,如 ark / h20-32b);缺省跟随配置")

    rp = sub.add_parser("review-page",
                        help="生成静态审片站(索引一屏列全量 episode + 逐条多路视频页),"
                             "落盘持久;由 UI 的 /review 路由服务(pod 重启不丢)")
    rp.add_argument("--input", required=True,
                    help="数据集目录(LeRobot 格式,或 rerun 的 .rrd 目录;自动识别)")
    rp.add_argument("--output", required=True,
                    help="产出目录(建议持久盘,如 /mnt/tos/review/<名字>)")
    rp.add_argument("--episodes", default=None, metavar="表达式",
                    help="只做指定 episode(同 run:34,56 或 10-20,可混用);缺省全量")
    rp.add_argument("--max-episodes", type=int, default=None, help="只做前 N 条")
    rp.add_argument("--title", default=None, help="页面标题(缺省用数据集目录名)")
    rp.add_argument("--rrd-fps", type=float, default=None, metavar="帧率",
                    help="仅 RRD 输入:采集帧率。RRD 里没有时间信息时必须给"
                         "(如 so101 用 30),数据自带帧时间戳时(如 bridge)不用管。"
                         "等价于 run 的 --set ingest.rrd_fps")

    pr = sub.add_parser("prune",
                        help="列出一份交付下的历次跑批(时间/条数/占用),"
                             "并按需删掉旧的几次;**默认只列不删**")
    pr.add_argument("delivery", help="交付目录(如 /mnt/tos/deliveries/droid-200-full)")
    pr.add_argument("--keep-latest", type=int, default=None, metavar="N",
                    help="要删的是哪几次:留最新的 N 次,更早的删掉。"
                         "latest 记的那次永远保留。不给这个参数就只列出、不删任何东西")
    pr.add_argument("--yes", action="store_true",
                    help="真的删(不加就只打印将要删什么;必须同时给 --keep-latest)")

    be = sub.add_parser("backends", help="一次列出全部 VLM 后端预设的在线状态与服务端模型")
    be.add_argument("--config", default=None,
                    help="站点配置(叠加到出厂默认;缺省读环境变量 CURATION_CONFIG)")
    be.add_argument("--timeout", type=float, default=5.0, help="单端点探活超时秒数")

    ui = sub.add_parser("ui", help="质检台 Web UI(Gradio):只读渲染交付目录")
    ui.add_argument("--delivery", required=True,
                    help="交付目录(或含多份交付的父目录,如 /mnt/tos/deliveries)")
    ui.add_argument("--config", default=None,
                    help="站点配置(仅供「后端状态」tab 探活;缺省读 CURATION_CONFIG)")
    ui.add_argument("--host", default="0.0.0.0", help="监听地址(默认 0.0.0.0,便于 port-forward)")
    ui.add_argument("--port", type=int, default=7860, help="监听端口(默认 7860)")
    ui.add_argument("--timeout", type=float, default=5.0, help="后端探活超时秒数")
    ui.add_argument("--review-dir", default=os.environ.get("CURATION_REVIEW_DIR"),
                    help="静态审片站根目录(curation review-page 的产出);给出后挂 /review "
                         "路由(同端口、Basic 锁覆盖)。也可用环境变量 CURATION_REVIEW_DIR")
    ui.add_argument("--data-root", default=os.environ.get("CURATION_DATA_ROOT"),
                    help="数据集根目录(「任务台」页签只列这个根下的数据集,"
                         "缺省 /mnt/tos/datasets)。⚠️ 面板**不接受任意路径输入**——"
                         "自由路径框等于把整个容器的文件系统开给任何拿到 UI 密码的人。"
                         "也可用环境变量 CURATION_DATA_ROOT")
    ui.add_argument("--terminal", action="store_true", default=_env_flag("CURATION_TERMINAL"),
                    help="打开顶层「终端」页签(内嵌网页终端:xterm.js + 本服务的 "
                         "/ws/term,与 UI 同端口同鉴权)。不传(或 CURATION_TERMINAL 未设)"
                         "则页签不渲染、/ws/term 路由不注册。"
                         "⚠️ 这是一个真 shell,公网暴露前必须配 "
                         "CURATION_UI_USER/CURATION_UI_PASSWORD + 网关鉴权")

    return p


def _cmd_backends(config_path: str | None, timeout: float) -> int:
    """`curation backends`:逐预设探活 + 列服务端模型,表格输出。

    信息型命令,恒返回 0(出厂自带的 self-hosted-example 占位预设注定不可达,
    以退出码报警会让健康的部署天天假红)。要脚本化判活,grep DOWN 即可。
    """
    from .adapters.vlm_client import list_models
    from .pipeline.config import load_config

    cfg = load_config(config_path)
    presets = cfg.get("vlm_backends") or {}
    if not presets:
        print("(配置中没有任何 vlm_backends 预设)")
        return 0
    print(f"{'预设':<24}{'状态':<10}服务端模型")
    for name in sorted(presets):
        p_ = presets[name] or {}
        try:
            ids = list_models(p_.get("endpoint") or "", p_.get("api_key_env"),
                              timeout_s=timeout)
            extra = f" …(共{len(ids)}个)" if len(ids) > 3 else ""
            print(f"{name:<24}{'✅在线':<10}{', '.join(ids[:3])}{extra}")
        except Exception as e:  # noqa: BLE001  单预设失败照常列完其余
            print(f"{name:<24}{'❌不可达':<10}({type(e).__name__})")
    return 0


def _cmd_prune(delivery: str, keep_latest: int | None, yes: bool) -> int:
    """`curation prune`:先把历次跑批摆出来,要删得说清删哪几次 + 加 --yes。

    **绝不做"按最新覆盖旧的"那种自动清理**(用户 2026-08-14 点名否掉):1812 跑
    20 条不能顶掉 0530 跑 200 条的成果 —— 哪一份更值钱只有人知道,系统只负责把
    事实(时间/条数/占用)摆清楚。human-decisions/ 与 latest 永远不在删除范围内。
    """
    import shutil

    from .delivery import is_delivery, prune_plan, size_text

    if not os.path.isdir(delivery):
        print(f"[输入错误] 目录不存在: {delivery}", file=sys.stderr)
        return 2
    if yes and keep_latest is None:
        # 光有 --yes 不说删哪几次 = 没说清就动手,一律拒绝(哪份该留只有人知道)
        print("[输入错误] --yes 必须和 --keep-latest N 一起给:得先说清要删哪几次",
              file=sys.stderr)
        return 2
    if not is_delivery(delivery):
        print(f"[输入错误] {delivery} 不是一份交付(既没有跑批子目录,也没有 passed.json)",
              file=sys.stderr)
        return 2
    try:
        plan = prune_plan(delivery, keep_latest)
    except ValueError as e:
        print(f"[输入错误] {e}", file=sys.stderr)
        return 2
    runs = plan["runs"]
    if not runs:
        print(f"{delivery}:这是 2026-08-14 之前布局的交付(结果直接在交付目录里),"
              "没有可清理的跑批子目录")
        return 0
    doomed = {f["name"] for f in plan["delete"]}
    print(f"{delivery} 共 {len(runs)} 次跑批:")
    for f in runs:
        mark = "  ← 最近一次" if f["is_latest"] else ""
        n = f.get("processed")
        cnt = f"本次处理 {n} 条" if n is not None else "条数未知"
        total = f.get("dataset_total")
        if total not in (None, ""):
            cnt += f"(数据集共 {total} 条)"
        flag = "删除" if f["name"] in doomed else "保留"
        print(f"  [{flag}] {f['name']}  {f.get('at') or ''}  {cnt}  "
              f"{size_text(f['size'])}{mark}")
    if keep_latest is None:
        print("\n(默认只列不删。要删:加 --keep-latest N 看将删哪几次,确认后再加 --yes)")
        return 0
    if not doomed:
        print(f"\n留最新 {keep_latest} 次 → 没有要删的")
        return 0
    freed = size_text(sum(f["size"] for f in plan["delete"]))
    if not yes:
        print(f"\n将删除上面标「删除」的 {len(doomed)} 次跑批,可释放 {freed}。"
              "确认无误就重跑一遍并加 --yes")
        return 0
    for f in plan["delete"]:
        shutil.rmtree(f["path"])
        print(f"  已删 {f['name']}")
    print(f"删除 {len(doomed)} 次跑批,释放 {freed};"
          "人工裁决(human-decisions/)与其余跑批一个字没动")
    return 0


def _list_datasets(parent: str) -> list[str]:
    """父目录下所有有效数据集(--batch 的清单)。

    两种格式各有各的"身份证":LeRobot 看 meta/info.json,RRD 看目录里有没有 *.rrd
    (P5,2026-08-10 补齐 —— 漏斗本身早就两种都吃,只有这份清单还只认 LeRobot,
    于是客户把 rrd 数据集摆进父目录跑 --batch 会得到"没有有效数据集")。
    """
    import os

    from .ingest.rrd_reader import is_rrd_dataset
    return sorted(
        name for name in os.listdir(parent)
        if os.path.exists(os.path.join(parent, name, "meta", "info.json"))
        or is_rrd_dataset(os.path.join(parent, name)))



def _parse_episodes(expr: str | None) -> set[int] | None:
    """"34" / "34,56" / "10-20" / "3,10-12" → {int};非法表达式抛 ValueError 由调用方友好报错。"""
    if not expr:
        return None
    out: set[int] = set()
    for part in str(expr).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):          # 区间(不把负号当分隔)
            lo, hi = part.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
            if hi_i < lo_i:
                raise ValueError(f"区间起止颠倒: {part}")
            out.update(range(lo_i, hi_i + 1))
        else:
            out.add(int(part))
    if not out:
        raise ValueError("未解析出任何 episode 编号")
    return out

class _TolerantStream:
    """包一层的标准输出/错误:**写日志失败绝不许弄死一次跑批**。

    2026-08-13 实测的死法:任务日志落在 TOS 的 FSX 挂载上,那个 fd 用着用着就坏
    (Stale file handle),于是管道里一句普通的 `print(...)` 抛 OSError、没人接,
    一整批 200 条在最后写明细的阶段整个崩掉 —— 而 traceback 又写不进同一个坏文件,
    现场什么都没留下(退出码 120 = CPython 退出时刷 stdout 失败)。

    跑批的价值在数据,不在日志。日志写不动就丢掉这几行,继续把活干完。
    (日志已改为先写容器本地盘、结束再归档,本层是第二道保险。)
    """

    def __init__(self, stream):
        self._s = stream

    def write(self, data):
        try:
            return self._s.write(data)
        except (OSError, ValueError):      # ValueError = 文件已被关闭
            return len(data)

    def flush(self):
        try:
            self._s.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._s, name)


def _tolerate_broken_log() -> None:
    """把 stdout/stderr 换成容错版。只在 CLI 入口调一次,库代码不受影响。"""
    sys.stdout = _TolerantStream(sys.stdout)
    sys.stderr = _TolerantStream(sys.stderr)


def main(argv: list[str] | None = None) -> int:
    import os
    _tolerate_broken_log()
    args = build_parser().parse_args(argv)
    if args.command == "review-page":
        from .export.review_page import build_review_page
        from .ingest.rrd_reader import cleanup_video_cache, is_rrd_dataset
        eps = _parse_episodes(args.episodes)
        # 输入格式嗅探(P5,2026-08-10):与 run 同一套判据。审片站只要
        # episode_id/标注/视频指针三样,RRD 走**轻量元数据**读法就够——它不做
        # schema 校验,也就不用逼用户为了看片先报 --embodiment(RRD 无 robot_type)。
        if is_rrd_dataset(args.input):
            from .ingest.lerobot_reader import NotADatasetError
            from .ingest.rrd_reader import read_rrd_meta
            try:
                rows = read_rrd_meta(args.input, episode_indices=eps, fps=args.rrd_fps)
            except NotADatasetError as e:
                # reader 给的出路是 run 的 `--set`(它不知道是谁在调它);这条命令的
                # 出路叫 --rrd-fps,补一句免得用户照抄一条跑不通的命令
                print(f"[输入错误] {e}\n"
                      f"  (review-page 的写法:--rrd-fps 30)", file=sys.stderr)
                return 2
        else:
            from .ingest.lerobot_reader import read_lerobot_rows
            rows = read_lerobot_rows(args.input, episode_indices=eps, validate=True)
        if args.max_episodes:
            rows = rows[: args.max_episodes]
        title = args.title or os.path.basename(os.path.normpath(args.input))
        print(f"[review-page] {len(rows)} 条 → {args.output}(已有片段跳过,幂等)", flush=True)
        done = [0]

        def _tick():
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(rows):
                print(f"[review-page] {done[0]}/{len(rows)}", flush=True)

        try:
            n = build_review_page(rows, args.output, title=title, on_progress=_tick,
                                  source_dataset=args.input)
        finally:
            # RRD 解出的临时 mp4 只是切片的原料,站点生成完就该消失(几百条能占几个 GB)
            cleanup_video_cache(args.input)
        print(f"[review-page] 完成:新编码 {n} 段;入口 {args.output}/index.html", flush=True)
        return 0

    if args.command == "prune":
        return _cmd_prune(args.delivery, args.keep_latest, args.yes)

    if args.command == "rejudge":
        from .delivery import is_legacy_delivery, resolve_run
        from .pipeline.config import load_config
        from .pipeline.rejudge import run_rejudge
        # --delivery 指的是**某一次跑批**;只给了交付目录时按 latest 记的那次执行
        # 并把选中的那次明说出来(裁决是写数据的命令,不许让人猜动了哪一份)。
        if not is_legacy_delivery(args.delivery):
            _run = resolve_run(args.delivery)
            if _run != args.delivery:
                print(f"[rejudge] 按 latest 记录选中 {os.path.basename(_run)};"
                      "要执行别的那次,把 --delivery 写到那一次的目录", flush=True)
            args.delivery = _run
        cfg = load_config(args.config)
        if args.vlm_backend:
            from .pipeline.config import apply_vlm_backend
            cfg = apply_vlm_backend(cfg, args.vlm_backend)
        summary = run_rejudge(args.delivery, args.input, cfg)
        print(json.dumps(summary, ensure_ascii=False, indent=1)
              if isinstance(summary, dict) else summary)
        return 0

    if args.command == "reprofile":
        from .delivery import is_legacy_delivery, resolve_run
        from .pipeline.config import load_config
        from .pipeline.reprofile import run_reprofile
        # 与 rejudge 同一套选运行语义:reprofile 也是写数据的命令,动了哪一份要明说
        if not is_legacy_delivery(args.delivery):
            _run = resolve_run(args.delivery)
            if _run != args.delivery:
                print(f"[reprofile] 按 latest 记录选中 {os.path.basename(_run)};"
                      "要重算别的那次,把 --delivery 写到那一次的目录", flush=True)
            args.delivery = _run
        cfg = load_config(args.config)
        if args.vlm_backend:
            from .pipeline.config import apply_vlm_backend
            cfg = apply_vlm_backend(cfg, args.vlm_backend)
        summary = run_reprofile(args.delivery, cfg=cfg)
        if summary.get("note"):
            print(f"[reprofile] {summary['note']}")
        return 0

    if args.command == "backends":
        return _cmd_backends(args.config, args.timeout)
    if args.command == "ui":
        try:
            import gradio  # noqa: F401
        except ImportError:
            print("[curation] ui 需要 gradio:pip install gradio", file=sys.stderr)
            return 2
        from .ui.app import launch
        launch(args.delivery, config_path=args.config, host=args.host,
               port=args.port, probe_timeout=args.timeout,
               terminal=args.terminal, review_dir=args.review_dir,
               data_root=args.data_root)
        return 0
    if args.command == "run":
        from .ingest.lerobot_reader import NotADatasetError, OutputExistsError
        from .pipeline.run import run_pipeline

        try:
            _eps = _parse_episodes(args.episodes)
        except ValueError as e:
            print(f"[输入错误] --episodes {args.episodes!r} 解析失败:{e}\n"
                  "  用法:单条 34 / 多条 34,56,78 / 区间 10-20 / 混用 3,10-12",
                  file=sys.stderr)
            return 2
        if _eps:
            print(f"[curation] 只跑指定 episode({len(_eps)} 条): "
                  f"{sorted(_eps)[:10]}{'…' if len(_eps) > 10 else ''}")

        # 跑批子目录名只算一次:--batch 下几个数据集共用同一个名字,一次点击的
        # 产物在各自交付里对得上号(`<交付>/<数据集>/20260814-074045/`)。
        from .delivery import new_run_name
        run_name = args.run_name or new_run_name()

        def _run_one(inp, outp):
            # finally 清临时视频缓存(P4):run_pipeline 正常收尾时自己会清,这里兜的是
            # **异常退出**那条路 —— RRD 解出的 mp4 躺在容器可写层,批处理连崩几个数据集
            # 就能把 /tmp 撑满。幂等,清两次不出错。
            try:
                return run_pipeline(args.config, inp, outp,
                                    embodiment_id=args.embodiment_id,
                                    max_episodes=args.max_episodes,
                                    only_checks=args.only, skip_checks=args.skip,
                                    report_only=args.report_only, lite=args.lite,
                                    run_name=run_name,
                                    set_overrides=args.set_overrides,
                                    episode_indices=_eps,
                                    vlm_backend=args.vlm_backend,
                                    vlm_endpoint=args.vlm_endpoint,
                                    vlm_model=args.vlm_model,
                                    vlm_api_key_env=args.vlm_api_key_env)
            finally:
                from .ingest.rrd_reader import cleanup_video_cache
                cleanup_video_cache(inp)

        if args.batch:
            datasets = _list_datasets(args.input)
            if not datasets:
                print(f"[输入错误] {args.input} 下没有有效数据集", file=sys.stderr)
                return 2
            print(f"[batch] 处理 {len(datasets)} 个数据集: {datasets}\n")
            agg = []
            robots: dict = {}
            for ds in datasets:
                print(f"===== {ds} =====")
                try:
                    s = _run_one(os.path.join(args.input, ds),
                                 os.path.join(args.output, ds))
                    print(f"  交付 {s['n_delivered']} 条(输入 {s['stats'].get('input')})")
                    agg.append((ds, s["stats"].get("input"), s["n_delivered"]))
                    robots[ds] = s.get("robot") or {}
                except Exception as e:  # noqa: BLE001  单集失败不拖垮整批
                    print(f"  失败: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
                    agg.append((ds, None, "失败"))
            print("\n===== 批处理汇总 =====")
            for ds, ni, nd in agg:
                print(f"  {ds}: 输入 {ni} → 交付 {nd}")
            print(f"  各数据集结果: {args.output}/<数据集名>/{run_name}/")
            # 批处理汇总文件(2026-07-15 用户定):数据集名 + 机器人型号一览。
            # 写法走 safe_write(本地临时文件 + copyfile):`--output` 可能落在
            # TOS 的 FSX 挂载上,库直写已经咬过六次。
            summary_rows = []
            for i, (ds, ni, nd) in enumerate(agg):
                rb = robots.get(ds) or {}
                summary_rows.append({"数据集": ds,
                                     "机器人": rb.get("robot_type", "(失败/未知)"),
                                     "规格表": rb.get("registry_profile", "-"),
                                     "输入": ni, "交付": nd})
            from .export.safe_write import write_json as _write_json
            from .export.safe_write import write_text as _write_text
            _write_json(os.path.join(args.output, "batch_summary.json"),
                        {"数据集数": len(agg), "datasets": summary_rows})
            md = ["# 批处理汇总", "",
                  "| 数据集 | 机器人型号 | 规格表 | 输入 | 交付 |",
                  "|---|---|---|---|---|"]
            for r in summary_rows:
                md.append(f"| {r['数据集']} | {r['机器人']} | {r['规格表']} |"
                          f" {r['输入']} | {r['交付']} |")
            md.append("")
            md.append(f"各数据集完整报告见 <输出目录>/<数据集名>/{run_name}/report.md")
            _write_text(os.path.join(args.output, "batch_summary.md"), "\n".join(md))
            print(f"  汇总清单: {args.output}/batch_summary.md")
            return 0

        try:
            summary = _run_one(args.input, args.output)
        except NotADatasetError as e:
            print(f"[输入错误] {e}", file=sys.stderr)
            return 2
        except OutputExistsError as e:
            print(f"[输出目录冲突] {e}", file=sys.stderr)
            return 3
        except Exception as e:
            from .pipeline.config import ConfigError
            if isinstance(e, ConfigError):
                print(f"[配置错误] {e}", file=sys.stderr)
                return 2
            raise
        print(f"质检统计: {summary['stats']}")
        print(f"本次跑批: {summary['run_dir']}")
        print(f"交付 {summary['n_delivered']} 条;三件套:")
        for k, v in summary["deliverables"].items():
            print(f"  - {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
