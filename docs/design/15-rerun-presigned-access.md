# 15 ReRun 经预签名访问质检台里登记的数据集（阶段一）

> 状态：**定稿**（2026-09-28 需求方确认开工，需求账本阶段 10 的 F10.1–F10.6，决策 D55）。§2 已随 C4 1.21.0 冻结（`signDataset`、`DatasetSignRequest` / `DatasetSignResponse`），§3 的链接格式已随 K4（F10.3）落地。
> 来源：安全评审要求 ReRun 去掉部署期预置的 TOS AK/SK。定下的方向是：访问密钥只录一次，存在质检台 Daemon 的封存库里；
> ReRun 读 TOS 时向 Daemon 要限时的预签名地址，浏览器里不再有 AK/SK。分阶段做，本篇只写第一阶段。
> 涉及两个仓库：本仓库（质检台）和 rerun 仓库（web viewer）。

## 给实施者的开工指引（先读这一节）

1. **目标**：在质检台的数据集列表或详情页点「可视化」，ReRun web viewer 打开这个数据集，读 TOS 用的全部是 Daemon 签的预签名地址。
   只覆盖质检台里登记过的私有 TOS 数据集。
2. **顺序**：T0 验证 → K1 签名模块 → K2 接口 → K3 契约与文档 → K4 链接 → R1–R4（ReRun）→ J1–J2 联调。
   K 与 R 可以并行，两边以 §2 的接口、§3 的链接格式为准。两边谁先上线都行（§3.2）。
3. **纪律**：质检台在 `feat/curator-v2` 上做，每个任务一个 commit，按路径提交；ReRun 在 rerun 仓库开分支，走 PR 进 `dev`。
   提交信息用英文，不加模型 co-author。密钥不进仓库、不进日志；T0 用的真实密钥只经环境变量传入，验证脚本不入库。
4. **读什么**：本篇 → `08-secrets-and-auth.md` §3、§6 → `03-rest-api.md` §7 → `backend/daemon/secrets/README.md` →
   rerun 仓库的 `crates/store/re_data_source/src/tos/client.rs` 与 `crates/viewer/re_viewer/src/app/session_restore.rs`。
5. **不做**：见 §7。尤其是 ReRun 的打开对话框、`/config.json` 里的密钥、catalog、原生会话，本期都不动。

## 0. 摘要

### 0.1 一句话

「可视化」的链接多带一个数据集编号；viewer 凭这个编号向 Daemon 要预签名地址，再拿地址直连 TOS。SK 不出 Daemon。

### 0.2 决策（候选，待确认后写进 `00-overview.md` §7）

| # | 决策 |
|---|---|
| D55 | ReRun 读 TOS 不再依赖部署期预置的 AK/SK：访问密钥只存在质检台 Daemon 的封存库里，ReRun 经 Daemon 代签的预签名地址直连 TOS（AWS SigV4 查询串签名，S3 兼容端点，只签读操作）。分阶段实施；阶段一只覆盖质检台里登记过的私有 TOS 数据集，签名范围由登记的数据集决定，调用方不能指定桶、密钥和地域 |

### 0.3 现状与本期改动

| 环节 | 现状 | 本期 |
|---|---|---|
| 「可视化」链接 | `?url=tos://…/?region=…`，不带任何密钥信息 | 多带 `curator_dataset=<数据集编号>`（§3） |
| viewer 用什么读 TOS | `/config.json` 下发的部署密钥，在浏览器里做 SigV4 头部签名 | 链接带编号时，改用 Daemon 签的预签名地址；不带时照旧 |
| Daemon 的签名接口 | `/media/sign`：只签任务的两个前缀，TOS 原生端点 | 新增 `POST /datasets/{id}/sign`（§2），S3 兼容端点 |
| 最近打开、会话恢复、分享链接 | 只记地址和地域 | 多记数据集编号，重新打开时重新要签名 |
| rrd 缓存、CORS 自助、打开对话框 | 用部署密钥 | 不变 |

## 1. 一次跳转的全过程

```
质检台页面                 ReRun web viewer（浏览器）           Daemon                    TOS
   │ 点「可视化」                  │                              │                        │
   │──新标签页 /?url=tos://…&curator_dataset=ds-…──▶│            │                        │
   │                              │ 解析出地址、地域、数据集编号    │                        │
   │                              │──POST /curation/api/v1/datasets/ds-…/sign──▶│          │
   │                              │                              │ 查数据集 → 取绑定的密钥  │
   │                              │                              │ 校验路径 → 本地算签名    │
   │                              │◀──────── 预签名地址（30 分钟）──│                        │
   │                              │──GET 预签名地址（可带 Range）──────────────────────────▶│
   │                              │◀──────────────────────── 数据 ─────────────────────────│
```

