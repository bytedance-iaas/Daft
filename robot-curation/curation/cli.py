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


def _disp_w(s: str) -> int:
    """终端显示宽度:东亚宽字符(中文/全角)按 2 列算。argparse 按字符数折行,
    中文文案会被它低估一半宽度,窄终端下溢出硬折 —— 折行必须按这个宽度来。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _wrap_disp(text: str, width: int) -> list:
    """按显示宽度贪心折行:中文逐字可断,英文单词/路径/URL 不拦腰断。"""
    import re
    out = []
    for para in text.split("\n"):
        line, lw = "", 0
        # token = 连续 ASCII 可见串(单词/路径/选项名)| 空白 | 单个宽字符
        for tok in re.findall(r"[\x21-\x7e]+|\s+|.", para):
            tw = _disp_w(tok)
            if lw + tw > width and line:
                out.append(line.rstrip())
                line, lw = "", 0
                if tok.isspace():
                    continue
            line += tok
            lw += tw
        out.append(line.rstrip())
    return out


class _CjkHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """帮助文案的折行按显示宽度算(中文双宽);description/epilog 保留手工换行。
    宽度跟终端走、顶格铺满,不设上限(2026-08-26 用户口味,别再加封顶)。"""

    def _split_lines(self, text, width):
        return [l for l in _wrap_disp(text, width) if l or len(_wrap_disp(text, width)) == 1]

    def _fill_text(self, text, width, indent):
        return "\n".join(indent + l for l in _wrap_disp(text, width - _disp_w(indent)))

    def _format_action(self, action):
        # 抄自 argparse.HelpFormatter._format_action,只把对齐改按显示宽度:
        # 中文 metavar(如 --episodes 表达式)按字符数对齐会让右列漂几格、
        # 尾巴溢出终端。放不下对齐列的,help 从下一行起头,永远对齐。
        help_position = min(self._action_max_length + 2, self._max_help_position)
        help_width = max(self._width - help_position, 11)
        action_width = help_position - self._current_indent - 2
        action_header = self._format_action_invocation(action)
        if not action.help:
            action_header = "%*s%s\n" % (self._current_indent, "", action_header)
            indent_first = 0
        elif _disp_w(action_header) <= action_width:
            pad = action_width - _disp_w(action_header)
            action_header = "%*s%s%*s  " % (self._current_indent, "",
                                            action_header, pad, "")
            indent_first = 0
        else:
            action_header = "%*s%s\n" % (self._current_indent, "", action_header)
            indent_first = help_position
        parts = [action_header]
        if action.help and action.help.strip():
            help_lines = self._split_lines(self._expand_help(action), help_width)
            if help_lines:
                parts.append("%*s%s\n" % (indent_first, "", help_lines[0]))
                for line in help_lines[1:]:
                    parts.append("%*s%s\n" % (help_position, "", line))
        elif not action_header.endswith("\n"):
            parts.append("\n")
        for subaction in self._iter_indented_subactions(action):
            parts.append(self._format_action(subaction))
        return self._join_parts(parts)


def _human_size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GB"


#: ls 一层最多列多少个文件(目录不设限):交付/数据集目录一层几百个文件是常态,
#: 几十万是事故,截断要说出省略了多少
_LS_FILE_CAP = 200


def _tos_err_line(e: Exception) -> str:
    """TOS SDK 异常一行化说人话:SDK 的 str(e) 是整个响应字典(带 header/
    request_id),终端上没法读。取 code/message,常见错配人话。"""
    d = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], dict) else {}
    code = getattr(e, "code", None) or d.get("code") or ""
    msg = getattr(e, "message", None) or d.get("message") or ""
    plain = {"NoSuchBucket": "桶不存在",
             "AccessDenied": "本实例的 TOS 密钥没有这个桶的权限",
             "NoSuchKey": "对象不存在"}.get(code)
    if plain:
        return f"{plain}({code})"
    if code or msg:
        return f"{code}{':' if code and msg else ''}{msg}"
    return str(e)[:200]


def _cmd_ls(path: str, region: str | None) -> int:
    """列一层内容:tos:// 走直连 delimiter 列举,本地走 scandir。输出先目录后文件。"""
    if str(path).startswith("tos://"):
        from . import tos_store
        try:
            bucket, prefix = tos_store.parse_tos_url(path)
        except Exception as e:  # noqa: BLE001 — URL 写法错要按输入错误报
            print(f"[输入错误] {e}", file=sys.stderr)
            return 2
        try:
            dirs, files = tos_store.make_store_for(bucket, region).list_dir(bucket, prefix)
        except Exception as e:  # noqa: BLE001 — 网络/权限/桶名,统一如实报
            print(f"[TOS 错误] 列不动 {path}:{_tos_err_line(e)}", file=sys.stderr)
            return 1
    else:
        if not os.path.isdir(path):
            print(f"[输入错误] {path} 不是目录", file=sys.stderr)
            return 2
        dirs, files = [], []
        for e in sorted(os.scandir(path), key=lambda x: x.name):
            if e.is_dir():
                dirs.append(e.name)
            else:
                files.append((e.name, e.stat().st_size))
    if not dirs and not files:
        print("(空)")
        return 0
    for d in sorted(dirs):
        print(d + "/")
    for name, size in files[:_LS_FILE_CAP]:
        print(f"{name}  {_human_size(size)}")
    if len(files) > _LS_FILE_CAP:
        print(f"……还有 {len(files) - _LS_FILE_CAP} 个文件未列出")
    tail = f"共 {len(dirs)} 个目录、{len(files)} 个文件"
    if files:
        tail += f",文件合计 {_human_size(sum(s for _, s in files))}"
    print(tail)
    return 0


