# 发版：打 tag、建 Release、传安装包

桌面用户是看 GitHub Release 判断有没有新版的，所以发一版要做三件事：改版本号、打安装包、
把 tag 和安装包发上去。第三步一条命令。

## 1. 改版本号：三处一起改

| 文件 | 改哪一行 | 谁读它 |
|---|---|---|
| `pyproject.toml` | `version = "0.6.1"` | 打包元数据 |
| `electron/package.json` | `"version": "0.6.1"` | 安装包的版本号和文件名（`biTrader-Setup-0.6.1.exe`） |
| `src/btrader/__init__.py` | `__version__ = "0.6.1"` | 后端启动时 `BITRADER_READY` 报的那个号，桌面端拿它比 |

三处对不上，`release.py` 直接拒（不是提醒，是拒）。改完提交、推到 `xf`：tag 要打在这笔提交上，
工作区不干净脚本不干。

## 2. 打安装包

三步，顺序别换——壳里装的 backend 是第二步的产物，安装包文件名里的版本号来自第二处版本号：

```bat
cd web
npm run build
cd ..
.venv\Scripts\python.exe -m PyInstaller packaging\bitrader.spec --noconfirm
cd electron
npm run dist
```

- `npm run build`（在 `web` 目录）：前端产物写进 `src/btrader/web/static`，下一步会一起打进 exe；
- `PyInstaller packaging\bitrader.spec`：产出 `dist\biTrader-backend\`，壳把它当 `extraResources` 装进
  安装目录的 `backend\`；
- `npm run dist`（在 `electron` 目录）：`electron-builder --win nsis`，产出安装包。

产物是 `dist-electron\biTrader-Setup-<版本>.exe`，一百四十多 MB。`dist-electron/` 在
`.gitignore` 里——**安装包不进仓库**，它只挂在 GitHub Release 上。

顺带说一句 `dist\biTrader-backend\biTrader-backend.exe`：它是给 Electron 壳用的，console 子系统的
程序。壳那边起它只给了 `windowsHide: true` —— libuv 自己据此置上 `CREATE_NO_WINDOW`，窗口压根
不会出现。（Node 的 `spawn` 没有 `creationflags` 这个选项，照 Windows API 那套写上去并不生效。）

**不要直接双击它**——双击就是开一个黑窗口，窗口关掉服务就没了。要手动起后端：在已经开着的终端里跑
`python.exe run_web.py`，日志直接打在终端里（别用 `Start-Process -WindowStyle Hidden` 隐身拉起，
那样仍会闪一个黑框）；用 `pythonw.exe run_web.py` 起的话没有 stdio，日志去
`<数据目录>\logs\bitrader.log` 看（`run_web.py` 自己 `setup_logging`，终端那点输出兜到
`<数据目录>\logs\console.log`）。

## 3. 发版

```powershell
$env:GITHUB_TOKEN = "ghp_…"          # 需要 Contents: write，见第 4 节
python packaging/release.py --version 0.6.1 --build
```

先看它要做什么（不发请求、不打 tag）：

```powershell
python packaging/release.py --version 0.6.1 --build --dry-run
```

一次跑完的顺序：`git tag -a v0.6.1` → `git push xf v0.6.1` → 建 **draft** Release
（`draft=true`，除了你没人看得见）→ 传 exe（边读边发，打印进度）→ `PATCH` 把 draft 改成正式发布、
顺手写正文 → 打印 Release 的 `html_url`。

先发成 draft、传完再发布，是刻意的：安装包一百五十来兆，传一半断了很常见。要是先发布再传，
`/releases/latest` 立刻把这条件 Release 当成最新，所有用户都看到「有新版本」却下不到包；
停在 draft 就没人看得见，重跑接着传。

- **正文**：`--notes-file notes.md` 从文件读；不给就用最近 20 条提交标题。正文空着会被 GitHub 422 拒。
- **`--build`**：把已经打好的 `dist-electron\biTrader-Setup-<版本>.exe` 一起传上去。脚本不替你打包。
  不带 `--build` 就只发 Release —— 桌面端拿不到新版，除了纯服务器版的修补，别这么发。

## 4. 需要的权限

建 Release、传安装包都要 **Contents: write**：

- 经典 token：勾 `repo`（私有仓库）或 `public_repo`（公开仓库）；
- 细粒度 token：Repository permissions → Contents → Read and write。

token 从哪来：先看环境变量 `GITHUB_TOKEN`，没有再问 `git credential fill`（就是你 `git push`
用的那把）。它在脚本里只用来拼 Authorization 头，打印出来最多是 `ghp_…（长度 40）`，
不写进文件、不进日志、不进异常信息。

## 5. 停下来的情况

前置校验是四项：`--version` 写成 `0.6.1` 这种三段数字、三处版本号都等于它、工作区干净、
带 `--build` 时安装包就位。任一项不过就退出，而且**什么都没做**——不打 tag、不 push、不发请求。
问题一次报全，不用逐个试。token 是在这四项之后才取的（先 `GITHUB_TOKEN`，没有再
`git credential fill`），拿不到也一样什么都不做。

tag 打过了不算「不能发」：脚本会先问一句 GitHub「这个 tag 上的 Release 是不是停在 draft」。
是 draft 就接着传（不重打 tag、不重推，传完自动发布）；不是 draft 说明这版已经发过，抬一个版本；
根本没有 Release 就停下，让人自己处理。

中途失败就停在那一步，贴命令或 GitHub 的原始输出，不重试、不猜原因：

- **403**：这把 token 没有 Contents: write。换一把，或者干脆在网页上手动发。
- **422**：常见两种——`v<版本>` 的 tag 已经存在；Release 正文是空的。
- **上传断了、或者最后发布那一步没成**：stderr 会写明那条件 Release 还是 draft、用户看不到。
  网络缓过来之后把同一条命令再跑一次就行——tag 还在、draft 还在，脚本认出它接着传，传完自动发布
  （安装包已经在的直接跳过上传）。
- **不想续传**：网页上删掉那个 draft Release，`git tag -d v0.6.1`，然后重新跑一遍。
- **tag 推上去了但 Release 没建**（比如 push 之后断了）：重跑会停下来说「GitHub 上没有它的 Release」。
  在网页上手动发这一版，或者 `git tag -d v0.6.1` 之后重跑。

## 6. 桌面端和服务器版看的不是一件事

- **Windows 桌面端**：装好的 exe 里没有 git、没有 Python 环境，**它不做在线更新**——只查一次
  GitHub Releases 的最新 tag，和包里的 `__version__` 比。有新版就在设置页显示一张卡，按钮是
  「下载新安装包」，点开是浏览器下载，装一遍就完事。Release 上没有 exe，用户看到「有新版本」
  却点不动下载，只能自己去 Release 页面翻，所以**每次发版都要传安装包**。
- **Linux 服务器版**：不看 Release，按提交 SHA 比（设置页那张卡走 `git fetch` + `--ff-only 合并`），
  服务器上不发 Release 也能更新。

两边的版本号都来自同一个 `__version__`，所以第 1 节那三行要一起改。

## 7. 版本号抬错了

已经发出去的 tag 不要改——`git tag -f` 之后别人的仓库和你对不上，桌面端也会看到两个 0.6.1。
改对版本号，抬一个新版本再发；旧的 tag 和 Release 在网页上删掉。

## 8. 旧版用户升级会看到什么（0.6.3 → 0.6.4）

- **数据目录没变**：冻结的桌面端仍然读写 `%APPDATA%\biTrader`，配置、Key、策略、回测记录、
  K 线缓存、成交历史都在。变的是「谁用哪一套」——从源码跑起来（`run_web.py`、
  `python -m btrader.web`、electron 的 dev 模式）现在落到 `%APPDATA%\biTrader-dev`。
  两条路分开之后，开发时造的测试数据进不了用户的库。
- **旧表不删，改名留档**：库升到 v1 时旧的 `orders` / `trades` 被改名成
  `orders_v0_backup` / `trades_v0_backup`，留在同一个库里。新表是空的（旧行没有
  `market_type`，撞号的那条早被覆盖，读不出来），但数据还在，能人工查。
- **旧 key 搬进作用域**：0.6.3 只有一份 `live.ledger.v1` / `risk.state.v1`。升级后第一次
  读时搬到 `:<当前网络>` 作用域（`live.ledger.v1:testnet` 之类），旧 key 原样保留。
  不搬的话老用户升级上来账本是空的——现货仓位显示 0，不带 size 的平仓还会报
  「账本里没有这一对」。
- **降级不保证**：装回 0.6.3 时库已经是 v1 结构，旧代码写 `trades` 少一列会直接报错。
  升级路径兼容，回退不兼容。