1. 链接里只有地址、地域、数据集编号，都不是秘密，也不会过期。
2. viewer 每读一个对象、每列一页，先看缓存里有没有没过期的地址，没有就向 Daemon 要。
3. 数据不经过 Daemon。Daemon 只做本地的 HMAC 计算，不因签名而访问 TOS。
4. 地址过期（或 TOS 回 403）时 viewer 重新要一次再重试，用户无感。
5. 从「最近打开」或会话恢复重新打开时，走的是同一条路。

## 2. 接口：`POST {base}/api/v1/datasets/{id}/sign`（C4）

### 2.1 请求

```jsonc
// Content-Type: application/json
{
  "ttl": 1800,
  "requests": [
    {"op": "get",  "key": "lerobot/droid_100/meta/info.json"},
    {"op": "list", "prefix": "lerobot/droid_100/", "delimiter": "/"},
    {"op": "list", "prefix": "lerobot/droid_100/", "continuation_token": "…"}
  ]
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `ttl` | 整数，60–3600，缺省 1800 | 与 `/media/sign` 相同 |
| `requests` | 数组，1–100 项 | 返回的地址与它一一对应、顺序相同 |
| `op: "get"` 的 `key` | 字符串 | 桶内的完整对象键，不以 `/` 开头 |
| `op: "list"` 的 `prefix` | 字符串 | 列举的前缀 |
| `delimiter` | 可选，只能是 `/` | |
| `continuation_token` | 可选 | 上一页 XML 里的 `NextContinuationToken` |
| `max_keys` | 可选，1–1000，缺省 1000 | |

对象键用桶内的完整键而不是相对路径：viewer 的 `TosClient` 处理的就是完整键，链接指向数据集的子目录时也不用换算。

### 2.2 响应

```jsonc
// 200，Cache-Control: no-store
{
  "expires_at": 1790000000000,          // 毫秒，与 SignedUrl.expires_at 同一口径
  "urls": [
    "https://<桶>.tos-s3-cn-beijing.volces.com/lerobot/droid_100/meta/info.json?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=…&X-Amz-Date=…&X-Amz-Expires=1800&X-Amz-SignedHeaders=host&X-Amz-Signature=…",
    "https://<桶>.tos-s3-cn-beijing.volces.com/?X-Amz-Algorithm=…&delimiter=%2F&list-type=2&max-keys=1000&prefix=lerobot%2Fdroid_100%2F&X-Amz-Signature=…"
  ]
}
```

列举地址的查询参数（`list-type=2`、`max-keys`、`prefix`、`delimiter`、`continuation-token`）与 viewer 今天自己发的完全一样，
所以返回的 XML 和解析代码都不用变。

### 2.3 校验规则

1. 数据集存在且属于当前 owner；`source` 必须是 `tos`。
2. 绑定的访问密钥还在、解得开：`svc.tos_key(ds.credential_id, owner=…, role="input")`，和 `/media/sign` 的 `input` 一路相同。
3. 密钥带自定义 endpoint 的本期不支持，拒绝并说明。
4. 数据集前缀 = `ds.uri` 去掉 `tos://<桶>/` 和结尾的 `/`；为空（整个桶）时拒绝。
5. `key` 与 `prefix` **只校验、不改写**。出现下面任何一种就拒绝：反斜杠、控制字符、以 `/` 开头、`//`、`.` 或 `..` 段、
   解码后是 `.` / `..` 或含 `/` 的段。`/media/sign` 会把多余的斜杠归一化掉，这里不能：对象键是字面量，`a//b` 和 `a/b` 是两个对象。
6. `key` 必须以「数据集前缀 + `/`」开头，且后面不为空；`prefix` 必须以「数据集前缀 + `/`」开头。
   这样登记在 `lerobot/droid_100` 的数据集签不出 `lerobot/droid_100_v2/…`。
7. 桶、地域都取自登记的数据集，请求里不能指定。地域 = `ds.region`，没有时用密钥的地域。
8. 只签 GET（读对象、列举）。

### 2.4 签名

- AWS Signature V4 的查询串签名：service 是 `s3`，`X-Amz-SignedHeaders=host`，payload 是 `UNSIGNED-PAYLOAD`。
  `Range` 不在签名里，所以一个对象的地址可以反复用来取不同的字节范围。
