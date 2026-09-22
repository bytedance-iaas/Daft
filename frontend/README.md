# Curator v2 前端（W10）

数据质检平台的网页控制台：React 18 + TypeScript（strict）+ Arco Design + React Router 6 + TanStack Query + Vite + ECharts。
构建产物是一堆静态文件，由 Daemon 托管；页面上的所有数据都向 Daemon 要（D17），浏览器只在 `localStorage` 里记几项界面偏好（每页条数、上次用的访问密钥之类）。

设计依据：`docs/design/07-frontend.md`（主规格）、`03-rest-api.md`，契约 `docs/contracts/openapi.yaml`（C4 1.4.0，请求和响应的形状只认它）与 `modules.json`（C1），静态稿在 `mockups/`（只读参考）。

## 目录

| 路径 | 内容 |
|---|---|
| `src/api/` | 由契约生成的类型 `schema.d.ts`（`npm run gen:api`，不要手改）、薄客户端、错误、查询键、SSE |
| `src/pages/` | 八个页面：概览、数据集、任务列表、新建任务（两屏）、任务详情、质检报告、人工裁决、密钥与资源 |
| `src/features/` | 几页共用的块：预检、数据集登记、任务操作、视频签名、逐条下钻抽屉、密钥抽屉 |
| `src/lib/` | 纯函数（深链解析、表单模型、裁决规则、报告取值……），都有单测 |
| `src/locales/zh.ts` | 全部界面文案，一个文件 |
| `src/mocks/` | MSW 模拟：内存里的一套「模拟世界」，每个 C4 接口都有处理器，数字和静态稿一致 |
| `src/test/` | 测试环境：jsdom 补丁、契约校验器、渲染与 Arco 操作的小工具 |
| `scripts/` | 类型生成与校验、`serve-dist.mjs`（按 Daemon 的方式托管构建产物） |

## 本地环境

Node 20.19 及以上或 22.13 及以上（见 `package.json` 的 `engines`）。依赖版本全部钉死，装依赖一律用 `npm ci`：

```bash
cd frontend
npm ci
```

要增减依赖时注意：npm 10.9.2 解析依赖树会崩（`Cannot read properties of null (reading 'edgesOut')`），用 `npx -y npm@11 install <包名>@<版本>` 改 `package-lock.json`；`npm ci` 用 npm 10 没问题。

## 常用命令

| 命令 | 作用 |
|---|---|
| `npm run dev` | 开发服务器 http://localhost:5173/ ，默认接模拟数据 |
| `npm run lint` | ESLint，零警告 |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest 全部单测与页面流程测试（本机约 15 秒） |
| `npm run build` | 生产构建到 `dist/`，不含任何模拟代码 |
| `npm run build:demo` | 带模拟数据的构建（演示用，别部署） |
| `npm run serve:dist` | 按 Daemon 的方式托管 `dist/`：前缀、SPA 回退、可选接口代理 |
| `npm run gen:api` | 从 `../docs/contracts/openapi.yaml` 重新生成 `src/api/schema.d.ts` |
| `npm run check:api` | 生成结果和提交的文件不一致就失败（CI 用） |

## 开发

- **默认用模拟数据**：`npm run dev` 启动后浏览器里由 Service Worker 应答所有接口，SSE 也有模拟。打不开 Service Worker 的环境（一些内嵌浏览器、无痕模式）自动改成在页面里拦截 fetch，此时 SSE 不可用，页面会自己降级成 5 秒轮询 —— 这条路径本身也值得看。
- **接真的 Daemon**：`VITE_API_TARGET=http://127.0.0.1:8080 npm run dev`，开发服务器把 `{前缀}/api` 和 `{前缀}/events` 代理过去，模拟数据关闭。
- **挂载前缀**：`CURATOR_BASE=/curation npm run dev`，然后打开 http://localhost:5173/curation/ 。开发服务器在 `index.html` 里做和 Daemon 一样的注入。

## 构建与部署：Daemon 要做的事

