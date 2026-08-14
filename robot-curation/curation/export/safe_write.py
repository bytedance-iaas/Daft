"""交付目录的文件写入(挂载安全写法,全交付件共用一个入口)。

为什么单独立一个模块:交付根挂在 TOS 的 FSX 上,**库/进程对着挂载路径直写**
已经咬过六次,每次现象都不一样、每次都静默:
  ① matplotlib savefig → 零填充的坏 PNG(文件大小正常,魔数不对);
  ② pyarrow 直写 parquet → EPERM(随机写被拒);
  ③ PyAV faststart 收尾 seek 回文件头 → 无 moov 的废 mp4,浏览器永久转圈;
  ④ 任务台归档 status.json → 164 字节全 `\\0`;
  ⑤ 2026-08-14 抓到的 passed.json → 10853 字节全 `\\0`(save_report 直接
     `open(...,"w") + json.dump`,而且一次跑批对同一路径写了三遍)。
解法永远是同一个:先写**容器本地**临时文件,再 `shutil.copyfile` 整份顺序拷过去
—— 交付目录只见"从头到尾一遍写完"。

⚠️ 回读校验只对最关键的几个产物开(`verify=`):FSX 上新写的文件有 20-60s 读不
回来的可见性延迟(delivery.write_latest 的教训),给每个文件都配"读不回就报警"
只会天天误报。开了也只做"重写一次 + 留一行日志",**绝不因为读不回让跑批失败**
—— 产物已经写出去了,可见性由后面的落盘回验轮询确认。
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile


#: 回读的三种结局。分开是有实测依据的(2026-08-14 在 pod 上量的 FSX 行为):
#: 刚 copyfile 完的文件**读回来是 0 字节**,要 30 秒上下才吐出真内容 —— 也就是
#: "读不到"在这张挂载上是**常态**,不是故障;而写坏的文件是**非空的零填充**
#: (passed.json 那次 10853 字节全 `\0`)。把两者混成一个 False,要么天天误报、
#: 要么把真事故当延迟放过去。
STATE_OK = "ok"
STATE_CORRUPT = "corrupt"        # 读到了,但内容是坏的 —— 等下去也不会变好
STATE_UNSEEN = "unseen"          # 还读不到 / 读到空:挂载可见性延迟,交给轮询


def content_state(path: str, kind: str = "text") -> str:
    """回读一次,判 ok / corrupt / unseen。不抛异常。

    ⚠️ corrupt 的判据里**必须有"不含 `\\0`"这一条**:`open(...).read().strip()`
    对一串 `\\0` 返回的是"非空"(strip 只吃空白字符),拿"非空"当判据的话,
    10853 字节全零的 passed.json 会一路验过去 —— 事故当天就是这么混过回验的。
    合法的 json / md / csv / jsonl 正文里都不会出现 `\\0`。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return STATE_UNSEEN
    if not raw.strip():
        return STATE_UNSEEN
    if b"\x00" in raw:
        return STATE_CORRUPT
    if kind == "json":
        try:
            return STATE_OK if json.loads(raw.decode("utf-8")) else STATE_CORRUPT
        except (ValueError, UnicodeDecodeError):
            return STATE_CORRUPT
    return STATE_OK


def readable(path: str, kind: str = "text") -> bool:
    """"现在就读得回来且内容是好的"吗 —— 轮询式回验用这个(unseen 要继续等)。"""
    return content_state(path, kind) == STATE_OK


@contextlib.contextmanager
def delivery_file(dst: str, *, encoding: str = "utf-8", newline: str | None = None,
                  verify: str | None = None):
    """交付件写入上下文:拿到的文件对象指向本地临时文件,退出时整份拷到 dst。

    用法与 `open(dst, "w")` 完全一致(json.dump / csv.writer / f.write 都照旧),
    调用方只需把 open 换成它。写入过程中抛异常 → 不落盘(半截文件不如没有)。

    verify: None(默认)/ "json" / "text"。开了就在拷完后回读一次,**只对"读到了
    但内容是坏的"(零填充/解析不了)动手**:重写一次并打一行日志,第二次仍坏也
    只警告不抛。读不到(0 字节)不算故障 —— 这张挂载上新文件普遍要 30 秒才可见,
    当成故障的话每份交付件都会误报一次(2026-08-14 e2e 实测,8 行全是狼来了)。
    """
    parent = os.path.dirname(os.path.abspath(dst))
    os.makedirs(parent, exist_ok=True)
    suffix = os.path.splitext(dst)[1] or ".tmp"
    fd, tmp = tempfile.mkstemp(prefix=".curation-out-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            yield f
        shutil.copyfile(tmp, dst)
        if verify and content_state(dst, verify) == STATE_CORRUPT:
            shutil.copyfile(tmp, dst)                      # 重写一次
            if content_state(dst, verify) == STATE_CORRUPT:
                print(f"[curation] ⚠️ 交付件回读是坏的(零填充/解析不了),重写一次"
                      f"仍坏:{dst} —— 这份产物不可用,需要重跑", flush=True)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def write_json(dst: str, payload, *, verify: str | None = None, **dump_kw) -> str:
    """JSON 交付件。默认 ensure_ascii=False(中文报告直接可读)+ indent=1。"""
    dump_kw.setdefault("ensure_ascii", False)
    dump_kw.setdefault("indent", 1)
    with delivery_file(dst, verify=verify) as f:
        json.dump(payload, f, **dump_kw)
    return dst


def write_text(dst: str, blob: str, *, verify: str | None = None) -> str:
    """纯文本交付件(report.md / html / jsonl 拼好的整串)。"""
    with delivery_file(dst, verify=verify) as f:
        f.write(blob)
    return dst