- 主机是公网、虚拟主机风格的 S3 兼容端点 `<桶>.tos-s3-<地域>.volces.com`，不管 Pod 自己走的是不是内网端点（08 §6 第 3 条）。
- 密钥带 session token 时加 `X-Amz-Security-Token`。
- 用 S3 兼容端点而不是 TOS 原生端点，是因为 viewer 的客户端按 S3 协议写的（列举返回 XML、元数据头是 `x-amz-meta-*`），
  同一份代码还要读 AWS S3 和 MinIO。让 Daemon 多写一份签名，比让 viewer 多学一种方言便宜。
- 参考实现在附录 A，只用标准库，已对 AWS 文档里的公开样例验证。

### 2.5 错误

| 情况 | 返回 |
|---|---|
| 数据集不存在 | 404 `not_found` |
| 绑定的密钥已被删除 | 404 `not_found`，`details.reason: credential_missing`（`unavailable()` 现有口径） |
| 密钥解不开 | 500 `internal`，`details.reason: secret_unreadable`（同上） |
| `source` 不是 `tos`、自定义 endpoint、路径越界或不合法、`ttl` 越界、条数超限 | 400 `validation_failed`，`details.errors` 指明是第几项、什么问题 |
| 没有 `Content-Type: application/json`，或 `Sec-Fetch-Site` 不是同源 | 沿用 `read_json_body` 的拒绝 |

一项不合格，整个请求失败，不返回部分结果。

### 2.6 给安全评审的要点

- SK 不出 Daemon。响应里只有预签名地址，其中带 AK 的 ID 和签名，不带 SK。
- 预签名地址是限时的凭据：只发给同源、已登录的调用方，响应 `no-store`，Daemon 的日志不打印响应体。
- 签名范围由登记的数据集决定，调用方换不了桶、密钥和地域，也出不了数据集的前缀。
- 这个接口是 POST，带 `read_json_body` 的两条防护（必须 `application/json`、必须同源），别的网站驱动不了它。
- 审计：同一个人对同一个数据集，一小时内第一次签名记一条事件 `dataset.viewer_sign`（资源是数据集编号）。不逐次记，量太大。
- 单租户前提不变（08 §1）：能登录的人可以读到每个登记数据集的内容，这和今天在控制台里预览 episode 是同一个权限。

## 3. 链接格式

### 3.1 格式

```
<同域>/<上一级>/?url=<encodeURIComponent("tos://<桶>/<前缀>/<数据集名>/?region=<地域>&curator_dataset=<数据集编号>")>
```

- 编号放在 tos 地址自己的查询参数里，和 `region` 并列。viewer 的 `parse_tos_url` 本来就在解析这一段，链路上不用多传一个页面参数。
- 只有 `source = tos` 的数据集带 `curator_dataset`。HuggingFace 缓存桶的链接不变，本地挂载的仍然置灰。
- viewer 按 `^(ds-[a-z]{9}|ds_[0-9A-Za-z]+)$` 校验编号，不合格就当作没有，并在日志里告警。

### 3.2 兼容与上线顺序

| 质检台 | viewer | 结果 |
|---|---|---|
| 新 | 旧 | 旧 viewer 只认 `region`，多出来的参数被忽略，照旧用部署密钥打开 |
| 旧 | 新 | 链接里没有编号，viewer 走原来的路 |
| 新 | 新 | 走预签名 |

所以两边上线不分先后，也可以单独回滚任何一边。

## 4. 任务拆解

人天是粗估，合计约 11 人天：验证与联调 2，质检台 3.5，ReRun 5.5。

登记到需求账本时建议这样分（编号是候选）：

| 候选编号 | 内容 | 对应任务 |
|---|---|---|
| F10.1 | TOS 行为验证与 SigV4 签名模块 | T0、K1 |
| F10.2 | 数据集签名接口与契约 | K2、K3 |
| F10.3 | 「可视化」链接带数据集编号 | K4 |
| F10.4 | ReRun：链接解析与远端签名访问 | R1、R2 |
| F10.5 | ReRun：打开链路、最近打开、会话恢复、错误提示 | R3、R4、R5 |
| F10.6 | 联调与集群验收 | J1、J2 |

### 4.0 T0 先验证 TOS 的行为（0.5 人天）

后面所有工作都建立在这三件事上，先花半天确认：

- [x] 预签名的 GET 对象，带 `Range: bytes=0-99`，在 S3 兼容端点上返回 206。
- [x] 预签名的 ListObjectsV2（带 `prefix`、`delimiter`，以及翻页的 `continuation-token`）返回 `<ListBucketResult>`。
- [x] 在已部署的 viewer 页面的浏览器控制台里 `fetch(地址, {headers: {Range: 'bytes=0-99'}})` 能成功，
      确认没有 `Authorization` 头时桶上现有的 CORS 规则仍然够用。