`npm run build` 的产物在 `frontend/dist/`：`index.html`、`favicon.svg`、`assets/*`（文件名带内容哈希）。资源路径全部是相对的，同一份产物能挂在任何前缀下。Daemon 托管时要做三件事：

1. **静态文件**：`{base}/` 下按路径返回 `dist/` 里的文件。`assets/*` 可以长缓存（`Cache-Control: public, max-age=31536000, immutable`），`index.html` 用 `no-cache`。
2. **SPA 回退**：`{base}/` 下不是文件、也不在 `{base}/api/`、`{base}/events/` 下的 GET 请求，一律返回 `index.html`（刷新 `/curation/tasks/<id>/report` 不能 404）。`assets/` 下找不到的文件返回 404，不要回退。
3. **注入前缀**：把 `index.html` 里的标记 `<!-- curator:base -->` 替换成

   ```html
   <base href="/curation/"><script>window.__CURATOR_BASE__ = "/curation";</script>
   ```

   部署在根路径时是 `<base href="/"><script>window.__CURATOR_BASE__ = "";</script>`。前缀只能含 URL 路径字符（`[A-Za-z0-9._~-/]`），写进 JS 时用 JSON 编码。`<base>` 必须是静态注入的：浏览器的预加载扫描器只认 HTML 里现成的 `<base>`，否则深链页面会先去错误的路径预取 `./assets/*`。没注入时页面会从地址推断前缀、自己补一个 `<base>`，但那只是兜底。

   可选：在同一处再注入站点开关 `window.__CURATOR_FEATURES__ = { local_input: true, home_region: "cn-beijing" }`（本地输入源是否开放、站点所在地域；C4 没有站点配置接口，见下文「契约缺口」）。

路由器的 basename、接口前缀 `{base}/api/v1`、SSE 地址 `{base}/events/tasks/{id}` 都从 `window.__CURATOR_BASE__` 拼出来，代码里没有写死的路径（ESLint 规则拦着 `/api/`、`/events/` 字面量）。

照着 Daemon 的做法在本机看一遍构建产物：

```bash
npm run build:demo                                   # 带模拟数据
npm run serve:dist -- --base /curation --port 4173   # http://localhost:4173/curation/
# 生产构建 + 真 Daemon：
npm run build && npm run serve:dist -- --base /curation --api http://127.0.0.1:8080
```

## 类型、契约与测试

- `src/api/schema.d.ts` 由 openapi-typescript 从 C4 生成（外部 `$ref` 到 `cli/*.schema.json` 都能解析），客户端 `src/api/client.ts` 在它上面用 openapi-fetch，路径、参数、请求体全有类型；非 2xx 统一变成 `ApiError`（`code` 给程序，`message` 是给人看的中文）。每个写请求都带 `Content-Type: application/json`，没有请求体也带（C4 1.2 起的要求）。改了 C4 要跑 `npm run gen:api`。
- 模拟数据不会和契约走样：`src/mocks/contract.test.ts` 把每个样例和每个接口的模拟响应都拿 C4 校验一遍，页面测试里 UI 发出的每个请求体也会被校验（违反契约的测试直接失败）。C4 1.1.0 有三处 `allOf` 组合了封闭 schema，没有实例能通过；1.2.0 修好了 `Task.vlm`，1.3.0 修好了另外两处，测试按契约版本判断还要不要打补丁。
- `CURATOR_CONTRACTS=<目录> npm test` 用另一份契约跑全部测试（目录结构同 `docs/contracts/`），用来提前试新的契约修订。
- 页面测试用 Vitest + Testing Library + MSW，覆盖：客户端与错误、深链解析（v1 的全部用例）、param_schema 生成的表单、指纹变化对话框、SSE 断开后降级轮询、所有必填项的红 * 与校验、每一页的主要流程、裁决三条纪律、视频地址失效自动重签。

## 手动验证（模拟数据）

`npm run dev`，打开 http://localhost:5173/ 。模拟世界讲的是静态稿里那套故事：主角是任务「droid 前 50 条质检」。

