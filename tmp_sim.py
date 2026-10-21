import json, sqlite3
from btrader.engine.paper import PaperBroker, PaperAccount

d = sqlite3.connect(r'C:\Users\27815\AppData\Roaming\biTrader\bitrader.db')
raw = dict(d.execute("select key, value from settings_kv"))['paper.account.v1']
snap = json.loads(raw)
b = PaperBroker()
ok = b.restore(snap)
print('restore ok', ok)
print('ledger', b.account.realized_by_symbol)
print('realized_pnl', b.realized_pnl())
print('positions', [(p.symbol, p.contracts, p.realized_pnl) for p in b.account.positions.values()])
print('snapshot ledger', b.snapshot().get('realized_by_symbol'))
