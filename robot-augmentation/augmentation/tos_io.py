"""TOS 读写小客户端(独立于质检系统)。凭证只认环境变量 TOS_ACCESS_KEY / TOS_SECRET_KEY,
端点 TOS_ENDPOINT(原生端点 tos-<region>.volces.com)、地区 TOS_REGION。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TosUrl:
    bucket: str
    key: str

    @classmethod
    def parse(cls, url: str) -> "TosUrl":
        assert url.startswith("tos://"), url
        rest = url[len("tos://"):]
        bucket, _, key = rest.partition("/")
        return cls(bucket, key)

    def __str__(self) -> str:
        return f"tos://{self.bucket}/{self.key}"

    def join(self, *parts: str) -> "TosUrl":
        key = "/".join([self.key.rstrip("/")] + [p.strip("/") for p in parts])
        return TosUrl(self.bucket, key)


class Tos:
    def __init__(self) -> None:
        import tos  # 只在 pod 上有

        ak = os.environ["TOS_ACCESS_KEY"]
        sk = os.environ["TOS_SECRET_KEY"]
        self.endpoint = os.environ.get("TOS_ENDPOINT", "tos-cn-beijing.volces.com")
        self.region = os.environ.get("TOS_REGION", "cn-beijing")
        self._tos = tos
        self.c = tos.TosClientV2(ak, sk, self.endpoint, self.region)

    def get_bytes(self, u: TosUrl) -> bytes:
        return self.c.get_object(u.bucket, u.key).read()

    def download(self, u: TosUrl, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c.get_object_to_file(u.bucket, u.key, path)

    def upload(self, path: str, u: TosUrl) -> None:
        self.c.put_object_from_file(u.bucket, u.key, path)

    def put_bytes(self, data: bytes, u: TosUrl) -> None:
        self.c.put_object(u.bucket, u.key, content=data)

    def exists(self, u: TosUrl) -> bool:
        try:
            self.c.head_object(u.bucket, u.key)
            return True
        except self._tos.exceptions.TosServerError as e:  # type: ignore[attr-defined]
            if e.status_code == 404:
                return False
            raise

    def presign(self, u: TosUrl, expires: int = 3 * 86400) -> str:
        out = self.c.pre_signed_url(self._tos.HttpMethodType.Http_Method_Get, u.bucket, u.key, expires)
        return out.signed_url

    def list(self, u: TosUrl):
        """遍历前缀下所有对象 (key, size)。"""
        token = None
        while True:
            r = self.c.list_objects_type2(u.bucket, prefix=u.key, continuation_token=token, max_keys=1000)
            for o in r.contents or []:
                yield o.key, o.size
            if not r.is_truncated:
                break
            token = r.next_continuation_token
