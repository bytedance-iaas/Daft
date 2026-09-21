# 随 Gradio 界面下线的 v1 测试（F1.2）

2026-09-20 删除 Gradio 界面（`ui/app.py`）与内嵌终端（`ui/terminal.py`、xterm 资产）时，
依赖它们的测试一并下线：测试函数自己或它用到的辅助函数、夹具导入了 `ui.app` / `ui.terminal`，
或者要求安装 gradio / starlette。其余测试照常运行，包括 `ui/` 里留下的
`manifest.py`、`runner.py`、`episode_detail.py`、`auth.py`、`reaper.py` 的逻辑测试（v2 移植的参考源）。

共 134 条。下表按原文件列出，「接手」是负责在新代码里重新覆盖这些行为的工作包；
测试原文在 `release_v1` 分支的 `robot-curation/curation/tests/` 下可查。

## test_backend_probe.py（3 条，接手：W8 / W10）

VLM 后端下拉的标签、探活缓存、过期选项的容错

- `test_dropdown_labels_carry_reason_and_strip_back`
- `test_probe_cache_avoids_refetch_within_window`
- `test_backend_dropdowns_tolerate_stale_values`：探活改了选项后缀后,在飞的事件可能带着旧值回来 —— 两个「模型服务」下拉必须

## test_cross_root_guard.py（3 条，接手：W4 / W10）

报告页按短名找交付时不能跨交付根认领同名交付（2026-08-31 事故）

- `test_rp_src_tracks_current_root`
- `test_load_skips_home_root_resolve_when_browsing_tos`：_load:直连浏览中 resolve_delivery(启动根候选)必须被跳过。
- `test_pick_delivery_rebinds_short_name_to_current_root`：_pick_delivery:直连语境下短名拼回当前根,绝不落启动根回捞。

## test_gradio_offline_fonts.py（1 条，接手：—）

Gradio 字体离线补丁，随 Gradio 退役

- `test_gradio_templates_have_no_google_fonts`

## test_public_catalog.py（3 条，接手：W10）

新建页的数据来源单选、HuggingFace 缓存桶选中后地址与地域置灰、公开数据集深链

- `test_source_radio_hidden_without_config_and_visible_with`
- `test_choosing_mirror_greys_root_and_region_lists_catalog_and_leaves_output_alone`
- `test_public_deeplink_preselects_and_switches_source`

## test_review_page.py（2 条，接手：W4）

/review 路由与鉴权覆盖；静态审片站下线，但「所有路由都过鉴权」要在 Daemon 上重新钉住

- `test_review_route_serves_and_auth_covers`：/review 挂上就能出 index.html;配 Basic 后同样 401→200。
- `test_no_review_dir_no_route`：不传 review_dir:/review 不存在(与终端「不传就没有」同一约定)。

## test_task_panel.py（4 条，接手：W10）

任务面板的轮询节奏与展示

