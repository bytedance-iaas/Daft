# 裁决/报告跨交付根串扰的回归钉子(2026-08-31 事故)。
#
# 事故:报告页直连浏览某个 tos:// 交付时,「短名回捞」(resolve_delivery 按
# 目录名在**启动参数的默认交付根**里认领)把默认根(挂载盘)下的同名老交付
# 连同它的裁决 CSV 端了上来——横幅
# 凭空多出「已裁 3 条」,五层排查(桶/缓存树/进程/会话)都追不到,因为数据
# 根本来自另一个交付根。名字相同不代表是同一份交付。
#
# 修法:_rp_src 记下当前浏览根;直连语境(root 是 tos://)下,_load 跳过
# resolve_delivery 的启动根回捞(读不到就 load_error 明说),_pick_delivery
# 把短名拼回**当前根**重走 tos 分支。
# UI 闭包不可直测,这里钉源码结构 —— 删守卫必红,搬家请连测试一起搬。
import inspect
import os
import re


def _app_source() -> str:
    import curation.ui.app as app_mod
    return open(inspect.getsourcefile(app_mod), encoding="utf-8").read()


def test_rp_src_tracks_current_root():
    src = _app_source()
    assert '_rp_src = {"region": "", "root": ""}' in src, \
        "_rp_src 必须携带当前浏览根(root),短名回捞的守卫靠它分辨语境"
    # 三条设置路径:回本地清空、直连列出、直连空前缀
    assert src.count('_rp_src["root"] = ""') >= 1
    assert src.count('_rp_src["root"] = s') >= 2


def test_load_skips_home_root_resolve_when_browsing_tos():
    """_load:直连浏览中 resolve_delivery(启动根候选)必须被跳过。"""
    src = _app_source()
    guard = re.search(
        r'if \(_rp_src\.get\("root"\) or ""\)\.startswith\("tos://"\):\s*\n'
        r'\s*m = load_delivery\(path\)\s*\n'
        r'\s*else:\s*\n'
        r'\s*m = load_delivery\(resolve_delivery\(path, discover_deliveries\(delivery\)\)\)',
        src)
    assert guard, ("_load 丢了跨根守卫:直连浏览中短名会被启动根按名认领,"
                   "把挂载根同名交付的报告与裁决台账端给用户(2026-08-31 事故)")


def test_pick_delivery_rebinds_short_name_to_current_root():
    """_pick_delivery:直连语境下短名拼回当前根,绝不落启动根回捞。"""
    src = _app_source()
    assert 'return _pick_delivery(root_now + "/" + p.rsplit("/", 1)[-1])' in src, \
        "_pick_delivery 丢了短名拼回当前根的守卫(2026-08-31 跨根串扰事故)"
    # 守卫必须在 resolve_delivery 回捞之前
    i_guard = src.index('return _pick_delivery(root_now + "/"')
    i_resolve = src.index('d = resolve_delivery(path, discover_deliveries(delivery))')
    assert i_guard < i_resolve, "守卫必须先于启动根回捞执行"
