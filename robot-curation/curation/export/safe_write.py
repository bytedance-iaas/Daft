"""交付目录的文件写入(挂载安全写法,全交付件共用一个入口)。

为什么单独立一个模块:交付根挂在 TOS 的 FSX 上,**库/进程对着挂载路径直写**
已经咬过六次,每次现象都不一样、每次都静默:
  ① matplotlib savefig → 零填充的坏 PNG(文件大小正常,魔数不对);
  ② pyarrow 直写 parquet → EPERM(随机写被拒);
  ③ PyAV faststart 收尾 seek 回文件头 → 无 moov 的废 mp4,浏览器永久转圈;
  ④ 任务台归档 status.json → 164 字节全 `\\0`;
  ⑤ 2026-08-14 抓到的 passed.json → 10853 字节全 `\\0`(save_report 直接
     `open(...,"w") + json.dump`,而且一次跑批对同一路径写了三遍)。
  ⑥ 2026-08-14 重判后的 passed.json → 138719 字节全 `\\0`,**而它已经走了本模块**
     —— 因为发布那一步是 `copyfile`(就地截断再写),不是原子的;同一路径一次跑批
     写四遍、重判写两遍,中间态被下一遍撞上就永久停在全零。

解法两段,缺一不可:
  · 先写**容器本地**临时文件 —— 任意写法(随机写、多次 flush)都不碰挂载;
  · 再**原子发布**:拷到目标目录内的临时名,`os.replace` 改名到位(见 `_publish`)。
    读者要么看到旧的整份、要么看到新的整份,**没有中间态**。

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
import stat
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

    verify: None(默认)/ "json" / "text"。开了就在发布后回读一次,**只对"读到了
    但内容是坏的"(零填充/解析不了)动手**:重写一次并打一行日志,第二次仍坏也
    只警告不抛。读不到(0 字节)仍然不算故障 —— 别的写者(daft 直写 parquet)
    照旧有可见性延迟,回验轮询负责等它。
    ⚠️ 走本模块发布的文件已经**不该**再出现 0 字节窗口了(改名是原子的,实测
    写完立刻读得回正确内容);要是又见到,说明有人绕开了这里直写挂载。
    """
    parent = os.path.dirname(os.path.abspath(dst))
    os.makedirs(parent, exist_ok=True)
    suffix = os.path.splitext(dst)[1] or ".tmp"
    fd, tmp = tempfile.mkstemp(prefix=".curation-out-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            yield f
        _publish(tmp, dst)
        if verify and content_state(dst, verify) == STATE_CORRUPT:
            _publish(tmp, dst)                             # 重写一次
            if content_state(dst, verify) == STATE_CORRUPT:
                print(f"[curation] ⚠️ 交付件回读是坏的(零填充/解析不了),重写一次"
                      f"仍坏:{dst} —— 这份产物不可用,需要重跑", flush=True)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _publish(local_tmp: str, dst: str) -> None:
    """把写好的本地临时文件**原子地**摆到 dst。

    2026-08-14 第七次事故的根因就在这一步:原来是 `shutil.copyfile(tmp, dst)`,
    也就是**就地截断再从头写**。发布不是原子的 ⇒ 存在一段"文件已经在那儿、内容
    还不是它"的中间态,谁在这段里读,读到的就是 0 字节或**零填充**;而一次跑批
    对同一个 `passed.json` 要写四遍(`save_report` 在 run.py 里被调 4 次),
    重判又写两遍 —— 中间态被另一遍写撞上,文件就永久停在全零(实测那份
    138719 字节全 `\\0`,25 分钟后仍是全零,不是可见性延迟)。

    改成"先拷到**目标目录内**的临时名,再 `os.replace` 改名"。rename 在同一挂载
    内是原子的:读者要么看到旧的整份,要么看到新的整份,没有中间态。
    实测收益还多一条:copyfile 写完立刻读是 **0 字节**(要 30-90 秒才吐出真内容),
    而 replace 写完**立刻**就能读回正确内容 —— 那个折磨了回验逻辑很久的可见性
    窗口,顺手一起没了。

    本地临时文件那一段**保留不动**:任意写法(savefig / csv.writer / json.dump
    的随机写与多次 flush)都只发生在本地盘上,挂载只承受一次顺序整份拷贝。
    """
    parent = os.path.dirname(os.path.abspath(dst))
    fd, staged = tempfile.mkstemp(prefix=".curation-pub-", dir=parent)
    os.close(fd)
    try:
        shutil.copyfile(local_tmp, staged)
        os.replace(staged, dst)                            # 原子改名(同一挂载内)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        raise


@contextlib.contextmanager
def delivery_dir(dst_dir: str):
    """目录级交付件("一个目录=一份数据集",episodes_parquet 这类)的原子发布。

    与 `delivery_file` 成对:文件有 `os.replace` 可用,目录没有等价物 —— 于是
    rejudge 的交付数据集同步曾写成 `rmtree(pq_dir)` 再 `write_parquet(pq_dir)`,
    **先删后写、中间零保护**:写失败 / 进程被杀 / pod 被驱逐,客户真正拿走的那份
    数据集就彻底没了(比 JSON 更糟 —— JSON 至少还剩个坏文件看得出出过事)。

    契约:`with delivery_dir(dst) as staging:` 拿到的是 **dst 同级**的空临时目录
    (同级=同一挂载,rename 才是原子的),写者只管往 staging 里写;退出上下文时
    staging 换到 dst 的位置。硬规矩:**新数据完全写好之前,原目录一个字节都不动**。

    换位是三步(`os.rename` 不能覆盖非空目录,只能这么走):
      旧目录改名到一边 → staging 改名到位 → 删旧目录。
    ⚠️ 如实说明:第一步和第二步之间有一个**微秒级的窗口,此刻 dst 不存在**
    ——这不是零窗口,但窗口里不涉及任何数据丢失(新旧两份都完整地在同级目录里,
    第二步失败会把旧目录放回去)。写者抛异常 ⇒ 原目录原样、staging 清干净、异常照抛。
    """
    dst_dir = os.path.abspath(dst_dir)
    parent = os.path.dirname(dst_dir)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".curation-stage-", dir=parent)
    # mkdtemp 建出来是 0700,改名后那就是交付目录本身的权限 —— 必须放开,否则
    # 交付数据集从此只有本用户读得了(原来 write_parquet 直建目录没这个问题)。
    # 跟**父目录**对齐:同一份交付里的目录权限本来就该一致。
    # ⚠️ 别用"os.umask(0) 探一下再还原"那套:umask 是**进程级**的,而管线是多线程
    # (funnel 的并发 UDF、导出线程都在跑),那一瞬别的线程新建的文件会拿到全开权限。
    with contextlib.suppress(OSError):
        os.chmod(staging, stat.S_IMODE(os.stat(parent).st_mode))
    try:
        yield staging
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    old = None
    if os.path.isdir(dst_dir):
        # POSIX rename 允许"新路径是空目录"时原子替换 ⇒ 借 mkdtemp 拿唯一名,
        # 免去"先删占位再改名"的竞态。
        old = tempfile.mkdtemp(prefix=".curation-old-", dir=parent)
        os.replace(dst_dir, old)
    try:
        os.replace(staging, dst_dir)
    except BaseException:
        if old is not None and not os.path.exists(dst_dir):
            with contextlib.suppress(OSError):
                os.replace(old, dst_dir)               # 新的没换上去 ⇒ 旧的放回原位
                old = None
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


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
