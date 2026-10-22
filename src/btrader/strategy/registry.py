"""Strategy discovery and plugin loading.

Built-in strategies live in ``btrader.strategy.builtin``; user strategies are
plain ``.py`` files dropped into the per-user strategies directory. A user file
with the same key as a built-in replaces it, which is how the UI lets people
edit a built-in and later restore it.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import pkgutil
import sys
import traceback
from pathlib import Path

from .base import Strategy

log = logging.getLogger(__name__)


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, type[Strategy]] = {}
        # Source file for user plugins. A built-in has no entry here unless a
        # user file overrides it; then this is the overriding file.
        self._sources: dict[str, Path] = {}
        # Keys a package built-in defines, no matter what is in effect now.
        self._builtin_keys: set[str] = set()
        self.errors: list[str] = []

    # ------------------------------------------------------------ discovery
    def discover(self, extra_dirs: list[Path] | None = None) -> None:
        self._strategies.clear()
        self._sources.clear()
        self._builtin_keys.clear()
        self.errors.clear()
        self._load_builtin()
        for folder in extra_dirs or []:
            self._load_folder(Path(folder))
        log.info("已加载 %d 个策略: %s", len(self._strategies), ", ".join(self._strategies))

    def _register(
        self, cls: type[Strategy], source: str, path: Path | None = None, *, builtin: bool = True
    ) -> None:
        try:
            instance = cls()
        except Exception:
            self.errors.append(f"{source}: 无法实例化 {cls.__name__}\n{traceback.format_exc(limit=2)}")
            return
        key = instance.key
        if not key:
            self.errors.append(f"{source}: 策略 {cls.__name__} 缺少 key")
            return
        if key in self._strategies:
            # A user file may replace the built-in it shadows. Everything else
            # still loses: two user files with the same key, or the same key
            # defined twice by the package.
            # Two user files with the same key are still a conflict, whether or
            # not a built-in of that name exists: the second one would silently
            # take the first one's place and the editor would show neither.
            if not builtin and key in self._builtin_keys and key not in self._sources:
                # Not a conflict: this is how the editor overrides a built-in.
                # No warning and no entry in errors — the reload endpoint shows
                # that list to the user and this case is intended.
                log.debug("用户策略覆盖内置: %s（来自 %s）", key, source)
                self._strategies[key] = cls
                if path is not None:
                    self._sources[key] = path.resolve()
                return
            log.warning("策略 key 冲突，跳过 %s（来自 %s）", key, source)
            self.errors.append(f"{source}: 策略 key 重复 '{key}'")
            return
        self._strategies[key] = cls
        if builtin:
            self._builtin_keys.add(key)
        if path is not None:
            self._sources[key] = path.resolve()

    def _load_builtin(self) -> None:
        from . import builtin

        for info in pkgutil.iter_modules(builtin.__path__):
            module_name = f"{builtin.__name__}.{info.name}"
            try:
                module = importlib.import_module(module_name)
            except Exception:
                self.errors.append(f"{module_name}: 导入失败\n{traceback.format_exc(limit=3)}")
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module.__name__:
                    self._register(obj, module_name, builtin=True)

    def _load_folder(self, folder: Path) -> None:
        if not folder.exists():
            return
        for path in sorted(folder.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module_name = f"btrader_user_strategy_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except Exception:
                self.errors.append(f"{path.name}: 加载失败\n{traceback.format_exc(limit=3)}")
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module_name:
                    self._register(obj, path.name, path, builtin=False)

    # --------------------------------------------------------------- lookup
    @property
    def keys(self) -> list[str]:
        return sorted(self._strategies)

    def get(self, key: str) -> Strategy:
        try:
            return self._strategies[key]()
        except KeyError as exc:
            raise KeyError(f"未知策略：{key}") from exc

    def source_path(self, key: str) -> Path | None:
        """The user-plugin file in effect for ``key``, or ``None`` for a built-in.

        A built-in that a user file overrides reports that file, because it is
        what actually runs.
        """
        return self._sources.get(key)

    def is_builtin(self, key: str) -> bool:
        """True when a package built-in defines this key, overridden or not."""
        return key in self._builtin_keys

    def is_overridden(self, key: str) -> bool:
        """True when a user file currently replaces the built-in for this key.

        Derived, never stored: a user file plus a built-in of the same key is
        exactly an override, while a user file on its own is an ordinary plugin.
        """
        return key in self._builtin_keys and key in self._sources

    @property
    def builtin_keys(self) -> list[str]:
        return sorted(self._builtin_keys)

    def meta_list(self) -> list[dict]:
        result = []
        for key in self.keys:
            instance = self._strategies[key]()
            source = self._sources.get(key)
            result.append(
                {
                    "key": key,
                    "name": instance.meta.name,
                    "description": instance.meta.description,
                    "allow_short": instance.meta.allow_short,
                    "params_spec": instance.params_spec,
                    "source_name": source.name if source else "",
                    # Everything is editable now: a built-in without a user
                    # file gets one the moment the editor saves.
                    "editable": True,
                    "builtin": key in self._builtin_keys,
                    "overridden": self.is_overridden(key),
                }
            )
        return result

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, key: str) -> bool:
        return key in self._strategies