结果（2026-09-28，桶 `curation`、cn-beijing、数据集 `datasets/so101-pick-place-v2`）：改为直接用做好的
`POST /datasets/{id}/sign` 取地址再请求 TOS，三项全部通过——同一个地址换 `Range` 反复取都是 206；列举与翻页都返回
`ListBucketResult`，翻页令牌里的 `+`、`/` 也签得对；改坏签名得到 403 `SignatureDoesNotMatch`。第三项在 J1 的浏览器里验：
来源 `http://localhost:9091` 不带 `Authorization` 直连 TOS，读对象 206、列举 200，桶上 catalog 装的 CORS 规则够用。
§6 第 1 条的备选方案不需要了。

做法：拿附录 A 的函数写一个一次性脚本，在有真实密钥的机器上跑，密钥经环境变量传入，用 `curl` 验证前两项。
catalog 的 `/catalog/presign` 已经在同一个端点上签 GET 对象，所以第一项风险低；没有先例的是第二项。
第二项不通过时改用 §6 第 1 条的备选方案，接口和 viewer 的列举部分要相应调整，先停下来同步。

### 4.1 质检台（本仓库）

**K1 签名模块（0.5 人天）**

- [ ] 新文件 `backend/daemon/secrets/sigv4.py`：一个纯函数，入参是方法、端点、桶、对象键、查询参数、密钥、地域、有效期、当前时间，
      返回地址。不做任何 I/O，不读时钟（时间由调用方传入，测试才能固定）。
- [ ] `backend/daemon/secrets/tos.py` 加 `s3_public_endpoint(region)`，返回 `https://tos-s3-<region>.volces.com`。
- [ ] 镜像和 CI 是 Python 3.10，别用 3.11 以后才有的语法和标准库。
- [ ] 测试 `backend/tests/secrets/test_sigv4.py`：
  - AWS 文档的公开样例（附录 A 里的那一组），签名逐字相等；
  - 对象键里有空格、中文、`+`、`%` 时的转义；路径里的 `/` 不转义，查询值里的 `/` 转义成 `%2F`；
  - 查询参数按名字排序；带 session token 时多出 `X-Amz-Security-Token`；
  - 返回的地址里没有 SK。

**K2 接口（1.5 人天）**

- [ ] 路由放在 `backend/daemon/routes/media.py`（它的职责就是「给浏览器的 TOS 访问」），路径
      `/datasets/{dataset_id:ds_id}/sign`。`ds_id` 这个路径转换器在 `routes/datasets.py` 里注册，要先 import 它
      （`datasets_exec.py` 第 18 行是现成的写法）。
- [ ] 路径校验放在 `backend/daemon/secrets/presign.py`，新增两个函数（对象键一个、列举前缀一个），不合格抛现有的 `BadPath`。
      不要复用 `relative_key`：它会改写路径（§2.3 第 5 条）。
- [ ] 处理顺序：`read_json_body(required=True)` → `validate("DatasetSignRequest", body)` → 取数据集 → §2.3 的校验 →
      逐项签名 → 返回，带 `Cache-Control: no-store`。不走 `write()`：它没有状态变更，不需要幂等键。
- [ ] 审计：`audit(request, "dataset.viewer_sign", ds.id, {...})`，按「操作人 + 数据集」在内存里去重，一小时一条。
- [ ] 测试 `backend/tests/secrets/test_dataset_sign.py`（沿用 `secret_client`、`add_access_key`、`fake_tos`）：
  - 读对象、列举各签一条，顺序与请求一致，主机是 `<桶>.tos-s3-<地域>.volces.com`；
  - 部署端点是内网（`.ivolces.com`）时，签出来的仍是公网主机；
  - 越界：兄弟前缀 `droid_100_v2/`、`..`、`//`、反斜杠、控制字符、百分号编码的点和斜杠、以 `/` 开头、空键；
  - `ttl` 30 与 7200、0 项与 101 项；
  - `public`、`local` 的数据集；密钥被删；数据集不存在；密钥带自定义 endpoint；
  - 没有 `Content-Type: application/json`、`Sec-Fetch-Site: cross-site`；
  - 审计事件一小时内只有一条。
- [ ] `backend/tests/secrets/test_no_secret_leaks.py` 把新接口加进去：响应、日志、数据库文件里都没有埋下的 SK。

**K3 契约与文档（1 人天）**

- [ ] `docs/contracts/openapi.yaml`：新路径，新 Schema `DatasetSignRequest`、`DatasetSignResponse`；`info.version` 次版本加一
      （别的会话也在改 C4，以提交时的版本为准）。