def _dist_version() -> str:
    """安装包的版本号(pyproject 单一来源,不另抄数字;开发树未安装时如实说不知道)。"""
    try:
        from importlib.metadata import version
        return version("curation")
    except Exception:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="curation",
        description="机器人数据质检流水线:质检/清洗/组织,交付干净数据集+质检报告+技能画像",
        epilog="每个子命令有详细帮助:curation <命令> --help\n"
               "典型流程:curation run(质检)→ 界面上人工裁决 → curation rejudge(执行裁决)",
        formatter_class=_CjkHelpFormatter,
        add_help=False,
    )
    p.add_argument("-h", "--help", action="help", help="显示本帮助并退出")
    p.add_argument("--version", action="version", help="显示版本号并退出",
                   version=f"curation {_dist_version()}")
    sub = p.add_subparsers(dest="command", required=True)

    def _cmd(name: str, one_liner: str, detail: str | None = None):
        # 顶层 help 是"选命令的目录",一条一句话;细节住在各命令自己的 --help 里
        sp = sub.add_parser(name, help=one_liner, description=detail or one_liner,
                            add_help=False,
                            formatter_class=_CjkHelpFormatter)
        sp.add_argument("-h", "--help", action="help", help="显示本命令的帮助并退出")
        return sp

    run = _cmd("run", "对一个数据集跑质检,产出交付数据集、质检报告与技能画像",
               "对一个数据集端到端跑质检:六维检查、字节级精确去重、技能画像、"
               "标注审计,产出交付数据集、质检报告与三份判决清单"
               "(通过 / 拒绝 / 待裁决)。\n"
               "每跑一次,在交付目录下新建一个以时间戳命名的批次目录"
               "存放本次结果;之前的批次原样保留。")
    run.add_argument("--config", default=None,
                     help="这一次用你自己的流水线 YAML(叠加到出厂默认)。缺省读环境"
                          "变量 CURATION_CONFIG 指的站点配置,部署里已配好,平时不用给")
    run.add_argument("--input", required=True,
                     help="输入数据集:本地目录,或 tos://桶/前缀(TOS 直读:LeRobot 数据集"
                          "不落本地盘,RRD 先暂存;地区用 --input-region)")
    run.add_argument("--output", required=True,
                     help="交付目录:本地目录,或 tos://桶/前缀(TOS 直连,本地跑完"
                          "整树上传,完整性标志最后传;地区用 --output-region);"
                          "本次结果落在 <交付>/<时间戳>/ 里,永不覆盖上一次")
    run.add_argument("--input-region", default=None, metavar="地区",
                     help="--input 为 tos:// 时的桶地区(如 cn-beijing / ap-southeast-1);"
                          "缺省读 TOS_REGION,再从 TOS_ENDPOINT 推导")
    run.add_argument("--output-region", default=None, metavar="地区",
                     help="--output 为 tos:// 时的桶地区;缺省规则同 --input-region")
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
    # UI 任务台的内部通道(「跑质检」页把任务编号传进来,让结果与任务对得上),
    # 不是给人手填的 → help 藏起来(2026-08-26 参数审计):自由名(如 b1)会写出
    # prune/latest/交付发现都不认的"幽灵批次"(_RUN_NAME_RE 只认时间戳打头),
    # 下面在入口校验堵死这坑。原说明:本次跑批的子目录名,缺省按本地时间生成。
    run.add_argument("--run-name", default=None, metavar="子目录名",
                     help=argparse.SUPPRESS)
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
                     help="存放 API Key 的环境变量名(托管端点用;密钥本身不进命令行)。"
                          "缺省读 CURATION_VLM_API_KEY_ENV")
    run.add_argument("--vlm-backend", default=None, metavar="预设名",
                     help="一键切换 VLM 后端(端点/模型/密钥三元组整组换),预设定义在 "
                          "default.yaml 的 vlm_backends 段(如 ark / h20-8b);"
                          "仍可 --set checks.task_success.vlm.* 微调(--set 后应用,赢)")
    run.add_argument("--report-only", action="store_true",
                     help="只出报告,不导出数据集(单模块快查时省去重编码视频的时间)")

    rj = _cmd("rejudge", "执行人工裁决:按裁决记录重判并落实到交付",
              "按人工裁决更新交付,三类裁决一次消化:\n"
              "· 标注分歧(human-decisions/label_decisions.csv):采纳改标的条目"
              "用新标注重跑任务成败检测;\n"
              "· 任务成败裁决(human-decisions/task_verdicts.csv):不重判,"
              "以人的结论为准;\n"
              "· 被拒复议(human-decisions/reject_appeals.csv):人判可用的条目"
              "从“拒绝”翻回“通过”,并补回交付数据集。\n"
              "技能画像只重排被裁决的这几条;要在已有技能体系里对全部轨迹"
              "重新分类,用 reprofile。")
    rj.add_argument("--delivery", required=True,
                    help="要更新的那一次跑批目录(<交付目录>/<时间戳>/);"
                         "只给交付目录时按 latest 记的那次执行")
    rj.add_argument("--input", required=True,
                    help="原始数据集目录或 tos://桶/前缀(重判需重新解码视频)")
    rj.add_argument("--input-region", default=None, metavar="地区",
                    help="--input 为 tos:// 时的桶地区;缺省按部署的 TOS_REGION")
    rj.add_argument("--delivery-region", default=None, metavar="地区",
                    help="--delivery 为 tos:// 时的桶地区(交付在桶里:先镜像到本地"
                         "执行,改动同步回桶;缺省按部署的 TOS_REGION)")
    rj.add_argument("--config", default=None,
                    help="流水线 YAML,须与原 run 一致(叠加到出厂默认;缺省读环境"
                         "变量 CURATION_CONFIG 指的站点配置)")
    rj.add_argument("--vlm-backend", default=None, metavar="预设名",
                    help="重判用的 VLM 后端预设(同 run,如 ark / h20-32b);缺省跟随配置")
    rj.add_argument("--retry-abstained", action="store_true",
                    help="补判弃权条目:只对「VLM 调用/解析失败」类弃权重跑成败判定"
                         "(超时/网络故障的廉价补救,免整批重跑);模型真答\"判不了\"的"
                         "弃权不重试(重试大概率同答案),仍走人工裁决。人工已裁的"
                         "以人为准,不重试")

    rp = _cmd("review-page", "生成静态审片站(索引页 + 逐条多路视频页)",
              "生成静态审片站:索引一屏列全量 episode,逐条页多路视频同看。"
              "产物落盘持久,由质检平台的 /review 路由提供访问,服务重启不丢。")
    rp.add_argument("--input", required=True,
                    help="数据集目录(LeRobot v2/v3;RRD 本版本未开放)")
    rp.add_argument("--output", required=True,
                    help="产出目录(建议持久盘,如 /mnt/tos/review/<名字>)")
    rp.add_argument("--episodes", default=None, metavar="表达式",
                    help="只做指定 episode(同 run:34,56 或 10-20,可混用);缺省全量")
    rp.add_argument("--max-episodes", type=int, default=None, help="只做前 N 条")
    rp.add_argument("--title", default=None, help="页面标题(缺省用数据集目录名)")
    # RRD 能力 release 默认关(ingest.rrd_enabled),对普通用户这个参数是一堵墙
    # → help 里藏起来(功能保留,开了 RRD 的部署照用);--help 只亮可靠的
    # (2026-08-26 用户定的纪律)。原说明:采集帧率,RRD 无时间信息时必须给
    # (如 so101 用 30);等价于 run 的 --set ingest.rrd_fps。
    rp.add_argument("--rrd-fps", type=float, default=None, metavar="帧率",
                    help=argparse.SUPPRESS)

    pr = _cmd("prune", "列出并按需清理一份交付下的历次跑批(默认只列不删)",
              "列出一份交付下的历次跑批(时间 / 条数 / 占用),并按需删掉旧的几次。"
              "默认只列不删;真删需要同时给 --keep-latest 与 --yes。")
    pr.add_argument("delivery", help="交付目录(如 /mnt/tos/deliveries/droid-200-full)")
    pr.add_argument("--keep-latest", type=int, default=None, metavar="N",
                    help="要删的是哪几次:留最新的 N 次,更早的删掉。"
                         "latest 记的那次永远保留。不给这个参数就只列出、不删任何东西")
    pr.add_argument("--yes", action="store_true",
                    help="真的删(不加就只打印将要删什么;必须同时给 --keep-latest)")

    lsp = _cmd("ls", "列一个目录或 tos:// 地址下有什么(数据集桶 / 交付桶都能看)",
               "列出本地目录或 tos://桶/前缀 下的一层内容:先目录后文件,文件带大小。"
               "直连形态下数据集不落本地盘,在终端里看源数据集桶、交付桶里有什么,"
               "就用它。凭证用本实例的 TOS 密钥,读不读得到由密钥权限决定。")
    lsp.add_argument("path", metavar="地址", help="本地目录,或 tos://桶/前缀")
    lsp.add_argument("--region", default=None, metavar="地区",
                     help="tos:// 地址的桶地区(如 cn-beijing;缺省读 TOS_REGION,"
                          "再从 TOS_ENDPOINT 推导)")

    fe = _cmd("fetch", "从数据来源站拉公开数据集到本站数据集根",
              "从数据来源站拉公开数据集进本站数据集根,下完即可直接跑质检。"
              "下载先落本地暂存、逐文件校验后再顺序拷入,不直写 TOS。")
    fe.add_argument("--source", required=True, metavar="来源",
                    help="数据来源。目前可选:ai-infra  内网 HuggingFace 缓存桶"
                         "(经 oniond 下载,同区直连);清单可在站点配置 fetch_sources 段扩充")
    fe.add_argument("--ref", required=True, metavar="源站名",
                    help="数据集在源站上的名字(如 libero)")
    fe.add_argument("--include", action="append", metavar="模式",
                    help="只下匹配这些模式的文件(可重复,如 --include 'meta/*');"
                         "不给 = 下整个数据集。部分拉取的数据不完整,不会被当作"
                         "可用数据集列出;要可用数据集请完整拉取")
    fe.add_argument("--into", default=None, metavar="数据集根",
                    help="落到哪个数据集根目录;缺省用配置里第一个 tos_buckets 的 "
                         "datasets_path")
    fe.add_argument("--name", default=None, metavar="落地名",
                    help="落地目录名(缺省与 --ref 同名)")
    fe.add_argument("--overwrite", action="store_true",
                    help="同名数据集已存在时覆盖重下(缺省跳过并说明)")
    fe.add_argument("--config", default=None,
                    help="站点配置(叠加到出厂默认;缺省读环境变量 CURATION_CONFIG)")

    be = _cmd("backends", "列出全部模型服务预设的在线状态与服务端模型")
    be.add_argument("--config", default=None,
                    help="站点配置(叠加到出厂默认;缺省读环境变量 CURATION_CONFIG)")
    be.add_argument("--timeout", type=float, default=5.0, help="单端点探活超时秒数")

    pb = _cmd("public", "列出可直接质检的公共数据集",
              "列出可直接质检的公共数据集(站点配置 public_datasets 指的 HuggingFace 缓存桶)。")
    pb.add_argument("--config", default=None,
                    help="站点配置(叠加到出厂默认;缺省读环境变量 CURATION_CONFIG)")
    pb.add_argument("--json", action="store_true", help="按 JSON 输出(给脚本用)")
    # 零测试零真机使用记录 → 藏(同上纪律);原说明:忽略缓存,重读清单。
    pb.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)

    ui = _cmd("ui", "启动质检平台 Web 界面",
              "启动质检平台 Web 界面:跑质检、看质检报告、人工裁决、执行裁决都在这里。")
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
                    help="数据集根目录(「跑质检」页只列这个根下的数据集,"
                         "缺省 /mnt/tos/datasets)。面板只在这个根下选数据集,"
                         "不接受任意路径输入(安全边界)。"
                         "也可用环境变量 CURATION_DATA_ROOT")
    ui.add_argument("--terminal", action="store_true", default=_env_flag("CURATION_TERMINAL"),
                    help="打开顶层「终端」页签(内嵌网页终端:xterm.js + 本服务的 "
                         "/ws/term,与 UI 同端口同鉴权)。不传(或 CURATION_TERMINAL 未设)"
                         "则页签不渲染、/ws/term 路由不注册。"
                         "⚠️ 这是一个真 shell,公网暴露前必须配鉴权:"
                         "CURATION_UI_HTPASSWD_FILE(htpasswd 多用户,推荐)或 "
                         "CURATION_UI_USER/CURATION_UI_PASSWORD(单用户),"
                         "并在网关上再加一层")
    ui.add_argument("--root-path", default=os.environ.get("CURATION_UI_ROOT_PATH", ""),
                    help="UI 挂载前缀(如 /curation):与别的服务共用一个网关域名、"
                         "按路径分流时用;网关不剥前缀,全部路由都注册在前缀下。"
                         "缺省挂根路径。也可用环境变量 CURATION_UI_ROOT_PATH")

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
            from .adapters.vlm_client import probe_failure_reason
            reason = probe_failure_reason(e, p_.get("api_key_env"))
            state = "❌密钥问题" if ("密钥" in reason or "鉴权" in reason) else "❌不可达"
            print(f"{name:<24}{state:<10}{reason}")
    return 0


