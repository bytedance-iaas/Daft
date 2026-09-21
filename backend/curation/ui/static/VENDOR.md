# 本目录的第三方前端资产(vendored)

内嵌式网页终端的前端全部**打进仓库**,不走 CDN:pod 里没有公网 npm/CDN 通路,
demo 现场也不能靠外网。三个文件均为 **MIT License**(见 `xterm.css` 头部的完整
license 文本,xterm.js 与 addon-fit.js 是同一 upstream 的构建产物)。

| 文件 | upstream 包 | 版本 | md5 |
|---|---|---|---|
| `xterm.js` | [`@xterm/xterm`](https://github.com/xtermjs/xterm.js) `lib/xterm.js` | **5.5.0** | `7ab162c8b43e3d6590400143d9b4388f` |
| `xterm.css` | `@xterm/xterm` `css/xterm.css` | **5.5.0** | `b9e25e4fb0f5a4fbf4b23962dc246b34` |
| `addon-fit.js` | [`@xterm/addon-fit`](https://github.com/xtermjs/xterm.js/tree/master/addons/addon-fit) `lib/addon-fit.js` | **0.10.0** | `f8d75700a8b8838b556c11d7e0a61845` |

版本是**按字节比对确认**的(2026-07-29):文件先由同事的 lerobot-agent-console 仓库
`static/vendor/` 取得,再与 `https://unpkg.com/@xterm/xterm@5.5.0/lib/xterm.js` 等
逐个 md5 对照,三个全等 → 未经改动的官方构建产物。

升级办法(别手改这些文件):

```sh
curl -fsSLO https://unpkg.com/@xterm/xterm@<版本>/lib/xterm.js
curl -fsSL  https://unpkg.com/@xterm/xterm@<版本>/css/xterm.css   -o xterm.css
curl -fsSL  https://unpkg.com/@xterm/addon-fit@<版本>/lib/addon-fit.js -o addon-fit.js
```

`term.js` 是**我们自己写的**(不是第三方),见文件头注释。