- [ ] `docs/contracts/examples/` 补一份合法、一份不合法的示例；`docs/contracts/SUMMARY.md` 更新。
- [ ] `cd backend && ../.venv/bin/python -m curation.contracts lock`。
- [ ] `cd frontend && npm run gen:api`；`src/mocks/handlers.ts` 给新接口加处理器（每个 C4 接口都要有）。
- [ ] 设计文档：`03-rest-api.md` §2 的表和 §7 加一小节；`08-secrets-and-auth.md` §6 加一段，写明 §2.3 的规则和 §2.6 的要点；
      `00-overview.md` §7 加 D55；本篇状态改为定稿；`CLAUDE.md` 的设计篇目表加第 15 篇。
- [ ] `backend/daemon/README.md`、`backend/daemon/secrets/README.md` 的接口表和「手动验证步骤」。

**K4 「可视化」链接（0.5 人天）**

- [ ] `frontend/src/lib/rerun.ts`：`rerunViewerUrl` 的入参多要 `id`、`source`；`source === 'tos'` 时在 tos 地址后面接
      `curator_dataset`。文件头的注释里「Credentials come from the viewer's own deployment configuration」要改。
- [ ] `frontend/src/features/datasets/VisualizeButton.tsx`：属性类型改为 `Pick<DatasetItem, 'id' | 'source' | 'uri' | 'region'>`。
      两个调用处（数据集列表、详情页）传的都是整条 `d`，不用动。
- [ ] 测试：`src/lib/rerun.test.ts` 加「tos 带编号」「public 不带编号」「没有地域时只有编号」三条；
      `src/pages/datasets/DatasetPages.test.tsx` 里有四处对 `href` 的断言（第 43、45、53、184 行），私有 TOS 的三处要加上编号。
- [ ] 文档：`07-frontend.md` §4.4「可视化」一段（现在写的是「访问密钥由 ReRun 自己的部署配置提供，这里不传」）；
      `frontend/README.md` 的手动验证步骤。

质检台这边提交前跑：

```bash
cd backend && ../.venv/bin/python -m pytest -q tests/secrets tests/daemon tests/contracts && ../.venv/bin/python -m curation.contracts check
cd frontend && npm run check:api && npm run lint && npm run typecheck && npm test && npm run build
```

本期不碰判决，不需要跑对账基线。

### 4.2 ReRun（rerun 仓库）

**R1 链接解析与分享链接（0.5 人天）**

- [ ] `crates/viewer/re_viewer_context/src/open_url.rs`：
  - `parse_tos_url` 多解析 `curator_dataset`，按 §3.1 的格式校验；
  - `ViewerOpenUrl::TosDataset` 多一个 `curator_dataset: Option<String>`；
  - 转回字符串时（分享链接）把它写回去；
  - `from_data_source` 里现在靠 `dataset_region_of(url)` 找回地域，编号同样要找得回来（见 R3）。
- [ ] `crates/viewer/re_viewer_context/src/command_sender.rs`：`SystemCommand::LoadTosDataset` 多一个字段。
      `re_test_context` 里的匹配分支写的是 `{ .. }`，不用改。
- [ ] 测试：`open_url.rs` 现有的 `test_viewer_open_url_tos_dataset` 旁边加带编号的往返、编号不合格时被忽略。

**R2 访问方式与远端签名（2 人天）**

全部 12 处 TOS 请求都经过 `TosClient::signed_request` 一个函数（`client.rs` 第 723 行），改动集中在这里。

- [ ] 引入「怎么取得访问权」的枚举（名字可调，下面叫 `TosAccess`）：
  - `Keys(TosCredentials)`：现有的本地签名，原生 viewer、打开对话框、rrd 缓存都还用它；
  - `CuratorDataset { endpoint, dataset_id, api_base }`：向质检台要预签名地址。
  `TosCredentials` 本身不动，并实现 `From<TosCredentials> for TosAccess`，现有的 `TosClient::new(credentials, bucket)` 调用处就不用改。
- [ ] `TosDatasetSource.credentials` 改成 `access: TosAccess`（`lerobot_stream.rs`）。构造它的地方有五处：
      `open_tos_modal.rs` 第 467 行、`session_restore.rs` 第 90 和 164 行、`rrd_convert.rs` 第 83 行、`tos_smoke.rs` 第 136 行。
- [ ] `signed_request` 按访问方式分流。远端模式下：
  - 只支持读对象和列举；PUT、POST、DELETE、HEAD、`?cors` 直接报错。数据集的读取链路（`TosStore`）只用到
    `list_objects`、`list_dir`、`get_object_once`，够用；
  - 先查缓存，没有或快过期就 POST `{api_base}/datasets/{id}/sign`；
  - 拿返回的地址原样发 GET，只带不参与签名的头：`range`，以及 wasm 下现有的 `cache-control: no-cache`。
    不要带 `authorization`、`x-amz-date`、`x-amz-content-sha256`；
  - 地址原样使用，不要重新编码。
