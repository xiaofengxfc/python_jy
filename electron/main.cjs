/*
 * biTrader desktop shell.
 *
 * The shell owns nothing but the window: the Python backend is spawned as a
 * child process, announces the port it picked on stdout, and is torn down when
 * the window closes. Everything the user sees is the same web bundle the
 * browser build serves, so the two surfaces cannot drift apart.
 */

const { app, BrowserWindow, ipcMain, shell } = require("electron");
const { spawn } = require("child_process");
const path = require("node:path");
const fs = require("node:fs");

const READY_PREFIX = "BITRADER_READY ";
const DEV_URL = process.env.BITRADER_DEV_URL || "";

let win = null;
let backend = null;
/** Set once the backend handshake arrives; cleared on exit. */
let backendPort = 0;

function backendCommand() {
  if (app.isPackaged) {
    const exe = path.join(process.resourcesPath, "backend", "biTrader-backend.exe");
    return { command: exe, args: [], cwd: path.dirname(exe) };
  }
  // Development: run the package straight from the virtualenv.
  const root = path.resolve(__dirname, "..");
  const python = path.join(root, ".venv", "Scripts", "python.exe");
  return {
    command: fs.existsSync(python) ? python : "python",
    args: ["-m", "btrader.web"],
    cwd: root,
    env: { PYTHONPATH: path.join(root, "src") },
  };
}

function startBackend() {
  const spec = backendCommand();
  const args = [
    ...spec.args,
    "--port",
    "0",
    "--parent-pid",
    String(process.pid),
    "--watch-stdin",
  ];

  // 装好的桌面版只认后端自己挑的那份数据（%APPDATA%\biTrader）。调试时常常设着
  // BITRADER_DATA_DIR，继承下去桌面版就会去读开发目录——正是这一轮要断开的混用。
  // 目录名由 Python 的 paths 决定，壳不复制一份。
  const env = { ...process.env, ...(spec.env || {}) };
  if (app.isPackaged) delete env.BITRADER_DATA_DIR;

  backend = spawn(spec.command, args, {
    cwd: spec.cwd,
    env,
    stdio: ["pipe", "pipe", "pipe"],
    // This line is what keeps the black window away, and it is the only thing that
    // can: Node's spawn has no `creationflags` option at all (the option list is
    // windowsHide / detached / windowsVerbatimArguments, and an unknown key is
    // silently ignored). `windowsHide: true` is the switch — libuv turns it into
    // CREATE_NO_WINDOW when it creates the process. Without it Windows gives the
    // child a console of its own, because the backend exe is built with console=True
    // (console subsystem) and this Electron GUI process has no console to inherit.
    // Deliberately not `detached`: the backend must die with the window.
    windowsHide: true,
  });

  return new Promise((resolve, reject) => {
    let buffered = "";
    let settled = false;

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error("后端 60 秒内未就绪"));
    }, 60_000);

    backend.stdout.setEncoding("utf8");
    backend.stdout.on("data", (chunk) => {
      buffered += chunk;
      const lines = buffered.split(/\r?\n/);
      buffered = lines.pop() ?? "";
      for (const line of lines) {
        const at = line.indexOf(READY_PREFIX);
        if (at === -1) continue;
        try {
          const info = JSON.parse(line.slice(at + READY_PREFIX.length));
          backendPort = info.port;
          if (!settled) {
            settled = true;
            clearTimeout(timer);
            resolve(info);
          }
        } catch {
          /* A malformed handshake is not worth crashing over. */
        }
      }
    });

    backend.stderr.setEncoding("utf8");
    backend.stderr.on("data", (chunk) => {
      // Keep the tail of backend output visible when running from a terminal.
      if (!app.isPackaged) process.stderr.write(chunk);
    });

    backend.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });

    backend.on("exit", (code) => {
      backend = null;
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new Error(`后端提前退出，退出码 ${code}`));
      }
      // The backend is the reason the window exists; if it dies, so do we.
      if (app.isPackaged || code !== 0) app.quit();
    });
  });
}

function createWindow(port) {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    show: false,
    frame: false,
    backgroundColor: "#f7f7f8",
    title: "biTrader",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.once("ready-to-show", () => win.show());

  // External links belong in the user's browser, never in the app frame.
  // Only http/https may reach the OS: handing shell.openExternal a file:, smb: or a
  // custom scheme lets a remote page pick a local handler, which is worse than a no-op.
  win.webContents.setWindowOpenHandler(({ url }) => {
    const target = String(url ?? "");
    if (/^https?:\/\//i.test(target)) void shell.openExternal(target);
    return { action: "deny" };
  });

  // The app frame itself must never leave the local backend. `setWindowOpenHandler`
  // above only covers new windows; a file dropped onto the page makes Chromium
  // navigate this very window to file://, which replaces the trading UI with a
  // blank local file. Only the loopback origins we serve from are allowed, and the
  // check is by origin so file:/data:/custom schemes can never match.
  const allowedOrigins = new Set([`http://127.0.0.1:${port}`, `http://localhost:${port}`]);
  if (DEV_URL) {
    try {
      allowedOrigins.add(new URL(DEV_URL).origin);
    } catch {
      /* dev URL 写坏了就只认回环地址 */
    }
  }
  win.webContents.on("will-navigate", (event, url) => {
    let origin = "";
    try {
      origin = new URL(String(url ?? "")).origin;
    } catch {
      origin = "";
    }
    if (!allowedOrigins.has(origin)) event.preventDefault();
  });

  const target = DEV_URL || `http://127.0.0.1:${port}/`;
  void win.loadURL(target);

  win.on("closed", () => {
    win = null;
  });
}

ipcMain.handle("window:minimize", () => win?.minimize());
ipcMain.handle("window:toggle-maximize", () => {
  if (!win) return false;
  if (win.isMaximized()) win.unmaximize();
  else win.maximize();
  return win.isMaximized();
});
ipcMain.handle("window:close", () => win?.close());

// Only one copy may hold the backend; a second launch focuses the first window.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  app.whenReady().then(async () => {
    try {
      const info = await startBackend();
      createWindow(info.port);
    } catch (error) {
      console.error("后端启动失败:", error);
      const { dialog } = require("electron");
      dialog.showErrorBox(
        "biTrader 无法启动",
        `${error?.message ?? error}\n\n请查看日志后重试。`,
      );
      app.quit();
    }

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0 && backendPort) {
        createWindow(backendPort);
      }
    });
  });
}

app.on("window-all-closed", () => {
  app.quit();
});

app.on("before-quit", () => {
  if (!backend) return;
  // Closing stdin is the backend's own shutdown signal; kill is the backstop.
  try {
    backend.stdin.end();
  } catch {
    /* already gone */
  }
  backend = null;
});
