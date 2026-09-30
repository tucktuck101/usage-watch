"""The account registry (D7): accounts, alias assertions, merges.

An `account_key` is random and never derived from an alias. Aliases never
move: a merge joins accounts, and unmerging revokes the merge row only.
The canonical account follows unrevoked merges.

No function here opens a transaction: the caller owns them.
"""

from __future__ import annotations

import secrets
import sqlite3
import time

from usage_watch import model
from usage_watch.errors import Problem


def _now() -> int:
    return int(time.time() * 1000)


def _exists(conn: sqlite3.Connection, account_key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM accounts WHERE account_key = ?", (account_key,)
    ).fetchone() is not None


def create_account(conn: sqlite3.Connection, provider: str, label: str | None = None) -> str:
    """Create an account with a random 128-bit hex key and return the key."""
    key = secrets.token_hex(16)
    now = _now()
    conn.execute(
        "INSERT INTO accounts (account_key, provider, label, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?)",
        (key, provider, label, now, now),
    )
    return key


def canonical(conn: sqlite3.Connection, account_key: str) -> str:
    """Follow unrevoked merges from `account_key` to the account with none."""
    seen = {account_key}
    key = account_key
    while True:
        row = conn.execute(
            "SELECT into_key FROM account_merges WHERE from_key = ? AND revoked_at IS NULL",
            (key,),
        ).fetchone()
        if row is None:
            return key
        key = row[0]
        if key in seen:  # merge() refuses cycles; this only guards a hand-edited store
            raise Problem(
                f"account merges form a cycle through {key}",
                "revoke one of the merges in the cycle with: usage-watch accounts unmerge <merge_id>",
                expected="unrevoked merges that end at one canonical account",
            )
        seen.add(key)


def _reported_accounts(conn: sqlite3.Connection, alias_kind: str, alias_hash: str) -> set[str]:
    """Canonical accounts the alias's unrevoked `reported` rows resolve to (D7)."""
    rows = conn.execute(
        "SELECT account_key FROM account_aliases"
        " WHERE alias_kind = ? AND alias_hash = ? AND evidence = 'reported'"
        " AND revoked_at IS NULL",
        (alias_kind, alias_hash),
    ).fetchall()
    return {canonical(conn, r[0]) for r in rows}


def assert_alias(conn: sqlite3.Connection, *, provider: str, alias_kind: str, alias_hash: str,
                 asserted_by: str, evidence: str, confidence: str,
                 account_key: str | None = None, verified_by: str | None = None) -> str:
    """Record (or re-confirm) an alias assertion. If account_key is None, reuse the
    account an unrevoked alias with the same (alias_kind, alias_hash) already points
    at (canonical), else create one. Returns the canonical account_key."""
    if alias_kind not in model.ALIAS_KINDS:
        raise Problem(f"unknown alias kind {alias_kind!r}",
                      "use one of the alias kinds D7 lists",
                      expected=", ".join(model.ALIAS_KINDS))
    if evidence not in model.ALIAS_EVIDENCES:
        raise Problem(f"unknown alias evidence {evidence!r}",
                      "use 'reported', or 'co_reported' with the other alias's account_key",
                      expected=", ".join(model.ALIAS_EVIDENCES))
    if confidence not in model.CONFIDENCES:
        raise Problem(f"unknown confidence {confidence!r}",
                      "use one of the confidence values D1 lists",
                      expected=", ".join(model.CONFIDENCES))
    now = _now()
    if account_key is None:
        if evidence == "co_reported":
            raise Problem(
                f"co_reported {alias_kind} assertion from {asserted_by} names no account",
                "pass account_key: the account of the alias it was reported alongside",
                expected="a co_reported row written against the other alias's account (D7)",
            )
        own = conn.execute(
            "SELECT account_key FROM account_aliases WHERE alias_kind = ? AND alias_hash = ?"
            " AND asserted_by = ? AND evidence = 'reported' AND revoked_at IS NULL",
            (alias_kind, alias_hash, asserted_by),
        ).fetchall()
        if len(own) == 1:
            account_key = own[0][0]  # this source re-confirming its own row
        else:
            existing = _reported_accounts(conn, alias_kind, alias_hash)
            if len(existing) > 1:
                raise Problem(
                    f"{alias_kind} alias is in conflict: its reported rows resolve to "
                    f"{len(existing)} accounts",
                    "pass account_key explicitly, or merge or revoke the conflicting "
                    "assertions (usage-watch doctor lists them)",
                    expected="an alias whose reported rows resolve to one canonical account",
                )
            account_key = existing.pop() if existing else create_account(conn, provider)
    elif not _exists(conn, account_key):
        raise Problem(
            f"account {account_key} does not exist",
            "create it with create_account, or pass account_key=None to resolve it from the alias",
            expected="an account_key present in accounts",
        )
    conn.execute(
        "INSERT INTO account_aliases (alias_kind, alias_hash, account_key, asserted_by,"
        " evidence, confidence, verified_by, first_seen, last_confirmed)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (alias_kind, alias_hash, account_key, asserted_by) DO UPDATE SET"
        " last_confirmed = max(last_confirmed, excluded.last_confirmed),"
        " verified_by = coalesce(excluded.verified_by, verified_by)",
        (alias_kind, alias_hash, account_key, asserted_by, evidence, confidence,
         verified_by, now, now),
    )
    # D7: seeing an account again clears its tombstone.
    conn.execute(
        "UPDATE accounts SET last_seen = max(last_seen, ?), removed_at = NULL"
        " WHERE account_key = ?",
        (now, account_key),
    )
    return canonical(conn, account_key)


