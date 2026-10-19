"""Storage transactions: a failed block must not leave partial writes behind."""

from __future__ import annotations

import pytest

from btrader.core.storage import Database


def test_failed_transaction_rolls_back(tmp_path):
    """A half-written row must not be flushed by the next unrelated commit."""
    db = Database(tmp_path / "tx.db")
    try:
        with pytest.raises(RuntimeError):
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO settings_kv (key, value) VALUES (?,?)",
                    ("partial", "1"),
                )
                raise RuntimeError("boom mid-transaction")

        # Writing something else afterwards commits the still-open transaction
        # on a connection that never rolled back.
        db.set_value("after", "2")

        assert db.get_value("partial") is None
        assert db.get_value("after") == "2"
    finally:
        db.close()


def test_successful_transaction_still_commits(tmp_path):
    db = Database(tmp_path / "tx2.db")
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO settings_kv (key, value) VALUES (?,?)", ("kept", "yes")
            )
        assert db.get_value("kept") == "yes"
    finally:
        db.close()
