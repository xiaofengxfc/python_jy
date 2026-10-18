"""Settings: API credentials, network, risk limits and strategy directory."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ... import paths
from ...context import AppContext
from ...core.types import MarketType, Network
from ..theme import TEXT_DIM
from ..widgets.common import mono_font
from ..widgets.tables import DataTable
from ..workers import TaskRunner

log = logging.getLogger(__name__)


class SettingsPage(QWidget):
    status = Signal(str)
    network_changed = Signal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.runner = TaskRunner(self, max_threads=1)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        root = QVBoxLayout(holder)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(12)
        root.addWidget(self._build_api_group())
        root.addWidget(self._build_network_group())
        root.addWidget(self._build_risk_group())
        root.addWidget(self._build_strategy_group())
        root.addWidget(self._build_paths_group())
        root.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll)

    def _build_api_group(self) -> QWidget:
        group = QGroupBox("币安 API 凭据")
        layout = QVBoxLayout(group)
        hint = QLabel(
            "API Key 与 Secret 通过系统密钥环（Windows 凭据管理器）保存，不会写入配置文件。"
            "建议只授予「读取 + 现货/合约交易」权限，切勿开启提现权限，并绑定 IP 白名单。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self.status_label = QLabel("未知")
        form.addRow("当前状态", self.status_label)

        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("粘贴 API Key")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API Key", self.key_edit)

        self.secret_edit = QLineEdit()
        self.secret_edit.setPlaceholderText("粘贴 API Secret")
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API Secret", self.secret_edit)

        self.show_check = QCheckBox("显示明文")
        self.show_check.toggled.connect(
            lambda checked: (
                self.key_edit.setEchoMode(
                    QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
                ),
                self.secret_edit.setEchoMode(
                    QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
                ),
            )
        )
        form.addRow("", self.show_check)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        save_btn = QPushButton("保存凭据")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._save_credentials)
        buttons.addWidget(save_btn)

        test_btn = QPushButton("测试连接")
        test_btn.setObjectName("Flat")
        test_btn.clicked.connect(self._test_connection)
        buttons.addWidget(test_btn)

        delete_btn = QPushButton("删除凭据")
        delete_btn.setObjectName("Flat")
        delete_btn.clicked.connect(self._delete_credentials)
        buttons.addWidget(delete_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.api_result = QLabel("")
        self.api_result.setWordWrap(True)
        layout.addWidget(self.api_result)
        return group

    def _build_network_group(self) -> QWidget:
        group = QGroupBox("网络与市场")
        form = QFormLayout(group)
        self.network_combo = QComboBox()
        for network in Network:
            self.network_combo.addItem(network.label, network.value)
        self.network_combo.currentIndexChanged.connect(self._on_network_changed)
        form.addRow("环境", self.network_combo)

        self.market_combo = QComboBox()
        for market in MarketType:
            self.market_combo.addItem(market.label, market.value)
        self.market_combo.currentIndexChanged.connect(self._on_market_changed)
        form.addRow("默认市场", self.market_combo)

        self.sound_check = QCheckBox("信号提醒音")
        self.sound_check.toggled.connect(self._save_notifications)
        form.addRow("", self.sound_check)
        return group

    def _build_risk_group(self) -> QWidget:
        group = QGroupBox("风控限制（0 表示不限制）")
        layout = QVBoxLayout(group)
        form = QFormLayout()
        risk = self.ctx.settings.risk

        self.max_order_spin = QDoubleSpinBox()
        self.max_order_spin.setRange(0, 1e9)
        self.max_order_spin.setDecimals(2)
        self.max_order_spin.setValue(risk.max_order_quote)
        self.max_order_spin.setSuffix(" USDT")
        form.addRow("单笔最大金额", self.max_order_spin)

        self.max_position_spin = QDoubleSpinBox()
        self.max_position_spin.setRange(0, 1e9)
        self.max_position_spin.setDecimals(2)
        self.max_position_spin.setValue(risk.max_position_quote)
        self.max_position_spin.setSuffix(" USDT")
        form.addRow("单品种最大持仓", self.max_position_spin)

        self.max_leverage_spin = QDoubleSpinBox()
        self.max_leverage_spin.setRange(1, 125)
        self.max_leverage_spin.setDecimals(0)
        self.max_leverage_spin.setValue(risk.max_leverage)
        self.max_leverage_spin.setSuffix(" x")
        form.addRow("最大杠杆", self.max_leverage_spin)

        self.max_loss_spin = QDoubleSpinBox()
        self.max_loss_spin.setRange(0, 1e9)
        self.max_loss_spin.setDecimals(2)
        self.max_loss_spin.setValue(risk.max_daily_loss)
        self.max_loss_spin.setSuffix(" USDT")
        form.addRow("当日最大亏损", self.max_loss_spin)

        self.max_trades_spin = QSpinBox()
        self.max_trades_spin.setRange(0, 10_000)
        self.max_trades_spin.setValue(risk.max_daily_trades)
        self.max_trades_spin.setSuffix(" 笔")
        form.addRow("当日最大交易次数", self.max_trades_spin)

        self.max_losses_spin = QSpinBox()
        self.max_losses_spin.setRange(0, 100)
        self.max_losses_spin.setValue(risk.max_consecutive_losses)
        self.max_losses_spin.setSuffix(" 笔")
        form.addRow("连续亏损熔断", self.max_losses_spin)

        self.confirm_check = QCheckBox("实盘下单前二次确认")
        self.confirm_check.setChecked(risk.require_confirmation)
        form.addRow("", self.confirm_check)
        layout.addLayout(form)

        kill_row = QHBoxLayout()
        self.kill_check = QCheckBox("全局急停（拒绝所有新订单）")
        self.kill_check.setChecked(risk.kill_switch)
        kill_row.addWidget(self.kill_check)
        kill_row.addStretch(1)
        layout.addLayout(kill_row)

        row = QHBoxLayout()
        save_btn = QPushButton("保存风控设置")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._save_risk)
        row.addWidget(save_btn)
        self.risk_state = QLabel("")
        self.risk_state.setObjectName("Muted")
        row.addWidget(self.risk_state)
        row.addStretch(1)
        layout.addLayout(row)
        return group

    def _build_strategy_group(self) -> QWidget:
        group = QGroupBox("策略插件")
        layout = QVBoxLayout(group)
        hint = QLabel(
            "把自己的策略 .py 文件放入下面的目录，重启应用后即可在回测/自动交易中选择。"
            "策略需继承 btrader.strategy.base.Strategy 并实现 generate()。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        self.strategy_path = QLabel(str(paths.user_strategies_dir()))
        self.strategy_path.setFont(mono_font(9))
        self.strategy_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.strategy_path, 1)
        open_btn = QPushButton("打开目录")
        open_btn.setObjectName("Flat")
        open_btn.clicked.connect(self._open_strategy_dir)
        row.addWidget(open_btn)
        reload_btn = QPushButton("重新加载")
        reload_btn.setObjectName("Flat")
        reload_btn.clicked.connect(self._reload_strategies)
        row.addWidget(reload_btn)
        layout.addLayout(row)

        self.strategy_table = DataTable(["策略", "Key", "说明"])
        self.strategy_table.setMinimumHeight(140)
        layout.addWidget(self.strategy_table)
        self._fill_strategy_table()
        return group

    def _build_paths_group(self) -> QWidget:
        group = QGroupBox("数据目录")
        layout = QVBoxLayout(group)
        for label, path in [
            ("配置", paths.data_dir()),
            ("缓存", paths.cache_dir()),
            ("日志", paths.logs_dir()),
        ]:
            row = QHBoxLayout()
            name = QLabel(label)
            name.setFixedWidth(60)
            value = QLabel(str(path))
            value.setFont(mono_font(9))
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(name)
            row.addWidget(value, 1)
            layout.addLayout(row)
        open_btn = QPushButton("打开数据目录")
        open_btn.setObjectName("Flat")
        open_btn.setFixedWidth(130)
        open_btn.clicked.connect(lambda: self._open_path(paths.data_dir()))
        layout.addWidget(open_btn)
        return group

    # ---------------------------------------------------------------- loading
    def _load_values(self) -> None:
        s = self.ctx.settings
        self.network_combo.blockSignals(True)
        self.market_combo.blockSignals(True)
        self.network_combo.setCurrentIndex(self.network_combo.findData(s.network))
        self.market_combo.setCurrentIndex(self.market_combo.findData(s.market_type))
        self.network_combo.blockSignals(False)
        self.market_combo.blockSignals(False)
        self.sound_check.setChecked(s.notification_sound)
        self._update_credential_status()

    def _update_credential_status(self) -> None:
        creds = self.ctx.credentials.load(self.ctx.network, self.ctx.market_type)
        if creds.is_set:
            self.status_label.setText(f"已配置（{creds.masked_key}）· {self.ctx.network.label}")
            self.status_label.setStyleSheet("color: #26a69a;")
        else:
            self.status_label.setText("未配置")
            self.status_label.setStyleSheet(f"color: {TEXT_DIM};")

    def _fill_strategy_table(self) -> None:
        metas = self.ctx.strategies.meta_list()
        self.strategy_table.setRowCount(0)
        for row, meta in enumerate(metas):
            self.strategy_table.set_row(
                row, [meta["name"], meta["key"], meta["description"][:80]]
            )
        if not metas:
            self.strategy_table.setRowCount(1)
            self.strategy_table.set_row(0, ["（未加载任何策略）", "", ""])

    # ----------------------------------------------------------------- events
    def _save_credentials(self) -> None:
        key = self.key_edit.text().strip()
        secret = self.secret_edit.text().strip()
        if not key or not secret:
            QMessageBox.information(self, "提示", "请填写完整的 API Key 与 Secret。")
            return
        if self.ctx.save_credentials(key, secret):
            self.key_edit.clear()
            self.secret_edit.clear()
            self._update_credential_status()
            self.status.emit("凭据已保存到系统密钥环")
            self.api_result.setText("已保存。可点击「测试连接」验证权限。")
        else:
            QMessageBox.warning(self, "保存失败", "无法写入系统密钥环，请检查系统权限。")

    def _test_connection(self) -> None:
        self.api_result.setText("正在测试…")
        market = self.ctx.market_type
        rest = self.ctx.rest_for(market)

        def check():
            balance = rest.fetch_balance(market)
            return balance

        def done(balance) -> None:
            assets = balance.get("total") or {}
            funded = {k: v for k, v in assets.items() if v}
            self.api_result.setText(
                f"连接成功，账户资产 {len(funded)} 项；USDT 余额 "
                f"{float(assets.get('USDT') or 0):,.2f}"
            )
            self.api_result.setStyleSheet("color: #26a69a;")

        def failed(message: str) -> None:
            self.api_result.setText(f"连接失败：{message}")
            self.api_result.setStyleSheet("color: #ef5350;")

        self.runner.run(check, done, failed)

    def _delete_credentials(self) -> None:
        answer = QMessageBox.question(
            self, "删除凭据", "确认从系统密钥环删除当前环境的 API 凭据？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.ctx.credentials.delete(self.ctx.network, self.ctx.market_type)
        from ...core.credentials import Credentials

        self.ctx.creds = Credentials()
        self.ctx.rest.credentials = self.ctx.creds
        self.ctx.rest.reset()
        self._update_credential_status()
        self.status.emit("凭据已删除")

    def _on_network_changed(self) -> None:
        network = Network(self.network_combo.currentData())
        if network.value != self.ctx.network.value:
            self.ctx.set_network(network)
            self._update_credential_status()
            self.network_changed.emit()
            self.status.emit(f"已切换到 {network.label}")

    def _on_market_changed(self) -> None:
        market = MarketType(self.market_combo.currentData())
        if market.value != self.ctx.market_type.value:
            self.ctx.set_market_type(market)
            self._update_credential_status()

    def _save_notifications(self) -> None:
        self.ctx.settings.notification_sound = self.sound_check.isChecked()
        self.ctx.save_settings()

    def _save_risk(self) -> None:
        risk = self.ctx.settings.risk
        risk.max_order_quote = self.max_order_spin.value()
        risk.max_position_quote = self.max_position_spin.value()
        risk.max_leverage = self.max_leverage_spin.value()
        risk.max_daily_loss = self.max_loss_spin.value()
        risk.max_daily_trades = self.max_trades_spin.value()
        risk.max_consecutive_losses = self.max_losses_spin.value()
        risk.require_confirmation = self.confirm_check.isChecked()
        risk.kill_switch = self.kill_check.isChecked()
        self.ctx.risk.settings = risk
        if not risk.kill_switch:
            self.ctx.risk.clear_halt()
        self.ctx.save_settings()
        self.risk_state.setText("已保存")
        self.status.emit("风控设置已保存")

    def _open_strategy_dir(self) -> None:
        self._open_path(paths.user_strategies_dir())

    def _open_path(self, path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except Exception as exc:
            log.warning("打开目录失败: %s", exc)

    def _reload_strategies(self) -> None:
        self.ctx.strategies.discover([paths.user_strategies_dir()])
        self._fill_strategy_table()
        errors = self.ctx.strategies.errors
        if errors:
            QMessageBox.warning(self, "部分策略加载失败", "\n\n".join(errors[:5]))
        self.status.emit(f"策略已重新加载：{len(self.ctx.strategies)} 个")