- [ ] 签名请求的写法：JSON 正文，`Content-Type: application/json`，凭据模式用同源
      （和 `viewer_config.rs` 取 `config.json` 时的 `with_credentials(ehttp::Credentials::SameOrigin)` 一样）。
      少了前者 Daemon 会拒绝，少了后者浏览器不带登录信息。
- [ ] 缓存：键是「方法 + 路径 + 查询参数」，值是地址和过期时间；离过期不到 60 秒就当作没有。
      TOS 回 403 时丢掉这一条、重新签一次、重试一次，仍然 403 才报错。
- [ ] 有效期先写死 1800 秒；`config.json` 里加一个可选的 `curator_sign_ttl`，联调时设成 60 用来验证重签。
- [ ] 远端模式只在 wasm 下可用。原生 viewer 没有同源的 Daemon，也没有浏览器的登录状态。
- [ ] wasm 下 `signed_request` 开头的 `ensure_cors_via_server_once` 保留，地域从 `TosAccess` 取。
- [ ] `api_base` 由上层传进来：`re_data_source` 不能依赖 `re_viewer_context`，而控制台地址在 `daft_link::base_url()` 里。
      `config.json` 的 `daft_url` 配成了别的域名时远端签名用不了（跨域，Daemon 会拒绝），按没有编号处理并告警。
- [ ] 测试：请求正文的拼装、缓存的命中与过期（时间可注入）、403 后只重试一次、远端模式下写操作被拒。

**R3 打开链路、最近打开、会话恢复（1.5 人天）**

- [ ] `crates/viewer/re_viewer/src/app/mod.rs`：`pending_tos_opens` 现在是 `(TosLocation, String)`，改成带编号的结构。
- [ ] `app/command_handling.rs` 第 531 行：把编号一起入队。
- [ ] `app/session_restore.rs` 的 `process_pending_tos_opens`：带编号且在 wasm 下，用 `TosAccess::CuratorDataset` 构造数据源，
      **不再要求 `config.has_tos_credentials()`**；没有编号的照旧。仍然要等 `config.json`，因为 rrd 缓存的配置在里面。
- [ ] 同文件的 `maybe_restore_session`：带编号的条目同样不要求部署密钥。
- [ ] `recent_datasets.rs`：`RecentDataset` 加 `curator_dataset: Option<String>`，标 `#[serde(default)]`，旧的存档才读得回来。
      这个文件的规矩是「只存非敏感信息」，编号符合。
- [ ] `app/add_data_source.rs` 的 `remember_recent_dataset`：地域现在从 `source.credentials.endpoint` 取，改成从 `access` 取，并记下编号。
- [ ] `app_state.rs` 第 770 行附近：点「最近打开」里带编号的条目，直接发 `LoadTosDataset`，不弹对话框；不带编号的照旧弹。
- [ ] `crates/store/re_data_source/src/lerobot_remote.rs`：仿照 `DATASET_REGIONS`，加一张「地址 → 数据集编号」的表，
      打开时记下，分享链接用它找回编号。
- [ ] 原生 viewer 遇到带编号的链接：忽略编号，走原来的路，日志里说明一句。
- [ ] 测试：`RecentDataset` 旧存档能读、新字段往返。

**R4 错误提示与文档（1 人天）**

- [ ] Daemon 的错误体是 `{"error": {"code", "message", "details"}}`，`message` 是中文，直接显示给用户即可。几种要有明确说法的情况：
  - 404 且 `details.reason` 是 `credential_missing`：数据集绑定的访问密钥已被删除，请到质检台重新指定；
  - 404 其余：这个数据集在质检台里已经删除；
  - 401：登录已失效，刷新页面重新登录；
  - 网络错误或 5xx：质检台暂时连不上，稍后重试。
- [ ] 文案走 `tr` / `trf!`，中英文都要有。
- [ ] 文档：`docs/release/user-guide/01-viewer.md`（从质检台跳转过来的数据集怎么取得访问权）、
      `deploy/README.md` 的「Credential model」一节、`CHANGELOG.md`。
- [ ] 提交前：`pixi run rs-fmt`、`pixi run rs-check`、`cargo test -p re_data_source -p re_viewer_context -p re_viewer`、
      `pixi run rerun-build-web`（确认 wasm 能编过）。

**R5 顺手项：统一登录域（可选，0.1 人天）**