def _cmd_public(config_path: str | None, *, as_json: bool = False,
                refresh: bool = False) -> int:
    """`curation public`:公共数据集清单。每行一个数据集:名字、全名、版本、集数、
    直接能喂给 `run --input` 的 tos:// 地址与地区。"""
    from .ingest import public_catalog
    from .pipeline.config import load_config
    public_catalog.apply_config(load_config(config_path))
    if not public_catalog.configured():
        print("本实例没有配置公共数据集(站点配置 public_datasets.bucket 为空)",
              file=sys.stderr)
        return 2
    try:
        entries = public_catalog.catalog(force=refresh)
    except public_catalog.PublicCatalogError as e:
        print(f"[curation] {e}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"root": public_catalog.root_url(),
                          "region": public_catalog.region(),
                          "count": len(entries), "datasets": entries},
                         ensure_ascii=False, indent=1))
        return 0
    print(public_catalog.summary_line(len(entries)))
    if not entries:
        print("(缓存桶里目前没有 LeRobot 格式的数据集)")
        return 0
    w = max(len(e["name"]) for e in entries)
    for e in entries:
        eps = f"{e['episodes']} 条" if e.get("episodes") is not None else "-"
        warn = f"  ⚠️ {e['warning']}" if e.get("warning") else ""
        print(f"  {e['name']:<{w}}  {e.get('version') or '-':<6} {eps:>8}  {e['id']}{warn}")
    rg = public_catalog.region()
    print(f"跑法:curation run --input {public_catalog.root_url()}/<名字>"
          + (f" --input-region {rg}" if rg else "")
          + " --output tos://你的桶/deliveries/<交付名>")
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


