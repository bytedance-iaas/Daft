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

  preset: {
    full: '完整质检',
    quick: '快速质检',
    custom: '自选',
    quickLong: '快速质检（不调用模型的模块）',
  },

  taskList: {
    title: '质检任务',
    desc: '对机器人演示数据集跑自动质检，产出可直接训练的交付数据集与质检报告。',
    newTask: '新建任务',
    searchPlaceholder: '搜索任务名称或 ID',
    allStates: '全部状态',
    modulesFilter: '包含模块',
    modulesFilterPlaceholder: '按质检模块筛选（多选）',
    datasetFilter: (name: string) => `数据集：${name}`,
    clearDatasetFilter: '清除数据集筛选',
    autoRefresh: '有未结束的任务，每 5 秒自动刷新',
    colName: '任务名称',
    colState: '状态',
    colDataset: '数据集',
    colModules: '质检模块',
    colProgress: '进度 / 结果',
    colTokens: 'Token 消耗',
    colCreated: '创建时间',
    colActions: '操作',
    pendingBadge: (n: number) => `待裁决 ${n}`,
    deliveryStale: '交付待重新导出',
    deliveryNeverExported: '交付待导出',
    moduleCount: (n: number) => `${n} 项`,
    moduleErrors: (n: number) => `${n} 项错误`,
    moduleNotRun: (n: number) => `${n} 项未运行`,
    modulePopoverTitle: '本任务的质检模块',
    modulePopoverLoading: '正在取各模块状态…',
    created: '还没启动，配置都能改',
    queued: '排队中，等前面的任务结束',
    pausedAt: (stage: string, done: number, total: number) => `停在${stage} ${done} / ${total}`,
    resumeHint: '恢复后从断点继续',
    systemResumeHint: 'Daemon 重启后自动续跑',
    running: (stage: string) => `${stage}`,
    etaShort: (text: string) => `剩余约 ${text}`,
    resultLine: (p: number, r: number, h: number) => (h ? `通过 ${p} · 拒绝 ${r} · 待补跑 ${h}` : `通过 ${p} · 拒绝 ${r}`),
    passRate: (text: string) => `通过率 ${text}`,
    stoppedAt: (n: number, total: number) => `停下时完成 ${n} / ${total}`,
    empty: '还没有任务',
    emptyFiltered: '没有符合筛选条件的任务',
    deletedAt: '删除于',
    retryCount: (n: number) => (n ? `重试（${n} 条）` : '重试'),
  },

  actions: {
    start: '启动',
    edit: '编辑',
    copy: '复制为新任务',
    delete: '删除',
    restore: '恢复',
    pause: '暂停',
    resume: '恢复',
    stop: '停止',
    cancelQueue: '取消排队',
    retry: '重试',
    continue: '继续运行',
    report: '查看报告',
    view: '查看',
    adjudicate: '人工裁决',
    export: '导出',
    reexport: '重新导出',
    purge: '清理交付产物',
    systemPausedResume: '系统暂停的任务会自动恢复，不需要手动恢复',
    deleteDisabled: '任务还没结束，不能删除；先停止它',
    done: {
      start: '已启动，进入队列',
      pause: '已请求暂停：在飞的 episode 跑完后停下',
      resume: '已恢复，重新排队',
      stop: '已停止',
      retry: '已创建重试子任务，只补跑出错的条目',
      continue: '已创建继续运行的子任务，从断点接着跑',
      reexport: '已创建导出子任务，完成后逐文件核验',
      delete: '已删除，30 天内可以在「已删除」筛选里恢复',
      restore: '已恢复',
      purge: (path: string, size: string) => `已开始清理 ${path}（${size}）`,
      apply: '已创建执行裁决的子任务',
    },
    confirmStop: {
      title: '停止任务',
      content: '停止后不能恢复为运行中；之后可以「继续运行」，从断点接着跑。',
    },
    confirmDelete: {
      title: '删除任务',
      content: (name: string) => `删除「${name}」？只删平台里的任务记录，TOS 上的交付产物不动。30 天内可以在「已删除」筛选里恢复，之后连同裁决记录、Token 统计一起清除。要删 TOS 上的产物，用「清理交付产物」。`,
    },
    confirmRetry: {
      title: '重试出错的条目',
      content: (n: number) =>
        `建一个重试子任务，只补跑出错的 ${n} 条（从出错的那一档接着往后跑），已经成功的结果原样保留。新结果完整生成后才替换当前版本；重试失败或被停止，页面上仍是当前版本。开始前会检查：数据集能读、交付目录能写、模型能调通。`,
      contentModules: (names: string) =>
        `建一个重试子任务，只补跑 ${names} 出错的条目；模块整体失败时该模块全量重跑。已经成功的结果原样保留，新结果完整生成后才替换当前版本。开始前会检查：数据集能读、交付目录能写、模型能调通。`,
      ok: '开始重试',
    },
    confirmContinue: {
      title: '继续运行',
      content: '从断点接着跑主流程里没完成的部分，已完成的模块和 episode 不会重跑。开始前会检查：数据集能读、交付目录能写、模型能调通。',
      ok: '继续运行',
    },
    confirmExport: {
      titleFirst: '导出交付数据集',
      titleAgain: '重新导出交付数据集',
      content: (uri: string) =>
        `按当前通过名单导出（含待裁决条目，不含待补跑条目），写到 ${uri}。导出走增量，只处理变动的 episode；导出后逐文件回读核验，通过才标记完成。以后人工裁决改了判决，还要再导出一次，平台不会自动重建数据集。`,
      ok: '开始导出',
    },
    purgeDialog: {
      title: '清理交付产物',
      intro: '会删掉这个任务在 TOS 上的批次目录，删了不能恢复：',
      scope: '同一交付目录下别的任务的批次不动；latest 如果指向这个批次，会一并移除。平台里的任务记录保留。',
      confirmLabel: (name: string) => `输入任务名称「${name}」确认`,
      mismatch: '和任务名称不一致',
      ok: '清理',
      noRun: '这个任务还没有写过交付产物，没有可清理的批次目录。',
    },
    precheckFailed: '开始前检查没过，任务没有开始',
    precheck: {
      input: '数据集能读',
      output: '交付目录能写',
      vlm: '模型能调通',
    } as Record<string, string>,
  },

  fingerprint: {
    title: '数据集和添加时不一样了',
    intro: '开始前核对了数据集的指纹，和添加时（或最近一次预检时）记下的不一致。确认后会重新预检：结果和任务配置相容就接着开始，不相容就回到编辑页，标出要改的地方。',
    meta: (changed: boolean) => (changed ? 'meta 文件有变化' : 'meta 文件没变'),
    files: (a: number, r: number, m: number) => `文件：新增 ${a} 个 · 删除 ${r} 个 · 改动 ${m} 个`,
    samples: '受影响的文件（节选）',
    lastPreflight: '上次预检',
    confirm: '重新预检',
    running: '正在重新预检…',
    compatible: '重新预检通过，任务已开始',
    incompatible: '重新预检的结果和任务配置不相容，请按标出的地方修改后再开始',
  },

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
