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

## 3. 发版

```powershell
$env:GITHUB_TOKEN = "ghp_…"          # 需要 Contents: write，见第 4 节
python packaging/release.py --version 0.6.1 --build
```

先看它要做什么（不发请求、不打 tag）：

```powershell
python packaging/release.py --version 0.6.1 --build --dry-run
```

一次跑完的顺序：`git tag -a v0.6.1` → `git push xf v0.6.1` → 建 Release → 传 exe（边读边发，
打印进度）→ 打印 Release 的 `html_url`。

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

前置校验有五项：三处版本号一致、工作区干净、`v<版本>` 的 tag 不存在、带 `--build` 时安装包就位、
拿得到 token。任一项不过就退出，而且**什么都没做**——不打 tag、不 push、不发请求。问题一次报全，
不用逐个试。

中途失败就停在那一步，贴命令或 GitHub 的原始输出，不重试、不猜原因：

- **403**：这把 token 没有 Contents: write。换一把，或者干脆在网页上手动发。
- **422**：常见两种——`v<版本>` 的 tag 已经存在；Release 正文是空的。
- **tag 打了但 push 挂了**：本地 tag 还在，先 `git tag -d v0.6.1` 再重跑，否则会被「tag 已存在」拦住。
- **Release 建了但上传断了**：网页上删掉这个 Release（tag 还在就先删 tag）再重跑，或者手动把 exe 拖上去。

## 6. 桌面端和服务器版看的不是一件事

- **Windows 桌面端**：查 GitHub Releases 的最新 tag，和包里的 `__version__` 比。Release 上没有 exe，
  用户点了「立即更新」也下不到东西，所以**每次发版都要传安装包**。
- **Linux 服务器版**：不看 Release，按提交 SHA 比（设置页那张卡走 `git fetch` + `--ff-only 合并`），
  服务器上不发 Release 也能更新。

两边的版本号都来自同一个 `__version__`，所以第 1 节那三行要一起改。

## 7. 版本号抬错了

已经发出去的 tag 不要改——`git tag -f` 之后别人的仓库和你对不上，桌面端也会看到两个 0.6.1。
改对版本号，抬一个新版本再发；旧的 tag 和 Release 在网页上删掉。