def _stage_tos_delivery(args, tag: str):
    """--delivery 是 tos:// → 解析到批次、全量镜像到本地缓存,并把 args.delivery 改成
    本地路径;返回 (本地批次, 批次 URL, 地区) 供跑完写回。不是 tos:// 返回 ()。
    失败打印原因返回 None(调用方退出码 1)。"""
    d = str(getattr(args, "delivery", "") or "")
    if not d.startswith("tos://"):
        return ()
    from . import tos_store
    region = getattr(args, "delivery_region", None)
    try:
        url = tos_store.resolve_run_url(d, region)
        print(f"[{tag}] 交付在桶里:{url} → 先全量镜像到本地执行,改动再同步回桶", flush=True)
        local = tos_store.mirror_run(url, region)
    except (tos_store.TosUrlError, tos_store.TosConfigError) as e:
        print(f"[输入错误] {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001 网络/SDK 异常族杂
        print(f"[tos 失败] 镜像 {d} 失败:{type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return None
    args.delivery = local
    return (local, url, region)


def _sync_tos_delivery(sync, tag: str) -> int:
    """跑完把本地镜像的改动写回桶;返回退出码。写回失败要说清本地留在哪(人工判断
    不能静默丢)。"""
    if not sync:
        return 0
    local, url, region = sync
    from . import tos_store
    try:
        r = tos_store.sync_back(local, url, region)
    except Exception as e:  # noqa: BLE001
        print(f"[tos 失败] 写回 {url} 失败:{type(e).__name__}: {str(e)[:200]}\n"
              f"  改动保留在本地 {local},修好网络后重跑同一条命令即可续传",
              file=sys.stderr)
        return 1
    print(f"[{tag}] 已同步回 {url}:上传 {r['uploaded']} 个,删除 {r['deleted']} 个,"
          f"未变 {r['skipped']} 个", flush=True)
    return 0


def _reprofile_parser() -> argparse.ArgumentParser:
    """reprofile 的独立 parser(2026-08-27 用户定:对客户隐藏)。

    它是方针变更日的运维工具:客户的正常闭环(质检→裁决→rejudge)走完后
    reprofile 恒报 0 条变化,亮在 --help 里只会制造困惑 → 主 parser 里不注册
    (help / usage / 错误提示的候选列表都不出现),main 入口按第一个词拦截,
    功能与帮助原样保留(curation reprofile --help 仍有完整说明)。"""
    p = argparse.ArgumentParser(
        prog="curation reprofile",
        description="在已有的技能体系里,按当前归类方针对交付的全部轨迹重新分配归属"
                    "(归类文本标注优先:有原始标注用标注,没有才用已生成的 caption)。"
                    "不重新生成 caption、不重新归纳体系、不改交付数据集、不碰成败"
                    "判定;连跑两次,第二次报 0 条变化。\n"
                    "与 rejudge 的区别一句话:rejudge 只重排被人工裁决的那几条"
                    "(并把裁决落实到数据集);reprofile 无需裁决,对全部轨迹重排一遍。",
        formatter_class=_CjkHelpFormatter, add_help=False)
    p.add_argument("-h", "--help", action="help", help="显示本命令的帮助并退出")
    p.add_argument("--delivery", required=True,
                   help="要重算的那一次跑批目录(<交付目录>/<时间戳>/);"
                        "只给交付目录时按 latest 记的那次执行(同 rejudge)")
    p.add_argument("--delivery-region", default=None, metavar="地区",
                   help="--delivery 为 tos:// 时的桶地区(同 rejudge)")
    p.add_argument("--config", default=None,
                   help="流水线 YAML(缺省读环境变量 CURATION_CONFIG;仅用于"
                        "「归不进体系时问一次 LLM 补漏」,没配 VLM 端点就诚实留"
                        "「未归类」)")
    p.add_argument("--vlm-backend", default=None, metavar="预设名",
                   help="补漏用的 LLM 后端预设(同 run,如 ark / h20-32b);缺省跟随配置")
    return p


def _interactive_run_preflight(args) -> str | None:
    """TTY 下 run 的开跑前交互(2026-08-27 用户定:UI 弹框问的,CLI 也要问)。

    两问与 UI 同源:① 数据集没登记机器人型号 → 给疑似型号(档案登记/注册表
    试穿),可确认可跳过运动学;② LeRobot v3 → 问要不要切分逐条片段(不切
    也能按时间窗播放)。返回切分输出目录(答"切"时),否则 None。

    ⚠️ 只在 stdin/stdout 都是 TTY 时提问 —— UI 任务台子进程 / 脚本 / CI 里
    停下等键盘会把任务吊死;非 TTY 保持原行为(型号缺失照旧响亮报错)。
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    if getattr(args, "batch", False):
        return None
    inp = str(args.input or "").rstrip("/")
    from .ingest import dsfs
    try:
        info = dsfs.read_json(dsfs.join(inp, "meta", "info.json")) or {}
    except Exception:  # noqa: BLE001 读不动交给管道自己报,这里不拦
        info = {}
    if not info:
        return None
    # ① 机器人型号
    if (not args.embodiment_id
            and str(info.get("robot_type") or "").strip() in ("", "unknown")):
        from .ui.runner import embodiment_choices, suggest_embodiments
        root = os.path.dirname(inp) or "."
        name = os.path.basename(inp)
        sug = suggest_embodiments(root, name)
        print("这个数据集没有登记机器人型号,运动学检查需要知道型号才能查规格表。")
        for x in sug:
            print(f"  疑似:{x['id']}({x['reason']})")
        default = sug[0]["id"] if sug else ""
        tip = f"回车用 {default}" if default else "回车跳过运动学检查"
        ans = input(f"型号({' / '.join(embodiment_choices())};{tip},"
                    f"输 skip 跳过运动学): ").strip()
        if ans == "skip" or (not ans and not default):
            if args.only:
                kept = [x for x in str(args.only).split(",")
                        if x != "kinematic_limits"]
                args.only = ",".join(kept) or None
                if args.only is None:
                    args.skip = "kinematic_limits"
            else:
                args.skip = ",".join(filter(None,
                                            [args.skip, "kinematic_limits"]))
            print("[curation] 未指定型号:本次跳过运动学极限检查")
        else:
            args.embodiment_id = ans or default
            print(f"[curation] 机器人型号:{args.embodiment_id}")
    # ② LeRobot v3 切分(rrd 由总开关管,不在此问)
    ver = str(info.get("codebase_version") or "")
    if ver.startswith("v3"):
        ans = input("LeRobot v3 格式:多条轨迹合并存放,不切分也能按时间窗"
                    "播放;切分后播放体验更好。要切分吗?[y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            out = str(args.output or "").rstrip("/")
            clip_out = (f"{out}/review/{os.path.basename(inp)}"
                        if not out.startswith("tos://")
                        else os.path.join("/tmp/curation-review",
                                          os.path.basename(inp)))
            print(f"[curation] 质检完成后将切分逐条片段 → {clip_out}")
            return clip_out
    return None


def main(argv: list[str] | None = None) -> int:
    import os
    _tolerate_broken_log()
    _argv = list(sys.argv[1:] if argv is None else argv)
    if _argv[:1] == ["reprofile"]:
        args = _reprofile_parser().parse_args(_argv[1:])
        args.command = "reprofile"
    else:
        args = build_parser().parse_args(_argv)
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
        # 交付在桶里(2026-08-21):先把那次跑批全量镜像到本地缓存,在本地执行,
        # 最后把改动写回桶(改过的传、剔掉的删、完整性标志最后传)
        _sync = _stage_tos_delivery(args, "rejudge")
        if _sync is None and str(args.delivery).startswith("tos://"):
            return 1
        if str(args.input or "").startswith("tos://"):
            from .ingest import dsfs
            dsfs.configure(args.input_region)
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
        from .ingest.rrd_reader import apply_config as _rrd_apply_config
        _rrd_apply_config(cfg)
        from .ingest.public_catalog import apply_config as _public_apply_config
        _public_apply_config(cfg)
        summary = run_rejudge(args.delivery, args.input, cfg,
                              retry_abstains=getattr(args, "retry_abstained", False))
        print(json.dumps(summary, ensure_ascii=False, indent=1)
              if isinstance(summary, dict) else summary)
        return _sync_tos_delivery(_sync, "rejudge")

    if args.command == "reprofile":
        from .delivery import is_legacy_delivery, resolve_run
        from .pipeline.config import load_config
        from .pipeline.reprofile import run_reprofile
        _sync = _stage_tos_delivery(args, "reprofile")
        if _sync is None and str(args.delivery).startswith("tos://"):
            return 1
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
        from .ingest.public_catalog import apply_config as _public_apply_config
        _public_apply_config(cfg)
        summary = run_reprofile(args.delivery, cfg=cfg)
        if summary.get("note"):
            print(f"[reprofile] {summary['note']}")
        return _sync_tos_delivery(_sync, "reprofile")

    if args.command == "ls":
        return _cmd_ls(args.path, args.region)

    if args.command == "fetch":
        from .fetch import run_fetch
        from .pipeline.config import load_config
        return run_fetch(load_config(args.config), args.source, args.ref,
                         into=args.into, name=args.name, includes=args.include,
                         overwrite=args.overwrite)

    if args.command == "backends":
        return _cmd_backends(args.config, args.timeout)
    if args.command == "public":
        return _cmd_public(args.config, as_json=args.json, refresh=args.refresh)
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
               data_root=args.data_root, root_path=args.root_path)
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
        _then_clips = _interactive_run_preflight(args)
        from .delivery import is_run_name, new_run_name
        if args.run_name and not is_run_name(args.run_name):
            print(f"[输入错误] --run-name {args.run_name!r} 不是合法批次名:必须以"
                  "时间戳打头(YYYYMMDD-HHMMSS,可带 -后缀),否则清理/最新批次"
                  "解析都不认它。留空即自动生成。", file=sys.stderr)
            return 2
        run_name = args.run_name or new_run_name()

        # ── tos:// 直连(2026-08-20 融合自公开 PR#65):桶与前缀是运行时输入。
        # 输入先整体下到本地缓存;输出先落本地、跑完整树上传,完整性标志最后传
        # (细节与 MVP 限制见 curation/tos_store.py 模块头)。
        # ⚠️ 挂载快路径优先:UI 侧对认识的桶直接传挂载路径过来,根本走不进这段;
        # 这段只服务"陌生桶"与手写 tos:// 的 CLI 用户。
        inp_root, out_root = args.input, args.output
        tos_in = str(args.input or "").startswith("tos://")
        tos_out = str(args.output or "").startswith("tos://")
        if tos_in or tos_out:
            from . import tos_store
            # 公共(匿名读)桶要在第一次碰桶之前登记好,否则签名请求被拒还长得像
            # "密钥没权限"(2026-08-21);只读配置里这一段,不动别的
            from .ingest.public_catalog import apply_config as _public_apply_config
            from .pipeline.config import load_config as _load_config
            _public_apply_config(_load_config(args.config))
            if args.batch:
                # MVP 限制(比 PR#65 收得更紧:输入或输出任一 tos:// 都拒):
                # batch 分支里 args.output 被拼进 <output>/<数据集名> 与两份
                # 汇总文件,staging 改写不彻底会把半套产物写去错的地方。
                print("[输入错误] --batch 暂不支持 tos:// 输入/输出(MVP 限制):"
                      "请对每个数据集单独跑一条 run", file=sys.stderr)
                return 2
            try:
                if tos_out:
                    # URL 不合法要在跑批**前**炸,不能等几小时算完再发现传不上去。
                    # 本地产出根按 桶/前缀 固定:上传中断后重跑同一条命令,
                    # stage_out 能按远端对账续传。
                    _b, _p = tos_store.parse_tos_url(args.output)
                    out_root = os.path.join(tos_store.cache_root(), "out",
                                            _b, _p or "_root")
                if tos_in:
                    # 2026-08-21 起 LeRobot 桶**直读**(读端会说 tos://):meta/parquet
                    # 按需取回内存,视频走预签名 URL 顺序读,pod 不落一个字节,数据集
                    # 多大都不受容器盘限制。RRD 仍整包暂存(rerun SDK 只吃本地文件)。
                    from .ingest import dsfs
                    dsfs.configure(args.input_region)
                    if dsfs.exists(dsfs.join(args.input, "meta", "info.json")):
                        inp_root = args.input
                        print(f"[curation] 输入 {args.input}:TOS 直读(不暂存到本地盘)",
                              flush=True)
                    else:
                        inp_root = tos_store.stage_in(args.input, args.input_region)
            except (tos_store.TosUrlError, tos_store.TosConfigError) as e:
                print(f"[输入错误] {e}", file=sys.stderr)
                return 2
            except tos_store.TosStageError as e:
                print(f"[tos 失败] {e}", file=sys.stderr)
                return 1

        def _upload_if_tos() -> int:
            """跑批成功后把本地产出树上传到 --output 指的 tos:// 前缀;
            失败保留本地产出并明说怎么续传。返回退出码(0 = 无事/成功)。"""
            if not tos_out:
                return 0
            from . import tos_store
            try:
                n = tos_store.stage_out(out_root, args.output, args.output_region)
            except (tos_store.TosUrlError, tos_store.TosConfigError) as e:
                print(f"[输入错误] {e}", file=sys.stderr)
                return 2
            except tos_store.TosStageError as e:
                print(f"[tos 失败] {e}\n  本地产出保留在 {out_root},修好后重跑"
                      "同一条命令即可续传(已传部分按远端对账跳过)",
                      file=sys.stderr)
                return 1
            print(f"[tos] 交付已上传:{args.output}(本次 {n} 个文件)")
            # 全部在桶里了 → 本地产出树清掉(2026-08-21):不清的话每跑一次直连就往
            # 容器可写层堆一份交付,几次就把 pod 的临时盘顶满(kubelet 直接驱逐)。
            # 失败那条路不清:续传要靠它。
            import shutil
            shutil.rmtree(out_root, ignore_errors=True)
            print(f"[tos] 本地产出已清理:{out_root}", flush=True)
            return 0

        def _run_one(inp, outp):
            # finally 清临时视频缓存(P4):run_pipeline 正常收尾时自己会清,这里兜的是
            # **异常退出**那条路 —— RRD 解出的 mp4 躺在容器可写层,批处理连崩几个数据集
            # 就能把 /tmp 撑满。幂等,清两次不出错。
            # 直连输出(2026-08-21 方案 1):激活发布器,导出的文件封口即传、传完即删;
            # 收尾 finish() 等传完,失败在这儿就炸(远端还没有任何完整性标志)。
            from .export import publish as _publish
            _pub = (_publish.Publisher(outp, args.output, args.output_region)
                    if tos_out else None)
            try:
                with _publish.activate(_pub):
                    summary = run_pipeline(args.config, inp, outp,
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
                    if _pub is not None:
                        _pub.finish()
                        print(f"[tos] {_pub.summary()}", flush=True)
                    return summary
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
            summary = _run_one(inp_root, out_root)
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
            from . import tos_store as _ts
            if isinstance(e, _ts.TosStageError):
                print(f"[tos 失败] {e}", file=sys.stderr)
                return 1
            raise
        print(f"质检统计: {summary['stats']}")
        print(f"本次跑批: {summary['run_dir']}")
        print(f"交付 {summary['n_delivered']} 条;三件套:")
        for k, v in summary["deliverables"].items():
            print(f"  - {k}: {v}")
        rc = _upload_if_tos()
        if rc:
            return rc
        if _then_clips:
            # 交互答了「切分」:同一条命令里接着跑审片站(main 自递归,与 UI
            # 任务台串第二条命令同一哲学);切分失败不改质检的成功退出码,
            # 明说重跑哪条命令即可补切
            print(f"[curation] 质检完成,开始切分逐条片段 → {_then_clips}")
            rc2 = main(["review-page", "--input", args.input,
                        "--output", _then_clips])
            if rc2:
                print(f"[curation] 切分未完成(退出码 {rc2}):质检结果不受影响,"
                      f"补切可单独跑 curation review-page --input {args.input} "
                      f"--output {_then_clips}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