viewer 的 nginx 用的 realm 是 `rerun`（`deploy/entrypoint.sh` 第 45 行），Daemon 的是 `Robot Data Curation`。
两边校验的是同一张 htpasswd，但浏览器按 realm 记登录状态，所以「先进控制台、再进 viewer」会再弹一次登录框，
而「可视化」正是这个方向。把 nginx 的改成 `Robot Data Curation` 就不弹了。这个问题今天就有，不是本期引入的。

### 4.3 联调

**J1 本机（0.5 人天）**

viewer 和 Daemon 必须同源：Daemon 不开 CORS，还会拒绝 `Sec-Fetch-Site` 不是同源的写请求。
用一个反向代理把两者放到同一个端口下：

```nginx
server {
    listen 9091;
    location /curation/ { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; proxy_buffering off; }
    location /api/      { proxy_pass http://127.0.0.1:9094; }   # catalog: ensure-cors
    location /          { proxy_pass http://127.0.0.1:9191; }   # web viewer
}
```

代理占 9091：viewer 自动配的 CORS 规则里，本机来源写的就是 `http://127.0.0.1:9091`，换别的端口桶会拒绝。
viewer 容器（rerun 仓库 `deploy/docker-compose.yml` 的 `viewer`）改映射到 9191。Daemon 用 `.claude/launch.json` 的
`curator-daemon-dev`（`/curation` 前缀、不鉴权、8080）。在控制台里添加一把真实的访问密钥、登记一个数据集，
从 `http://127.0.0.1:9091/curation` 进。

2026-09-28 实测（没有起 viewer 容器，改用 debug 版 web viewer 的静态文件加一个 Python 标准库写的同源代理，脚本不入库；
viewer 的 `config.json` 里**没有任何** TOS 密钥）：从控制台「可视化」进入，viewer 只经 `/sign` 取地址、直连 TOS 读完元数据并逐条载入
episode（画面、曲线、任务文本正常），§5 的第 1–9、11 步符合期望（第 2 步在浏览器资源记录里核对：地址带签名、只签 `host`；
第 4 步更强——根本没有部署密钥；第 5 步把 `curator_sign_ttl` 设成 60，过期后重新载入同一 episode，同一对象换了新签名照常 206；
第 8 步用不存在的编号验）。会话恢复另有一个与本篇无关的既有问题：`App::save` 拿规范化后的 application id 与原始地址比，
`open_at_exit` 永远打不上，web 上重启后不会自动重开任何数据集（已另开任务修）；手工把存档里的标记设为 true 后，
带编号的条目能经代签恢复。第 10 步（缓存桶数据集）留到 J2。

**J2 集群（1 人天）**

两边出镜像，升级 galbot（流程见 `deploy/README.md`），按 §5 验收。

## 5. 验收

| # | 步骤 | 期望 |
|---|---|---|
| 1 | 在数据集列表点一个私有 TOS 数据集的「可视化」 | 新标签页打开 viewer，episode 列表出现并逐条加载，不弹打开对话框 |
| 2 | 开发者工具 Network 里筛 `tos-s3` | 请求地址带 `X-Amz-Signature`，请求头里没有 `Authorization` |
| 3 | Network 里筛 `/sign` | `POST /curation/api/v1/datasets/<编号>/sign` 返回 200，响应里没有 SK |
| 4 | 找一个部署密钥读不了、数据集绑定的密钥读得了的桶，重复第 1 步 | 能打开。这一条证明读数据用的确实不是部署密钥。桶的 CORS 要事先配好（§6 第 2 条） |
| 5 | `curator_sign_ttl` 设成 60，打开数据集后等两分钟，再点一个还没加载的 episode | 正常加载，Network 里能看到新的 `/sign` 请求 |
| 6 | 关掉标签页，第二天从 viewer 欢迎页的「最近打开」点这个数据集 | 直接打开，不弹对话框 |
| 7 | 复制 viewer 里的分享链接，在另一个浏览器里登录后打开 | 能打开，链接里带 `curator_dataset` |
| 8 | 在质检台里删掉这个数据集，再点第 6 步的条目 | 提示数据集已在质检台里删除，不是一个看不懂的网络错误 |
| 9 | 打开一条不带 `curator_dataset` 的旧链接 | 行为和升级前一样 |
| 10 | HuggingFace 缓存桶的数据集点「可视化」 | 链接里没有 `curator_dataset`，行为和升级前一样 |
| 11 | 质检台的审计事件 | 第 1 步产生一条 `dataset.viewer_sign`，一小时内重复打开不再增加 |

第 1–3、5–11 步同时写进两边 README 的「手动验证步骤」。

## 6. 风险与待定

