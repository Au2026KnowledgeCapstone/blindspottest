"""Durable, shared model-call reservations and exact-request response caching.

Reserve before network I/O. Unknown/failed attempts remain charged and cannot be
retried automatically, including after a crash. SQLite transactions serialize
concurrent reservations across processes using the same journal.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager


class ModelBudgetError(RuntimeError):
    pass


class ModelBudget:
    def __init__(self, path, *, max_calls=5, max_tokens=100_000,
                 max_input_tokens=30_000, max_output_tokens=6_000):
        if min(max_calls, max_tokens, max_input_tokens) < 0 or max_output_tokens < 1:
            raise ValueError('model limits must be nonnegative; output limit must be positive')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.max_input_tokens, self.max_output_tokens = max_input_tokens, max_output_tokens
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                key TEXT PRIMARY KEY, model TEXT NOT NULL, status TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL, response TEXT, usage TEXT,
                error_type TEXT)''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def summary(self):
        with self.connect() as db:
            rows = db.execute('SELECT status, reserved_tokens FROM requests').fetchall()
        return {'journal': str(self.path), 'attempted_calls': len(rows),
                'reserved_tokens': sum(r[1] for r in rows),
                'completed_calls': sum(r[0] == 'complete' for r in rows),
                'failed_calls': sum(r[0] == 'failed' for r in rows),
                'pending_calls': sum(r[0] == 'pending' for r in rows),
                'max_calls': self.max_calls, 'max_tokens': self.max_tokens}

    def wrap(self, backend, *, provider='configured'):
        return BudgetedBackend(backend, self, provider)


class BudgetedBackend:
    def __init__(self, backend, budget, provider):
        self.backend, self.budget, self.provider = backend, budget, provider
        self.model = backend.model
        # Built-in adapters pass this cap to the API. Custom adapters must
        # implement the same cap themselves and disable hidden retries.
        backend.max_tokens = budget.max_output_tokens

    def complete_json(self, *, system, user, schema):
        payload = json.dumps({'provider': self.provider, 'adapter': type(self.backend).__name__,
            'model': self.model, 'system': system, 'user': user, 'schema': schema,
            'output_cap': self.budget.max_output_tokens}, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(payload.encode()).hexdigest()
        # Conservative reservation, not an exact tokenizer or billing quote.
        # Count UTF-8 bytes for all supplied text/schema plus framing allowance.
        input_reservation = len(payload.encode()) + 1024
        charge = input_reservation + self.budget.max_output_tokens
        with self.budget.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,response FROM requests WHERE key=?', (key,)).fetchone()
            if row:
                if row[0] == 'complete':
                    return row[1]
                raise ModelBudgetError('matching model request previously failed or was interrupted; '
                    'automatic retry blocked (review the journal before starting a new budget)')
            count, tokens = db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_tokens),0) FROM requests').fetchone()
            if input_reservation > self.budget.max_input_tokens:
                raise ModelBudgetError('model input exceeds per-request reservation limit')
            if count >= self.budget.max_calls or tokens + charge > self.budget.max_tokens:
                raise ModelBudgetError('model call/token budget exhausted; resume the same journal '
                    'with explicitly larger limits to process remaining flows')
            db.execute('INSERT INTO requests(key,model,status,reserved_tokens) VALUES (?,?,?,?)',
                       (key, self.model, 'pending', charge))
        try:
            response = self.backend.complete_json(system=system, user=user, schema=schema)
            if not isinstance(response, str):
                raise ValueError('model returned no JSON text')
            usage = getattr(self.backend, 'last_usage', None)
            with self.budget.connect() as db:
                db.execute("UPDATE requests SET status='complete',response=?,usage=? WHERE key=?",
                           (response, json.dumps(usage), key))
            return response
        except BaseException as exc:
            # No exception messages: authentication errors may contain secrets.
            with self.budget.connect() as db:
                db.execute("UPDATE requests SET status='failed',error_type=? WHERE key=?",
                           (type(exc).__name__, key))
            raise