- `test_stop_button_starts_disabled_and_rides_every_status_refresh`：停止按钮默认不可点,且每条刷新状态区的路都带着它(与 #55 的「开始质检」同一条不变量)。
- `test_stop_button_follows_busy_state`
- `test_refresh_is_driven_by_page_script_not_gr_timer`：自动刷新 = 页面脚本点「刷新」按钮(真事件),不再有 gr.Timer 对着任务面板。
- `test_refresh_does_not_flash_loading_state`：轮询刷新不许带 Gradio 的加载态(2026-08-22 用户实见"一秒一闪"):每 2 秒把日志框 /

## test_tos_probe.py（3 条，接手：W8 / W10）

只读存储桶的提示与横幅、交付目录写探针的界面反馈

- `test_readonly_lock_messages`
- `test_no_bucket_instance_leaves_output_boxes_empty_and_has_modal`：同事的纯直连部署(没挂载、没 TOS_BUCKET):两页的「交付目录」默认留空等借桶,
- `test_run_page_region_red_notes_replace_fill_time_dialogs`：2026-08-28 用户定版:填表阶段桶/地区问题一律红字贴在地区下拉正下方,

## test_ui_datasources.py（9 条，接手：W10）

深链参数在界面上的行为（设计 07 篇要求 v1 的深链测试全部搬到新前端）

- `test_single_source_still_shows_source_dropdown`：★单桶部署:跑质检侧是「数据集目录」文本框(2026-08-20 融合改版,
- `test_multi_source_shows_dropdown_and_wires_it`：配了多个 TOS 桶(2026-08-20 融合改版):路径框默认预填第一桶的
- `test_deeplink_switches_source_dropdown_to_matching_bucket`：★深链(tos:// 完整地址)要同时落到**三处**:路径框切到根前缀、数据集
- `test_single_source_dataset_choices_match_old_data_root`：没配 tos_buckets 的老部署:数据集下拉的候选仍来自 --data-root(逐项一致)
- `test_dropdown_value_is_stable_id_and_label_never_a_key`：★两条硬约束一起钉:①下拉 choices 是 (显示文本, 内部标识) 成对,value 仍是
- `test_dataset_pickers_carry_no_info_and_notes_moved_below`：★防回退:两级下拉(数据集根目录/数据集/原始数据集)**不再带 info=**。
- `test_unmounted_root_is_marked_in_choices_and_does_not_crash_ui`：★未挂载的根:下拉项标「⚠️ 未挂载」,但 UI 照常建起来(别的桶可能是好
- `test_switching_root_clears_stale_notes_instead_of_leaving_them`：★切根目录时,两列说明"该变什么"就必须真的变过去 —— 不许残留上一个根的话。
- `test_info_line_endpoint_always_instance_config_not_link`：★说明行的「端点:」永远印**本实例配置**的值,不被链接里的端点顶掉。

## test_ui_manifest.py（64 条，接手：W4 / W10）

报告页、裁决页、任务台的界面组装与交互

- `test_app_builds_without_server`：Gradio 冒烟:四 tab 构造成功即可(不 launch)。无 gradio 环境自动跳过。
- `test_app_with_terminal_tab`：terminal=True:有「终端」页签 + xterm 容器 div,且它排在**最右**。
- `test_app_without_terminal_leaves_no_terminal_trace`：terminal=False:配置里连「终端」二字都没有,报告页那套子页签照旧。
- `test_task_console_is_top_level_and_report_tabs_untouched`：「跑质检」与质检报告并列在顶层;报告页那套子页签一个不少、顺序不变。
- `test_backend_probe_is_automatic`：2026-08-21 用户:可用性该系统自己查。没有「检测可用性」按钮;探活挂在
- `test_console_knobs_to_set_overrides`：面板上的旋钮 → --set 列表:**只发用户动过的**,留空一律不发。
- `test_concurrency_defaults_are_shown_as_placeholders_not_prefilled`：并发框把生效配置里的默认值说给用户听,但只放**占位符**。
- `test_concurrency_boxes_carry_the_default_in_the_placeholder`：界面这一侧钉死同一件事:占位符里带出厂默认值(32/16/32),value 仍是空。
- `test_probe_rescan_picks_up_a_backend_added_after_startup`：探活时重读配置:启动之后才加进站点 YAML 的服务也要出现在下拉里,
- `test_probe_rescan_clears_a_selection_that_left_the_config`：选中的预设从配置里删掉了 → 回到未选状态并说清楚,不留一个指向不存在服务的
- `test_probe_button_rereads_the_config_without_restarting_the_ui`：客户在站点 YAML 里新加一台自托管服务,不该为了看见它去重启 UI ——
- `test_vlm_involved_decides_concurrency_greying`：并发旋钮只在真调 VLM 时可用;自选模块要按两种语义分别算。
- `test_kin_will_run_decides_embodiment_ask`：型号追问只在这次真跑运动学极限时弹(2026-09-16 用户定):完整/快速都跑;
- `test_terminal_off_registers_no_routes`：不开终端:/ws/term 与静态资产整个不存在(404),与「不传就没有」的老行为对齐。
- `test_terminal_on_registers_routes_and_assets`：开终端:/ws/term 路由在,vendored 的 xterm.js/css/addon-fit/term.js 都取得到。
- `test_basic_auth_401_then_200`：配了 CURATION_UI_USER + PASSWORD:全路由 401,凭证对了才 200;/healthz 永远豁免。
- `test_basic_auth_covers_websocket`：鉴权必须**盖住 WS**(/ws/term 是真 shell;BaseHTTPMiddleware 会漏掉它,所以写的裸 ASGI)。
- `test_basic_auth_not_enabled_when_half_configured`：只配用户名不配密码 = 不启用(半配的鉴权最坏:自以为锁了其实没锁)。
- `test_htpasswd_auth_multiuser`：htpasswd 模式:同一份账号表多用户皆可登录,错密码/陌生用户 401,/healthz 豁免。
- `test_htpasswd_bcrypt_hash`：bcrypt 哈希($2y$,htpasswd -B 的缺省)也认 —— 生产账号表就是这种。
- `test_htpasswd_wins_over_single_user_env`：两种模式都配时 htpasswd 优先:旧的单用户凭证不再放行(一套账号表说了算)。
- `test_htpasswd_fail_closed_on_bad_file`：配了 htpasswd 但文件缺失/无可用行 = 锁死全部请求(探针除外),绝不静默裸奔。
- `test_ws_term_pty_roundtrip`：真握手 + 真 PTY:发一条 `echo <token>` 的 input 帧,读回 PTY 吐出来的 token。
- `test_ws_term_closes_when_shell_exits`：shell 自己 `exit` 之后服务端要主动关连接(不关 = 前端一直转,PTY fd 也漏着)。
- `test_app_has_perf_tab`：Gradio 层:「性能剖析」在,且报告页那几个页签一个不少。
- `test_app_skill_chart_wired_into_tab`：Gradio 层:技能画像页多了一块 HTML,且 _load 的返回数与输出组件数对得上。
- `test_app_has_manual_decision_tab`：Gradio 层:新增「人工裁决」页签,排在「轨迹」与 技能画像 之间;
- `test_asgi_app_serves_manual_decision_tab`：整页起得来,且「人工裁决」的文案真出现在首页 HTML 里。
- `test_app_has_reject_appeal_section`：Gradio 层:「任务失败复议」子页签在位,两个按钮文案在位。
- `test_load_callback_wiring_stays_aligned`：`_load` 的返回值个数必须与它的输出槽位个数逐一对齐。
- `test_app_has_sync_curve_tab_and_split_evidence`：Gradio 层:「同步曲线」页在(挨着 Stuck 时间线),曲线走独立的整幅宽度组件。
- `test_asgi_app_serves_sync_curve_tab`：整页起得来,「同步曲线」页签文案真出现在首页 HTML 里。
- `test_app_load_returns_match_outputs_after_rework`：接线闸门:_load 的返回数 = outputs 组件数(错位是运行期才炸的接线错误)。
- `test_app_episodes_tab_is_buckets_plus_list_and_detail`：Gradio 层:三桶单选 + 左清单 + 折叠的检查明细都在,证据帧画廊已撤。
- `test_detail_subtabs_order_and_the_new_video_page`：明细页五个子页的顺序钉死,新的「视频打分明细」排在「动作打分明细」之后。
- `test_perf_tab_is_top_level_right_of_detail`：「性能剖析」必须是**顶层页签**,且排在「明细」右边(2026-08-13 用户点名)。
- `test_adjudication_cards_can_play_every_camera_at_once`：「待你裁决」的合并裁决卡有「同时播放」按钮,且视频照旧独占一行。
- `test_delivery_picker_is_actually_ticked`：交付下拉的定时补扫**必须真的接了事件**。
- `test_execute_confirm_targets_loaded_run_not_dropdowns`：★「执行裁决」作用的是**当前报告页加载的那份交付与运行**,不是下拉的值。
- `test_execute_button_only_asks_and_never_starts`：★确认块不点「确定」绝不发起任务(防"按钮即执行")。
- `test_execute_refuses_and_says_so_while_a_task_is_running`：★有任务在跑时点执行:**明说**在等谁,不发起、不失败也不排队。
- `test_report_page_tab_set_is_frozen`：★红线:报告页现有页签的增删必须为零。
- `test_dropdown_arrow_click_is_delegated_to_the_input`：点下拉右边的箭头必须能展开列表(issue #53,同事 2026-08-19 报)。
- `test_confirm_box_is_a_real_modal_not_an_inline_panel`：🔴 「人工裁决结果会改写交付」那个确认框必须是**跳出来的对话框**,
- `test_review_queue_is_one_table_with_five_columns`：待裁决队列是**一张表**(2026-08-23 用户拍板,取代 8-19 的两表并列):
- `test_dataset_dropdown_is_multiselect`：「数据集」下拉必须是多选 —— 一次点击顺序跑几个就靠它。
- `test_delivery_name_hint_switches_when_several_datasets_are_picked`：选多个时「交付名」的说明要换成"父文件夹"那句。
- `test_dropdown_overlay_follows_the_page_scroll`：选项浮层要跟手:纯 JS 进不了 pytest,故按字符串钉住那几处要害。
- `test_dropdown_overlay_script_is_actually_injected`：脚本得真进 head —— 常量写好了却没接进 presentation(),界面上一点看不出来。
- `test_favicon_link_injected_with_root_prefix`：icon 的 <link> 必须自己注入且带挂载前缀 —— gradio 只服务 {root}/favicon.ico
- `test_force_light_theme_script_injected`：暗色系统下整页没法读(issue #114):Arco 样式全按浅色写死,gradio 却会跟随
- `test_report_overview_tab_is_one_table_without_the_funnel_word`：总览页:表不再顶「漏斗」这个标题,整份界面配置里也不剩这两个字。
- `test_effective_config_tab_is_gone`：「本次运行配置」页签已整个移除(2026-08-30 用户拍板)。
- `test_load_feeds_the_overview_table`：_load 的返回值与 outs 一一对齐,且总览那一格发的是新表、不是老的漏斗表。
- `test_footer_links_are_off`：页脚那排 Use via API / Built with Gradio / Settings 整排去掉。
- `test_start_button_refuses_an_empty_dataset_selection`：数据集多选默认空:点「开始质检」要给一句明确提示,不静默、不抛红框。
- `test_terminal_screen_clips_its_overflow`：终端容器必须 overflow:hidden —— 2026-08-13 用户截图里面板底部那条深色带,
- `test_polling_does_not_wipe_the_validation_message`：两秒一次的轮询必须把当前那句提示**带回去**,不是清空。
- `test_start_button_state_rides_every_status_refresh`：issue #55:「开始质检」有任务在跑时置灰,任务结束自动回蓝。
- `test_execute_button_state_refreshes_on_returning_to_report_page`：issue #55 同族:「执行裁决」的可点性在回到报告页时重算。
- `test_execute_with_retry_checkbox_passes_flag`：复盘 ⑩ 接线:勾「同时补判弃权条目」→ rejudge 命令带 --retry-abstained;
- `test_terminal_workdir_falls_back_mount_then_cache`：终端落脚目录(2026-08-28 去挂载依赖):环境变量点名 > 挂载根 >
- `test_adjudication_buttons_queue_clicks_and_lock_while_pending`：裁决/翻页按钮的守护点击(2026-09-04 立案修复):gradio .click 默认 trigger_mode
- `test_cli_parses_terminal_flag`：`ui --terminal` 是布尔开关;env CURATION_TERMINAL 提供缺省值。

## test_ui_perf.py（38 条，接手：W4 / W10）

性能剖析页签的界面组装

- `test_html_and_config_are_gzipped`
- `test_assets_get_immutable_cache_header`
- `test_root_path_prefixes_gradio_and_file_urls`：挂载前缀部署(dataverse /curation,网关不剥前缀;2026-08-31 裁决卡视频 404 案):
- `test_sse_stream_is_excluded_from_gzip`：事件流绝不能被 gzip 缓冲:压缩缓冲会把 app.load 的结果憋到流结束。
- `test_build_app_bootstraps_an_empty_delivery_root`：交付根一份交付都没有 → 自动放占位交付,UI 照常起来;占位走 safe_write
- `test_build_app_fails_loudly_when_root_unwritable`：占位都放不进去 = 交付根不可写的部署问题,必须响亮失败、话说清。
- `test_unmounted_instance_defaults_and_autolist_wiring`：同事的部署形态整体过一遍:交付根在本地盘、数据集根不存在、TOS_BUCKET 有。
- `test_mounted_instance_keeps_old_defaults_and_no_autolist`：我们自己的形态(挂载承载)逐字节不变:默认值同前、不挂自动列表。
- `test_report_root_default_lists_bucket_when_not_mounted`：没挂载的实例:报告页交付根默认地址 = 桶里前缀,开门/回车要**真去桶里列**,
- `test_picker_tick_relists_bucket_in_direct_mode`：切到报告页签的补扫在直连模式下要去桶里重列,不许扫本地把桶清单盖回去
- `test_follow_latest_canonicalizes_mount_output_to_tos`：跑批落在挂载根 → 报告页跟随时**连根带区**切成它的 tos:// 身份。
- `test_run_form_output_defaults_to_last_run`：交付目录代填的「自己的桶」分支吃记忆默认:最近一次跑批写到哪就填哪,
- `test_radio_groups_are_one_frame_not_per_option_pills`：issue #54 / #59-1:单选(多选)组按 Arco——选项不各自成框,整组一个框。
- `test_scan_radio_tips_cover_full_and_quick`：「完整质检」「快速质检」「指定 episode」都带悬停问号;文案口径统一到
- `test_report_tab_reloads_when_data_changed_on_disk`：切回报告页要能发现盘上数据变了(2026-08-25 复盘 ②):存在挂在事件上的
- `test_report_picker_defaults_to_most_recent`：⑥ 接线:build_app 后 picker 的初值 = 最近跑批的交付(不是 choices[0])。
- `test_queue_tables_tall_enough_to_avoid_scroll_hijack`：复盘 ⑨:两张裁决队列表 max_height ≥ 980 —— 420 时鼠标悬在表上滚轮只滚
- `test_backend_dropdown_announces_probing_then_probe_clears_it`：复盘 ⑤:模型服务下拉首屏 info=「正在检测…」(探活要出网 1-4s,空框不说话
- `test_adjudication_queue_title_and_status_radio`：人工裁决队列(2026-08-25 用户改名)+ 两组筛选(问题类型 × 状态)——
- `test_rerun_shape_dead_path_default_left_empty`：rerun 侧的部署形态(2026-08-26 实测):helm 传了 --data-root /data/datasets
- `test_rp_root_refresh_keeps_valid_selection`：交付下拉刷新保值(2026-08-27 用户实报:点一下交付目录框再点走,
- `test_preflight_pops_embodiment_ask_for_unknown_robot`：robot_type 没登记且表单没填型号 → 开跑前弹「机器人型号」模态,
- `test_preflight_asks_embodiment_only_when_kinematics_selected`：UI 开跑前的「机器人型号」追问只在勾了运动学极限时弹(2026-09-16 用户定):
- `test_preflight_bad_name_opens_gate_dialog_not_small_text`：交付名非法/为空 → 开跑闸模态,不是小红字(2026-08-28 用户 manager 实见:
- `test_modal_centers_without_transform_so_dropdowns_anchor`：型号追问框里的下拉浮层飞到页面左上/塌高(2026-08-29 用户实拍+真机
- `test_emb_ask_puts_suggestion_first`：型号追问的候选把建议排最前(2026-08-29 用户实拍:全量字母序看着像
- `test_preflight_bad_episode_selection_opens_gate_dialog`：episode 选择守门走开跑闸模态(issue #110 洞⑧+洞①前哨):N=0、表达式
- `test_history_renamed_and_report_jump`：issue #102 定案(2026-08-30):历史不搬家 ——「历史」改名「执行历史」
- `test_toasts_are_tamed`：issue #107(用户吐槽:toast 太吓人+倒计时几秒就没):
- `test_preflight_empty_root_and_output_open_gate_dialog`：数据集目录/交付目录没填 → 同一扇开跑闸模态(2026-08-29 用户:任务区
- `test_module_pick_change_does_not_rerender_itself`：自选模块里每勾一个,勾选框闪一下(2026-08-27 用户实见):勾选自身的
- `test_mirror_cache_files_are_servable`：直连交付的懒镜像缓存必须在 gradio 的文件放行名单里(2026-08-28 用户实见:
- `test_empty_adjudication_deck_collapses_card_block`：裁决队列为空时整个卡片区收起(2026-08-28 用户点名:机位框/翻页按钮空摆着
- `test_max_episodes_field_is_blank_textbox`：「只跑前 N 条(留空=全部)」必须真空白:gr.Number 的 None 会被 gradio 6.9
- `test_out_changed_mismatch_is_red_note_not_dialog`：交付目录探测:桶/地区问题 = 地区下拉正下方红字;填表阶段不弹窗;
- `test_preflight_gate_opens_dialog_and_never_clears_path`：开跑硬闸:交付目录用不了 → 弹 out-ask 模态说原因;响应里除模态外
- `test_root_changed_mismatch_red_note_and_keeps_input`：数据集目录:桶/地区问题红字化;目录框与地区下拉都不被改写。
- `test_rp_root_changed_mismatch_red_note`：报告页交付目录:同一套红字;交付下拉不动、说明区不再放桶/地区报错。

## test_ui_runner.py（1 条，接手：W5）

任务台跑批的界面入口

- `test_app_blocks_legacy_delivery_name_before_starting_a_task`：app 层:用老布局交付名点「开始质检」,任务根本不该被起起来 —— 任务目录

## test_ui_terminal.py（3 条，接手：—）

内嵌网页终端，按需求删除

- `test_rcfile_sets_colored_prompt_and_keeps_user_rc`
- `test_rcfile_survives_deletion`
- `test_bash_accepts_the_rcfile`