1. **概览**：「需要处理」一栏有错误任务 1 个、待裁决 42 条（分布在 2 个任务，点「看是哪些任务」列出来）、交付待导出、数据集有变化、密钥和 VLM 后端验证失败各 1；「正在运行」有进度条；近 7 天的 Token 柱状图。每一项点进去是筛好的列表。
2. **数据集**：5 个已登记的数据集，droid-200 显示「有变化，待重新预检」。点「重新检查」→ 弹出指纹变化对话框（新增 12 个文件）→「重新预检」后变成「一致」。「添加数据集」：必填项有红 *，填 `tos://pai-kit-datasets/lerobot/brand_new` 和访问密钥后自动预检，保存后进详情；同一地址再添加会打开已有的那条。删除 umi_640_notask 会被拒（有未结束的任务在用）。详情页有预检结果、模块可用性、指纹变化记录、跑过的任务，「新建质检任务」带着数据集进新建页。
3. **任务列表**：页码分页、状态中文名（`completed_with_errors` 显示「错误」）、系统暂停的图标与悬停提示、模块列只放摘要（悬停看全部）、按状态 / 模块 / 名称筛选。行内操作按状态给：运行中的能暂停，系统暂停的不能手动恢复，未结束的不能删除。
4. **新建任务**：第一屏填 `tos://pai-kit-datasets/lerobot/droid-200`，选访问密钥，停顿后自动预检，模块卡片显示可用 / 需补充 / 不支持（带原因）。交付目录访问密钥选 prod-tos，交付目录填 `tos://pai-kit-scratch/x`，失焦后做写探针：写得进但探针对象删不掉，给橙色警告；交付目录访问密钥换成 old-ci 报红。第二屏按 param_schema 生成模块参数，运动学极限要选机器人型号或跳过。「创建并开始」：droid-200 开始前指纹不一致，弹框确认后重新预检。深链：`/?dataset=tos://pai-kit-datasets/lerobot/droid_100&region=cn-beijing` 会跳到新建页并预填，每个参数都有常驻提示。
5. **任务详情**（从列表点「droid 前 50 条质检」）：报告概览六个数、分档进度、模块表（任务成败判定「2 条待补跑」可展开看是哪几条）、Token 卡片（不显示金额）、执行时间线（系统暂停与自动恢复、两个结果版本，可打开当时的报告；「so101 夜间批次」的执行裁决那次标着「改标重判：首轮完整流程」）、「更多信息」里的配置和执行计划；「日志」页签按档筛选、往上翻加载更早的。运行中的任务（如「umi_640 全量质检」）页头显示实时通道状态：SSE 连着时实时更新，断开后自动改成每 5 秒轮询。
6. **质检报告**（详情里点「查看详细报告」）：总览「输入 50 = 判废 7 + 交付 41 + 待补跑 2」、判废原因分布、数据包完整性、本次质检范围（含未运行的运动学极限）、七个模块小节（顺序同 report.json；任务成败判定与技能画像右上角「去裁决」）；小表直接嵌在小节里，大表在底部「明细表」服务端分页（每页 100 行、排序只能选白名单）；点任一 `ep N` 打开逐条下钻抽屉，「同时播放」三路视频（模拟地址放不出来，会看到自动重签两次后的失败提示）。版本下拉切到 r0001 看历史版本：黄色横幅、技能画像小节失败并给出错误、按钮都不可用。打开「so101 夜间批次」的报告：总览下面写「另有 3 条缺源文件，未参与质检」，没有重试入口；完整性一块列出 ep 212、587、901 各缺什么文件；任务列表和详情的汇总也多一项「缺源文件 3」（D40）。「性能剖析」页签（地址加 `#perf`）切全部 / 仅主流程 / 子任务。
7. **人工裁决**（报告里点「去裁决」或详情里的待裁决徽标）：一条 episode 一张卡片，卡片写着来源模块；每点一下就保存，顶部计数跟着变。ep 29 点「采纳新标注」后成败问题改成按新标注提问；点「其它原因，整条弃用」后成败按钮置灰并说明原因，再点一次撤销；「拿不准」卡片标橙、仍算待裁。「任务失败复议」页签只列任务成败判定拒掉的 5 条，折叠说明里写着另外 2 条为什么不在这里。「执行裁决」的确认框写明要应用几条裁决、其中几条改了标要重判任务成败、人已判了成功或失败的改标不重判；有改标重判时给出口径：默认与旧版一致（只跑多视角打分和逐机位复核两层），可选「按首轮的完整流程重判」，选择随请求体 `relabel_rerun` 提交（D39）。确认后「尚未应用」归零。
8. **密钥与资源**：两个页签（`#vlm` 直达 VLM 后端），列表只有非敏感信息和验证状态（失败带原因），从不回显密钥。新建访问密钥必填项有红 *，Access Key ID 以 BAD 开头会「保存但验证失败」。删除 prod-tos 会说明它正被未结束的任务使用；删除 ark-ep 先确认一次，服务端答复「被已结束的任务引用」后再确认一次才删。删掉 readonly-tos（只有已结束的任务在用）之后回到「droid 前 50 条质检」详情，会看到「访问密钥已删除」的横幅，点「重新绑定访问密钥」换一个。编辑 VLM 后端可改模型的思考强度和并行度，手填 `bad-model` 会报「最小请求没调通」且不保存。

