"""边出边传(2026-08-21 用户拍板的方案 1):产出文件**封口即上传、传完即删**。

为什么:直连交付(交付目录不是本实例挂载的桶)此前是"整棵产出树先落 pod 本地,跑成功后
stage_out 整体上传" —— 交付多大 pod 盘就得先放得下多大,大头是 lerobot_curated/ 里重编码的
视频。改成文件一封口就交给这里的发布器,本地随传随删,pod 盘上同时只有"正在写的几个文件 +
正在传的几个文件",上限由导出器的分文件阈值决定,与数据集大小无关。

契约:
- `activate(Publisher)` 上下文里,导出器 / 写盘器每封好一个文件调 `file_done(path)`(目录调
  `dir_done`);没激活时这些调用是空操作 —— 挂载交付、本地交付、单测一个字不用改。
- **完整性标志不在这儿传**(meta/info.json、passed.json、latest,见 tos_store.is_marker):
  它们必须押到其余对象全部就位之后,仍由跑批收尾的 stage_out 按 upload_plan 的顺序传;
  发布器只记下它们,最后由 stage_out 扫到。
- 上传在一个后台线程里顺序做(TOS 客户端不跨线程共用),与主线程的编码并行;失败重试
  (复用 tos_store._with_retry),仍失败记下来,`finish()` 时一并抛 TosStageError —— 此时
  远端没有任何标志文件,不会被当成完整交付。
- 传成功才删本地;传失败的文件留在本地,stage_out 的对账续传会补上。
"""
from __future__ import annotations

import contextlib
import os
import queue
import threading

_ACTIVE: dict = {"pub": None}


class Publisher:
    def __init__(self, local_root: str, url: str, region: str | None = None, *,
                 store=None, delete_local: bool = True):
        from .. import tos_store
        self.root = os.path.abspath(local_root)
        self.url = url
        self.bucket, prefix = tos_store.parse_tos_url(url)
        self.prefix = prefix.strip("/")
        self.region = region
        self.delete_local = delete_local
        self._store = store
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self.deferred: list[str] = []        # 完整性标志:只记不传
        self.uploaded: list[str] = []
        self.bytes_uploaded = 0
        self.errors: list[tuple[str, str]] = []
        self._finished = False

    # ── 入口 ──────────────────────────────────────────────────────────────
    def _rel(self, path: str) -> str:
        ap = os.path.abspath(path)
        rel = os.path.relpath(ap, self.root)
        if rel.startswith(".."):
            raise ValueError(f"{path} 不在产出根 {self.root} 下,不能发布")
        return rel.replace(os.sep, "/")

    def file_done(self, path: str) -> bool:
        """一个产出文件写完了。返回 True = 已排队上传;False = 没排(标志文件 / 重复 / 不存在)。"""
        from .. import tos_store
        if self._finished:
            raise RuntimeError("发布器已收尾,不能再排文件")
        if not os.path.isfile(path):
            return False
        rel = self._rel(path)
        if any(part.startswith(".curation-") for part in rel.split("/")):
            return False                       # safe_write 的发布残留目录,不进交付
        if tos_store.is_marker(rel):
            with self._lock:
                if rel not in self.deferred:
                    self.deferred.append(rel)
            return False
        with self._lock:
            if rel in self._seen:
                return False
            self._seen.add(rel)
        self._ensure_thread()
        self._q.put(rel)
        return True

    def dir_done(self, directory: str) -> int:
        """一个产出目录写完了:里面的文件逐个排队。返回排队数。"""
        n = 0
        for cur, _dirs, names in os.walk(directory):
            for name in sorted(names):
                if self.file_done(os.path.join(cur, name)):
                    n += 1
        return n

    # ── 后台上传 ──────────────────────────────────────────────────────────
    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="curation-publish",
                                            daemon=True)
            self._thread.start()

    def _get_store(self):
        if self._store is None:
            from .. import tos_store
            self._store = tos_store.make_store_for(self.bucket, self.region)
        return self._store

    def _worker(self) -> None:
        from .. import tos_store
        p = self.prefix + "/" if self.prefix else ""
        while True:
            rel = self._q.get()
            if rel is None:
                self._q.task_done()
                return
            local = os.path.join(self.root, rel)
            try:
                size = os.path.getsize(local)
                st = self._get_store()
                tos_store._with_retry("上传", rel, lambda: st.upload(local, self.bucket, p + rel))
                with self._lock:
                    self.uploaded.append(rel)
                    self.bytes_uploaded += size
                if self.delete_local:
                    with contextlib.suppress(OSError):
                        os.remove(local)
            except Exception as e:  # noqa: BLE001 SDK 异常族杂;留本地给 stage_out 续传
                with self._lock:
                    self.errors.append((rel, f"{type(e).__name__}: {str(e)[:160]}"))
            finally:
                self._q.task_done()

    def finish(self) -> int:
        """等队列传空;有失败就抛 TosStageError(失败的文件仍在本地)。返回上传成功数。"""
        from .. import tos_store
        if self._finished:
            return len(self.uploaded)
        self._finished = True
        if self._thread is not None:
            self._q.put(None)
            self._thread.join()
        if self.errors:
            head = "; ".join(f"{r}({m})" for r, m in self.errors[:3])
            raise tos_store.TosStageError(
                f"边出边传有 {len(self.errors)} 个文件没传上去:{head}"
                f"{' …' if len(self.errors) > 3 else ''};这些文件仍在本地 {self.root},"
                "完整性标志尚未上传,远端不会把这份半成品当完整交付列出,重跑即可续传")
        return len(self.uploaded)

    def summary(self) -> str:
        mb = self.bytes_uploaded / (1024 * 1024)
        return (f"边出边传:{len(self.uploaded)} 个文件 {mb:.0f} MB 已随产随传并清理本地,"
                f"{len(self.deferred)} 个完整性标志留待最后")


# ── 模块级入口:导出器 / 写盘器只认这几个函数,没激活就是空操作 ────────────────

def active() -> Publisher | None:
    return _ACTIVE["pub"]


@contextlib.contextmanager
def activate(pub: Publisher | None):
    prev = _ACTIVE["pub"]
    _ACTIVE["pub"] = pub
    try:
        yield pub
    finally:
        _ACTIVE["pub"] = prev


def file_done(path: str) -> bool:
    pub = _ACTIVE["pub"]
    return bool(pub and pub.file_done(path))


def dir_done(directory: str) -> int:
    pub = _ACTIVE["pub"]
    return pub.dir_done(directory) if pub else 0