def merge(conn: sqlite3.Connection, from_key: str, into_key: str, *,
          evidence: str, verified_by: str) -> int:
    """Merge `from_key` into `into_key` (D7). Both must be canonical. Refuses a
    cycle and a second live outgoing merge. Returns merge_id."""
    if evidence not in model.MERGE_EVIDENCES:
        raise Problem(f"unknown merge evidence {evidence!r}",
                      "use 'co_reported' (verified pairing) or 'user'",
                      expected=", ".join(model.MERGE_EVIDENCES))
    for key in (from_key, into_key):
        if not _exists(conn, key):
            raise Problem(f"account {key} does not exist",
                          "list accounts with: usage-watch accounts",
                          expected="two existing account keys")
    if from_key == into_key:
        raise Problem(f"merge of account {from_key} into itself",
                      "name two different accounts",
                      expected="from_key and into_key to differ")
    live = conn.execute(
        "SELECT merge_id, into_key FROM account_merges WHERE from_key = ? AND revoked_at IS NULL",
        (from_key,),
    ).fetchone()
    if live is not None:
        raise Problem(
            f"account {from_key} already has a live merge (merge {live[0]} into {live[1]})",
            f"merge its canonical account {canonical(conn, from_key)} instead, or revoke "
            f"merge {live[0]} first with: usage-watch accounts unmerge {live[0]}",
            expected="at most one unrevoked outgoing merge per account (D7 5.)",
        )
    if canonical(conn, into_key) == from_key:
        raise Problem(
            f"merging {from_key} into {into_key} would form a cycle: "
            f"{into_key} already merges into {from_key}",
            f"merge in the other direction, or revoke the existing merge first",
            expected="merges that end at one canonical account (D7 5.)",
        )
    into_canonical = canonical(conn, into_key)
    if into_canonical != into_key:
        raise Problem(
            f"account {into_key} is not canonical: it merges into {into_canonical}",
            f"merge into {into_canonical} instead",
            expected="both accounts canonical when the merge is created (D7 5.)",
        )
    if evidence != "user":
        if into_key > from_key:
            raise Problem(
                f"evidence merge into {into_key}, the larger key",
                f"merge {into_key} into {from_key} instead",
                expected="a merge from evidence goes into the smaller of the two keys (D7 5.)",
            )
        revoked = conn.execute(
            "SELECT merge_id FROM account_merges WHERE revoked_at IS NOT NULL"
            " AND ((from_key = ? AND into_key = ?) OR (from_key = ? AND into_key = ?))"
            " AND evidence = ? AND verified_by IS ?",
            (from_key, into_key, into_key, from_key, evidence, verified_by),
        ).fetchone()
        if revoked is not None:
            raise Problem(
                f"merge {revoked[0]} of these accounts was revoked, and this merge rests "
                f"on the same evidence ({verified_by})",
                "merge them only on new evidence, or as the user with evidence 'user'",
                expected="a revoked merge not re-created from its own evidence (D7)",
            )
    cur = conn.execute(
        "INSERT INTO account_merges (from_key, into_key, evidence, verified_by, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (from_key, into_key, evidence, verified_by, _now()),
    )
    return cur.lastrowid


def unmerge(conn: sqlite3.Connection, merge_id: int) -> None:
    """Revoke a merge. Aliases never move; effective attributions that relied
    on it are the caller's to recompute (D7)."""
    row = conn.execute(
        "SELECT revoked_at FROM account_merges WHERE merge_id = ?", (merge_id,)
    ).fetchone()
    if row is None:
        raise Problem(f"no merge {merge_id}",
                      "list merges with: usage-watch accounts",
                      expected="a merge_id from account_merges")
    if row[0] is None:
        conn.execute("UPDATE account_merges SET revoked_at = ? WHERE merge_id = ?",
                     (_now(), merge_id))


def alias_conflicts(conn: sqlite3.Connection) -> list[tuple[str, str, list[str]]]:
    """Aliases whose unrevoked `reported` rows resolve to more than one canonical
    account (D7): (alias_kind, alias_hash, sorted canonical account_keys)."""
    aliases = conn.execute(
        "SELECT DISTINCT alias_kind, alias_hash FROM account_aliases"
        " WHERE evidence = 'reported' AND revoked_at IS NULL ORDER BY alias_kind, alias_hash"
    ).fetchall()
    out = []
    for kind, h in aliases:
        keys = _reported_accounts(conn, kind, h)
        if len(keys) > 1:
            out.append((kind, h, sorted(keys)))
    return out