## 契约缺口（C4 1.4.0）与前端的做法

W10 报告的缺口大多已排进契约的 1.5 待修订（`docs/contracts/SUMMARY.md` §九）。在那之前前端这样做：

| 缺口 | 前端现在怎么做 |
|---|---|
| 任务列表不能按「有待裁决」「交付过期」筛选 | 概览取最新 100 个任务在前端挑出来 |
| 任务上没记录用的是哪个预设（完整 / 快速 / 自选） | 按注册表的 `needs` 反推，只读 `needs`、不猜模块 id |
| 没有站点配置接口（HuggingFace 缓存桶是否存在、站点地域、本地输入源） | 能浏览公共数据集就当缓存桶存在；其余读可选的 `window.__CURATOR_FEATURES__` |
| `SignedUrl` 带 `from_ts` / `to_ts`，签名时并不返回；`expires_at` 的单位没写 | 片段时间一律取逐条接口 `EpisodeView.videos[].from_ts/to_ts`；`expires_at` 按毫秒处理 |
| 报告里模块的 `summary` 是自由对象、明细表是通用表 | 默认渲染器：标量成指标卡，`{name, count}` 数组成柱状图，小表嵌在小节里；v1 的专用视图（卡顿时间线、同步曲线、判决卡、两级技能表）要等契约定下数据形状 |
| `report.overview` 没有「平均质量分」（06 §6.2 提到），复议没有「关键读数」 | 不显示 |
| 某模块出错的是哪几条 episode 没有接口 | 任务详情从该档的 error 日志里找 `episode_index` |
| 裁决卡片不带视频引用 | 每张卡片滚进视口时再调一次逐条接口取视频和证据帧 |
| `listAdjudication` 的摘要写「按来源模块分组」，07 §6 定的是一条 episode 一张卡片、不分组 | 按 07 §6 |
| 裁决的 `source` 只能传一个、`status` 没有「拿不准」 | 多选来源、「拿不准」在前端对已加载的卡片再筛 |
| `DecisionInput` 没有「撤回」 | 撤销「整条弃用」记一笔「拿不准」 |
| 只有标注分歧一问的卡片，改标后顺手给的成败结论（纪律 4）没有问题可挂，卡片上看不到 | 同一次打开页面时照算；刷新后「执行裁决」确认框可能把这条算成「要重判」，以服务端执行为准 |
| `AdjudicationCounts.unapplied` 数的是裁决还是条目没写 | 确认框自己取全部未应用的卡片来数：几条裁决、几条 episode、几条改标要重判 |