1. **TOS 认不认预签名的列举**。T0 已验证：认（2026-09-28，含翻页）。以下备选方案不再需要：不认的话，列举改由 Daemon 代做：新增 `POST /datasets/{id}/list`，Daemon 用 TOS SDK 列举后
   返回 JSON（对象键、大小、ETag、子目录、是否还有下一页），viewer 的 `list_objects`、`list_dir` 在远端模式下改调它。
   代价是 Daemon 要真的访问 TOS，viewer 多一条解析路径，估计多 1.5 人天。
2. **CORS**。浏览器直连 TOS 要求桶上有 viewer 的 CORS 规则。现在的自助配置（`/api/ensure-cors`）用的是部署密钥，
   部署密钥管不了的桶要手动配（rerun 仓库 `deploy/README.md` 的「Bucket CORS」一节）。下一阶段把自助配置挪进 Daemon，用数据集绑定的密钥执行。
3. **登录框**。见 R5。浏览器什么时候自动带上 Basic 凭据，要在 Chrome 和 Safari 里各试一次；viewer 调 `/sign` 收到 401 时要有明确提示（R4）。
4. **时钟**。签名用的是 Daemon 的时钟，和 TOS 相差超过 15 分钟会被拒。集群里有 NTP，本机联调时留意。
5. **rrd 缓存**。本期不变，仍用部署密钥读写缓存桶。部署密钥去掉之后缓存会自动停用（`ViewerConfig::rrd_artifacts` 在没有密钥时返回 `None`），
   到时要定用哪把密钥写。
6. **Daemon 是单副本**。签名是本地计算，很轻；真不够用时加 CPU，或者把代签拆成无状态的独立进程。不能靠加副本，SQLite 在 RWO 盘上。

## 7. 不在本期（后续阶段）

| 项 | 说明 |
|---|---|
| ReRun 打开对话框里的密钥下拉与「新增密钥」 | 在 ReRun 里直接填地址打开，本期仍用原来的机制。做这一项时接口是 `POST /credentials/{id}/sign`，多一个 `uri` 参数 |
| 去掉 `/config.json` 里的 AK/SK，Chart 去掉相应的键 | 要等上一项和下面几项都做完 |
| catalog（注册、给 SDK 的预签名、CORS 自助） | 方向：catalog 经集群内网向 Daemon 要签名，用服务令牌鉴权 |
| 原生会话、本机原生 viewer | 没有同源的 Daemon |
| rrd 缓存走预签名 | 需要签写操作，范围限定在缓存前缀之内 |
| HuggingFace 缓存桶、本地挂载的数据集 | 前者是匿名桶，后者 viewer 读不到 |
| 「质检」反向跳转带数据集编号 | viewer 已经记下了编号，控制台这边按编号预选即可 |

## 附录 A SigV4 查询串签名的参考实现

只用标准库，Python 3.10 可用。2026-09-28 对 AWS 文档「Authenticating Requests: Using Query Parameters」的样例验证通过：
桶 `examplebucket`、对象 `test.txt`、地域 `us-east-1`、时间 `20130524T000000Z`、有效期 86400、
AK `AKIAIOSFODNN7EXAMPLE`、SK `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`，
签名应为 `aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404`。这一组直接用作 K1 的第一条单测。

```python
import hashlib
import hmac
from urllib.parse import quote


def _enc(text: str, safe: str = "") -> str:
    # RFC 3986 unreserved characters stay; everything else becomes %XX (UTF-8).
    return quote(text, safe=safe + "-_.~")


def presign(method, host, key, query, ak, sk, region, ttl_s, amz_date, token=None):
    """``key`` is '' for a bucket-level request (listing); ``amz_date`` is YYYYMMDDTHHMMSSZ."""
    date = amz_date[:8]
    scope = f"{date}/{region}/s3/aws4_request"
    params = dict(query)
    params.update({
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{ak}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(ttl_s)),
        "X-Amz-SignedHeaders": "host",
    })
    if token:
        params["X-Amz-Security-Token"] = token
    canonical_query = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(params.items()))
    path = "/" + _enc(key, safe="/")            # S3 encodes the path once and keeps '/'
    canonical_request = "\n".join(
        [method, path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope,
         hashlib.sha256(canonical_request.encode()).hexdigest()])
    signing_key = ("AWS4" + sk).encode()
    for part in (date, region, "s3", "aws4_request"):
        signing_key = hmac.new(signing_key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{path}?{canonical_query}&X-Amz-Signature={signature}"
```

列举时 `query` 传 `{"list-type": "2", "max-keys": "1000", "prefix": …}`，按需加 `delimiter`、`continuation-token`。
