"""Freqtrade sidecar: configuration, process lifecycle and REST client.

The bot runs in its own Python environment and its own process. biTrader never
imports Freqtrade modules, which keeps the GPL project at arm's length and lets
the user upgrade it independently.
"""

from __future__ import annotations

import ast
import base64
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import paths
from .core.config import FreqtradeSettings

log = logging.getLogger(__name__)


class FreqtradeError(RuntimeError):
    pass


class FreqtradeClient:
    """Small REST client for the local Freqtrade API server."""

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token = ""
        self._token_at = 0.0

    def _basic(self) -> str:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def login(self) -> str:
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/token/login",
            method="POST",
            headers={"Authorization": f"Basic {self._basic()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            raise FreqtradeError(f"Freqtrade 登录失败：{exc}") from exc
        token = str(data.get("access_token") or "")
        if not token:
            raise FreqtradeError("Freqtrade 登录响应缺少 access_token")
        self._token = token
        self._token_at = time.monotonic()
        return token

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> Any:
        if auth and (not self._token or time.monotonic() - self._token_at > 600):
            self.login()
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and auth:
                # Token expired or the bot restarted. Retry once with a fresh one.
                self._token = ""
                return self.request(method, path, payload, auth=auth)
            raise FreqtradeError(f"Freqtrade HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise FreqtradeError(f"Freqtrade 不可达：{exc}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise FreqtradeError("Freqtrade 返回了非 JSON 响应") from exc

    def ping(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/v1/ping", timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False


class FreqtradeBridge:
    """Owns the Freqtrade directory, config and (optionally) a child process."""

    def __init__(self, settings: FreqtradeSettings, root: Path | None = None) -> None:
        self.settings = settings
        self.root = (root or paths.freqtrade_dir()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.user_data = self.root / "user_data"
        self.strategy_dir = self.user_data / "strategies"
        self.strategy_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.user_data / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "config.json"
        self.credentials_path = self.root / "credentials.json"
        self.pid_path = self.root / "freqtrade.pid"
        self.log_path = self.root / "freqtrade.log"
        self._process: subprocess.Popen[bytes] | None = None
        self._version = ""
        self._version_at = 0.0

    def _ensure_sitecustomize(self) -> None:
        """Cap the CoinGecko timeout for the sidecar.

        Freqtrade's RPC layer loads a CoinGecko coin list on every start. In
        networks where api.coingecko.com is blocked the bundled client waits
        120 seconds before continuing, which looks like a hung bot. A
        ``sitecustomize`` file in the sidecar root (the subprocess cwd) is
        imported by Python before Freqtrade and only shortens that one host.
        It is not installed into the venv and does not affect biTrader itself.
        """
        path = self.root / "sitecustomize.py"
        if path.exists():
            return
        path.write_text(
            "\"\"\"biTrader: fail fast when CoinGecko is unreachable.\"\"\"\n"
            "import requests\n"
            "\n"
            "_original = requests.Session.request\n"
            "\n"
            "\n"
            "def _request(self, method, url, **kwargs):\n"
            "    if 'api.coingecko.com' in str(url):\n"
            "        kwargs['timeout'] = 2\n"
            "    return _original(self, method, url, **kwargs)\n"
            "\n"
            "\n"
            "requests.Session.request = _request\n",
            encoding="utf-8",
        )


    # ------------------------------------------------------------------ paths
    @property
    def python(self) -> Path:
        configured = (self.settings.python or "").strip()
        if configured:
            return Path(os.path.expandvars(configured)).expanduser().resolve()
        if sys.platform == "win32":
            return self.root / "venv" / "Scripts" / "python.exe"
        return self.root / "venv" / "bin" / "python"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.trade_port}"

    def _credentials(self) -> dict[str, str]:
        if self.credentials_path.exists():
            try:
                raw = json.loads(self.credentials_path.read_text(encoding="utf-8"))
                if all(raw.get(k) for k in ("password", "jwt_secret", "ws_token")):
                    return {
                        "password": str(raw["password"]),
                        "jwt_secret": str(raw["jwt_secret"]),
                        "ws_token": str(raw["ws_token"]),
                    }
            except (OSError, ValueError):
                log.warning("读取 Freqtrade 凭证失败，重新生成", exc_info=True)
        credentials = {
            "password": secrets.token_urlsafe(18),
            "jwt_secret": secrets.token_urlsafe(32),
            "ws_token": secrets.token_urlsafe(24),
        }
        tmp = self.credentials_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
        tmp.replace(self.credentials_path)
        return credentials

    def client(self) -> FreqtradeClient:
        return FreqtradeClient(
            self.base_url, self.settings.username, self._credentials()["password"]
        )

    # ----------------------------------------------------------------- status
    def version(self) -> str:
        if self._version and time.monotonic() - self._version_at < 60:
            return self._version
        if not self.python.exists():
            return ""
        try:
            result = subprocess.run(
                [str(self.python), "-m", "freqtrade", "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        text = (result.stdout or result.stderr or "").strip()
        self._version = text.splitlines()[-1] if text else ""
        self._version_at = time.monotonic()
        return self._version

    def _port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.settings.trade_port), timeout=0.5):
                return True
        except OSError:
            return False

    @property
    def running(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        return self._port_open()

    def log_tail(self, lines: int = 40) -> str:
        if not self.log_path.exists():
            return ""
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(content[-lines:])

    def status(self) -> dict[str, Any]:
        installed = self.python.exists()
        version = self.version() if installed else ""
        running = self.running
        api_ok = False
        if running:
            try:
                api_ok = self.client().ping()
            except FreqtradeError:
                api_ok = False
        return {
            "enabled": self.settings.enabled,
            "installed": installed,
            "version": version,
            "python": str(self.python),
            "root": str(self.root),
            "config_exists": self.config_path.exists(),
            "running": running,
            "api_ok": api_ok,
            "trade_port": self.settings.trade_port,
            "exchange": self.settings.exchange,
            "dry_run": self.settings.dry_run,
            "strategy": self.settings.strategy,
            "timeframe": self.settings.timeframe,
            "pairs": list(self.settings.pairs),
            "log_tail": self.log_tail(),
        }

    # ----------------------------------------------------------------- config
    def write_config(self) -> Path:
        credentials = self._credentials()
        config: dict[str, Any] = {
            "max_open_trades": max(1, int(self.settings.max_open_trades)),
            "stake_currency": "USDT",
            "stake_amount": max(1.0, float(self.settings.stake_amount)),
            "tradable_balance_ratio": 0.99,
            "fiat_display_currency": "USD",
            "dry_run": bool(self.settings.dry_run),
            "dry_run_wallet": 10_000,
            "cancel_open_orders_on_exit": True,
            "trading_mode": "spot",
            "margin_mode": "",
            "timeframe": self.settings.timeframe or "15m",
            "exchange": {
                "name": self.settings.exchange or "gate",
                "key": "",
                "secret": "",
                "ccxt_config": {},
                "ccxt_async_config": {},
                "pair_whitelist": list(self.settings.pairs) or ["BTC/USDT"],
                "pair_blacklist": [],
            },
            "entry_pricing": {
                "price_side": "same",
                "use_order_book": False,
                "order_book_top": 1,
            },
            "exit_pricing": {
                "price_side": "same",
                "use_order_book": False,
                "order_book_top": 1,
            },
            "pairlists": [{"method": "StaticPairList"}],
            "api_server": {
                "enabled": True,
                "listen_ip_address": "127.0.0.1",
                "listen_port": int(self.settings.trade_port),
                "username": self.settings.username,
                "password": credentials["password"],
                "jwt_secret_key": credentials["jwt_secret"],
                "ws_token": credentials["ws_token"],
                "CORS_origins": [],
            },
            "bot_name": "biTrader",
            "initial_state": "stopped",
            "force_entry_enable": False,
            "internals": {"process_throttle_secs": 5},
            "dataformat_ohlcv": "json",
            "dataformat_trades": "jsongz",
        }
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.config_path)
        return self.config_path

    # --------------------------------------------------------------- lifecycle
    def start(self, strategy: str = "") -> dict[str, Any]:
        if not self.python.exists():
            raise FreqtradeError(
                f"Freqtrade 未安装：{self.python}。请先创建独立 venv 并安装 freqtrade。"
            )
        if self.running:
            return self.status()
        selected = (strategy or self.settings.strategy or "").strip()
        if not selected:
            strategies = self.strategies()
            if not strategies:
                raise FreqtradeError("Freqtrade 策略目录为空，请先保存一个策略")
            selected = strategies[0]["name"]
        self.write_config()
        self._ensure_sitecustomize()
        args = [
            str(self.python),
            "-m",
            "freqtrade",
            "trade",
            "--config",
            str(self.config_path),
            "--strategy",
            selected,
        ]
        log_file = open(self.log_path, "ab")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.root) + os.pathsep + environment.get("PYTHONPATH", "")
        try:
            self._process = subprocess.Popen(
                args,
                cwd=str(self.root),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        finally:
            log_file.close()
        self.pid_path.write_text(str(self._process.pid), encoding="utf-8")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise FreqtradeError(
                    "Freqtrade 启动后退出，日志：\n" + (self.log_tail(20) or "无输出")
                )
            try:
                if self.client().ping():
                    break
            except FreqtradeError:
                pass
            time.sleep(1)
        else:
            raise FreqtradeError("Freqtrade 启动超时，请查看日志")
        self.settings.strategy = selected
        return self.status()

    def stop(self) -> dict[str, Any]:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        elif self.pid_path.exists():
            try:
                pid = int(self.pid_path.read_text(encoding="utf-8").strip())
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        self._process = None
        try:
            self.pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        deadline = time.monotonic() + 10
        while self._port_open() and time.monotonic() < deadline:
            time.sleep(0.3)
        return self.status()

    # -------------------------------------------------------------- strategies
    def strategies(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.strategy_dir.glob("*.py")):
            classes: list[str] = []
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        classes.append(node.name)
            except (OSError, SyntaxError):
                pass
            items.append(
                {
                    "name": path.stem,
                    "file": path.name,
                    "classes": classes,
                    "mtime": path.stat().st_mtime,
                }
            )
        return items

    def strategy_source(self, name: str) -> str:
        path = (self.strategy_dir / f"{Path(name).stem}.py").resolve()
        if path.parent != self.strategy_dir:
            raise FreqtradeError("策略路径越界")
        if not path.exists():
            raise FreqtradeError(f"策略不存在：{name}")
        return path.read_text(encoding="utf-8")

    def save_strategy(self, name: str, code: str) -> dict[str, Any]:
        stem = "".join(ch for ch in name if ch.isalnum() or ch in "_-")[:64]
        if not stem:
            raise FreqtradeError("策略文件名不能为空")
        path = (self.strategy_dir / f"{stem}.py").resolve()
        if path.parent != self.strategy_dir:
            raise FreqtradeError("策略路径越界")
        try:
            ast.parse(code)
        except SyntaxError as exc:
            raise FreqtradeError(f"策略语法错误：{exc}") from exc
        if "IStrategy" not in code:
            raise FreqtradeError("策略必须继承 freqtrade.strategy.IStrategy")
        cleaned = code.replace("\r\n", "\n").strip() + "\n"
        tmp = path.with_suffix(".py.tmp")
        tmp.write_text(cleaned, encoding="utf-8")
        tmp.replace(path)
        return {"ok": True, "name": stem, "file": path.name}

    # --------------------------------------------------------------- backtest
    def write_ohlcv(self, pair: str, timeframe: str, rows: Any) -> dict[str, Any]:
        """Write OHLCV rows in the JSON format Freqtrade's datahandler expects.

        ``rows`` is an iterable of ``[timestamp_ms, open, high, low, close,
        volume]`` (a pandas DataFrame with the same columns also works).
        """
        if not pair or "/" not in pair:
            raise FreqtradeError(f"交易对格式应为 BTC/USDT：{pair}")
        safe_pair = pair.replace("/", "_").replace(":", "_")
        folder = self.data_dir / (self.settings.exchange or "gate")
        folder.mkdir(parents=True, exist_ok=True)
        path = (folder / f"{safe_pair}-{timeframe}.json").resolve()
        if path.parent != folder.resolve():
            raise FreqtradeError("行情文件路径越界")
        payload: list[list[float]] = []
        if hasattr(rows, "itertuples"):
            for row in rows.itertuples(index=False):
                values = list(row)
                payload.append(
                    [
                        int(values[0]),
                        float(values[1]),
                        float(values[2]),
                        float(values[3]),
                        float(values[4]),
                        float(values[5]),
                    ]
                )
        else:
            for row in rows:
                values = list(row)
                payload.append(
                    [
                        int(values[0]),
                        float(values[1]),
                        float(values[2]),
                        float(values[3]),
                        float(values[4]),
                        float(values[5]),
                    ]
                )
        if not payload:
            raise FreqtradeError("没有可写入的 K 线")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
        return {
            "ok": True,
            "path": str(path),
            "rows": len(payload),
            "pair": pair,
            "timeframe": timeframe,
            "exchange": self.settings.exchange,
        }

    def backtest(
        self,
        strategy: str,
        *,
        timerange: str = "",
        timeframe: str = "",
        pairs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a Freqtrade CLI backtest and map its result to the UI shape.

        Local OHLCV files must already exist under
        ``user_data/data/<exchange>/<PAIR>-<tf>.json``. The data bridge that
        fills them is part of the next phase; this method fails with a clear
        message when they are missing.
        """
        if not self.python.exists():
            raise FreqtradeError("Freqtrade 未安装")
        if not (self.strategy_dir / f"{strategy}.py").exists():
            raise FreqtradeError(f"策略文件不存在：{strategy}.py")
        self.write_config()
        args = [
            str(self.python),
            "-m",
            "freqtrade",
            "backtesting",
            "--config",
            str(self.config_path),
            "--strategy",
            strategy,
            "--data-format-ohlcv",
            "json",
            "--export",
            "trades",
        ]
        if timerange:
            args.extend(["--timerange", timerange])
        if timeframe:
            args.extend(["--timeframe", timeframe])
        if pairs:
            args.extend(["--pairs", *pairs])
        result = subprocess.run(
            args,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=900,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode not in (0, 1):
            raise FreqtradeError(f"Freqtrade 回测失败：\n{output[-1500:]}")
        pointer = self.user_data / "backtest_results" / ".last_result.json"
        if not pointer.exists():
            raise FreqtradeError("Freqtrade 没有生成回测结果：\n" + output[-1500:])
        latest = json.loads(pointer.read_text(encoding="utf-8")).get("latest_backtest", "")
        archive = self.user_data / "backtest_results" / latest
        if not archive.exists():
            raise FreqtradeError(f"回测结果文件不存在：{archive}")
        data = _read_backtest_archive(archive, strategy)
        return _map_backtest(data, strategy)

    def backtest_status(self) -> dict[str, Any]:
        pointer = self.user_data / "backtest_results" / ".last_result.json"
        if not pointer.exists():
            return {"available": False}
        latest = json.loads(pointer.read_text(encoding="utf-8")).get("latest_backtest", "")
        return {
            "available": True,
            "latest": latest,
            "path": str(self.user_data / "backtest_results" / latest),
        }


def _read_backtest_archive(archive: Path, strategy: str) -> dict[str, Any]:
    import zipfile

    with zipfile.ZipFile(archive) as zipped:
        names = [
            name
            for name in zipped.namelist()
            if name.endswith(".json") and "_config" not in name
        ]
        if not names:
            raise FreqtradeError("回测压缩包中没有结果 JSON")
        raw = json.loads(zipped.read(names[0]))
    strategies = raw.get("strategy") or {}
    if strategy in strategies:
        return strategies[strategy]
    if strategies:
        return next(iter(strategies.values()))
    raise FreqtradeError("回测结果中没有策略数据")


def _map_backtest(data: dict[str, Any], strategy: str) -> dict[str, Any]:
    trades = list(data.get("trades") or [])
    starting = float(data.get("starting_balance") or 10_000)
    rows: list[dict[str, Any]] = []
    equity = [{"t": int(data.get("backtest_start_ts") or 0), "equity": starting}]
    running = starting
    for trade in sorted(trades, key=lambda item: int(item.get("close_timestamp") or 0)):
        is_short = bool(trade.get("is_short"))
        amount = float(trade.get("amount") or 0)
        open_ts = int(trade.get("open_timestamp") or 0)
        close_ts = int(trade.get("close_timestamp") or 0)
        rows.append(
            {
                "timestamp": open_ts,
                "side": "sell" if is_short else "buy",
                "price": float(trade.get("open_rate") or 0),
                "amount": amount,
                "pnl": 0.0,
                "reason": "entry",
            }
        )
        profit = float(trade.get("profit_abs") or 0)
        running += profit
        rows.append(
            {
                "timestamp": close_ts,
                "side": "buy" if is_short else "sell",
                "price": float(trade.get("close_rate") or 0),
                "amount": amount,
                "pnl": profit,
                "reason": str(trade.get("exit_reason") or ""),
            }
        )
        equity.append({"t": close_ts, "equity": running})
    total_return = float(data.get("profit_total") or 0)
    return {
        "metrics": {
            "total_return": total_return,
            "annual_return": float(data.get("cagr") or 0),
            "max_drawdown": float(data.get("max_drawdown_account") or 0),
            "sharpe": float(data.get("sharpe") or 0),
            "win_rate": float(data.get("winrate") or 0),
            "profit_factor": float(data.get("profit_factor") or 0),
            "trades": float(data.get("total_trades") or 0),
            "fee_paid": float(data.get("total_volume") or 0) * 0.001,
        },
        "params": {"strategy": strategy, "timeframe": data.get("timeframe", "")},
        "equity": equity,
        "trades": rows,
        "source": "freqtrade",
    }
