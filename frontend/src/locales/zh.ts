// All user-facing copy lives here (doc 07 §9): one file to review against the humanizer-zh rules
// before a release. Terminology follows doc 07 §8 (地域、访问密钥、存储桶、推理接入点、思考强度、
// HuggingFace 缓存桶、恢复为可用). Module names never live here: they come from GET /modules.

const join = (xs: readonly string[]) => xs.join('、');

export const zh = {
  app: {
    product: 'Physical AI Kit · 数据质检平台',
    vendor: '火山引擎',
    docTitle: (page: string) => `${page} · 数据质检平台`,
  },

  nav: {
    groupMain: '数据质检',
    overview: '概览',
    datasets: '数据集',
    tasks: '质检任务',
    credentials: '密钥与资源',
    breadcrumbRoot: '数据质检平台',
  },

  common: {
    ok: '确定',
    cancel: '取消',
    save: '保存',
    saving: '保存中…',
    delete: '删除',
    edit: '编辑',
    refresh: '刷新',
    retry: '重试',
    back: '返回',
    view: '查看',
    more: '更多',
    close: '关闭',
    confirm: '确认',
    loading: '加载中…',
    loadMore: '加载更多',
    noData: '暂无数据',
    optional: '选填',
    none: '—',
    unknown: '未知',
    unlimited: '不限',
    all: '全部',
    search: '搜索',
    copy: '复制',
    copied: '已复制',
    total: (n: number) => `共 ${n} 条`,
    items: (n: number) => `${n} 条`,
    episodes: (n: number) => `${n} 条 episode`,
    pageSizeOption: (n: number) => `${n} 条/页`,
    manage: '管理',
    requiredMark: '必填',
    yes: '是',
    no: '否',
    seconds: (s: number) => `${s} 秒`,
  },

  errors: {
    network: '连不上服务，请检查网络后重试',
    unexpected: (status: number) => `服务返回了意外的响应（HTTP ${status}），请稍后重试`,
    unknown: '出了点问题，请稍后重试',
    pageTitle: '页面数据没能取回',
    notFound: '要找的内容不存在，可能已被删除',
    backHome: '回到概览',
    required: (label: string) => `请填写${label}`,
    requiredSelect: (label: string) => `请选择${label}`,
    routeNotFound: '这个页面不存在',
  },

  time: {
    justNow: '刚刚',
    duration: (s: number | null | undefined) => {
      if (s === null || s === undefined || Number.isNaN(s)) return '—';
      const t = Math.max(0, Math.round(s));
      const h = Math.floor(t / 3600);
      const m = Math.floor((t % 3600) / 60);
      const sec = t % 60;
      if (h) return `${h} 小时 ${m} 分`;
      if (m) return `${m} 分 ${String(sec).padStart(2, '0')} 秒`;
      return `${sec} 秒`;
    },
    eta: (s: number) => `预计还要 ${zh.time.duration(s)}`,
  },

  /** Task states (doc 07 §8). completed_with_errors is 「错误」 since 2026-09-21. */
  state: {
    created: '待启动',
    queued: '排队中',
    running: '运行中',
    pausing: '暂停中',
    paused: '已暂停',
    stopping: '停止中',
    stopped: '已停止',
    succeeded: '已完成',
    completed_with_errors: '错误',
    failed: '失败',
    deleted: '已删除（30 天内可恢复）',
    systemPauseTip: '系统将自动恢复',
  } as const,

  stage: {
    autolabel: '补任务描述',
    numeric: '数值档',
    frame: '帧档',
    vlm: 'VLM 档',
    verdict: '判决',
    dedup: '去重',
    profile: '技能画像',
    final: '终判',
    export: '导出',
    report: '报告',
    verify: '交付核验',
    post_verdict: '判决之后',
  } as Record<string, string>,

  stageState: {
    pending: '等待中',
    running: '进行中',
    succeeded: '已完成',
    completed_with_errors: '有出错条目',
    failed: '失败',
    skipped: '跳过',
  } as Record<string, string>,

  moduleState: {
    pending: '等待中',
    running: '运行中',
    succeeded: '已完成',
    completed_with_errors: '错误',
    failed: '失败',
    skipped: '未运行',
    stale: '待同步',
  } as Record<string, string>,

  gate: {
    hard: '一票否决',
    soft: '打分项',
    dedup: '剔除重复',
    none: '只出画像',
  } as Record<string, string>,

  source: {
    tos: '私有 TOS',
    public: 'HuggingFace 缓存桶',
    local: '本地挂载路径',
    experimental: 'experimental',
  } as Record<string, string>,

  format: {
    lerobot_v2: 'LeRobot v2',
    lerobot_v3: 'LeRobot v3',
    unsupported: '不支持',
  } as Record<string, string>,

  verify: {
    unverified: '未验证',
    ok: '已验证',
    failed: '验证失败',
  } as Record<string, string>,

  checkState: {
    ok: '一致',
    changed: '有变化，待重新预检',
    unchecked: '未检查',
  },

  /** Preflight reason_code → Chinese (C2 preflight). Unknown codes fall back to `reason`. */
  reason: {
    format_unsupported: (a: { detected?: unknown }) => `当前版本仅支持 LeRobot v2/v3，检测到 ${String(a.detected ?? '未知格式')}`,
    metadata_invalid: (a: { problem?: unknown }) => `数据集的 meta/info.json 有问题：${String(a.problem ?? '')}`,
    missing_input: (a: { missing?: unknown; video_cause?: unknown }) => {
      const names: Record<string, string> = {
        timestamps: '时间戳列',
        action: 'action 列',
        state: 'observation.state 列',
        video: '视频',
      };
      const missing = Array.isArray(a.missing) ? a.missing.map((m) => names[String(m)] ?? String(m)) : [];
      const cause =
        a.video_cause === 'none_declared'
          ? '（info.json 没有声明相机）'
          : a.video_cause === 'files_missing'
            ? '（声明了相机，但找不到对应的视频文件）'
            : '';
      return `数据集缺少${missing.length ? join(missing) : '所需数据'}${cause}`;
    },
    embodiment_unsupported: (a: { subject?: unknown; given_by?: unknown; supported?: unknown }) => {
      const list = Array.isArray(a.supported) ? join(a.supported.map(String)) : '';
      const who = a.given_by === 'embodiment_id' ? '所选的机器人型号' : '机器人型号';
      return `${who} ${String(a.subject ?? '')} 不在规格库${list ? `（已支持：${list}）` : ''}，该模块整项跳过，其余模块照常`;
    },
    robot_type_unknown: (a: { robot_type?: unknown }) =>
      a.robot_type
        ? `info.json 里的机器人型号 ${String(a.robot_type)} 认不出，请补充型号，或跳过该模块`
        : '未读到机器人型号（info.json 里没有 robot_type），请补充型号，或跳过该模块',
    vlm_backend_missing: () => '还没选 VLM 后端，在「模型配置」里选一个',
  } as Record<string, (args: Record<string, unknown>) => string>,

  /** Deep-link notes (doc 07 §2.1): persistent red text under the field concerned. */
  deeplink: {
    onlyTos: (s: string) => `只认识 tos:// 开头的数据集链接，这个解析不了：${s}`,
    unparsable: (s: string) => `数据集链接解析不了：${s}`,
    noBucket: (s: string) => `链接里没有存储桶名，解析不了：${s}`,
    bucketOnly: (s: string) => `链接只有存储桶名、没有数据集段，不知道要选哪个数据集：${s}`,
    badName: (problem: string, s: string) => `链接里的数据集名不合法（${problem}），这个链接不处理：${s}`,
    notTosUrl: (s: string) => `不是 tos:// 地址：${JSON.stringify(s)}（写法：tos://存储桶名/前缀）`,
    badBucket: (b: string) => `存储桶名不合法：${JSON.stringify(b)}（3–63 位小写字母、数字、中划线）`,
    dotDot: (p: string) => `前缀里不允许 '..'：${JSON.stringify(p)}`,
    backslash: (p: string) => `前缀里不允许反斜杠：${JSON.stringify(p)}`,
    publicReadonly: '公共数据集只读，交付目录请填你有写权限的 tos://存储桶名/目录',
    publicMissing: '本站没有配置 HuggingFace 缓存桶，链接里的 source=public 用不了',
    sourceUnknown: '链接里的 source 参数不认识，已忽略（只认 public）',
    regionBad: '链接里的地域参数写法不对（形如 cn-beijing），已忽略',
    endpointBad: '链接里的端点参数看不懂，已忽略（不影响预选）',
    mixedIgnored: (names: string[]) => `链接里同时有完整地址和裸名字，只按完整地址预填；已忽略：${names.join(', ')}`,
    emptyValue: '链接带了数据集参数但没有可用的值，没有预选',
    publicNotFound: (n: string) => `链接指的数据集「${n}」在 HuggingFace 缓存桶里没找到 —— 名字可能变了`,
    addressUnparsable: (msg: string) => `链接里的数据集地址解析不了：${msg}`,
    addressIgnored: (u: string) => `链接里的地址解析不了，已忽略：${u}`,
    otherPrefixIgnored: (root: string, u: string) => `链接带了多个不同前缀，只认第一个（${root}）；已忽略 ${u}`,
    rootOnly: (root: string) => `链接只给到 ${root}，没有指到具体的数据集，请在地址后补上数据集名`,
    registeredTwice: (n: string) => `「${n}」在「数据集」里登记过不止一次（地域不同），没有自动带出访问密钥，请核对地域`,
    ambiguousName: (name: string, k: number) =>
      `链接里的数据集「${name}」在「数据集」里有 ${k} 个同名的（分属不同存储桶或前缀），没有预选 —— 请手动选择`,
    notFound: (names: string[]) =>
      `链接里的数据集在本站找不到：${names.join(', ')}（「数据集」里没有登记同名的数据集，可以直接填完整的 tos:// 地址）`,
    spansRoots: (roots: string[]) =>
      `链接里的数据集分属不同存储桶或前缀（${roots.join('、')}），一次只能预选一处 —— 这次没有预选，请手动选择`,
    endpointConflict: (host: string, linkRegion: string, ours: string) =>
      `链接给的端点（${host}，地域 ${linkRegion}）与这里的地域 ${ours} 不一致 —— 存储桶名全局唯一，同名存储桶不可能同时在两个地域，其中必有一处有误。预选不受影响，请核对地域`,
    endpointSuffix: (host: string) => `；链接标注的端点是 ${host}`,
    borrowFailed: (why: string) => `数据集所在的存储桶不能当交付目录：${why}。请另填一个可写的 tos://存储桶名/目录`,
    batchMode: (n: number) => `链接带了 ${n} 个数据集：共用下面这一套配置，一次创建 ${n} 个任务，交付目录各自为「交付根目录/数据集名-月日」`,
  },
};

export type Zh = typeof zh;
