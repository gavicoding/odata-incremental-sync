"""Incremental sync and key-only reconciliation for OData APIs."""

from .checkpoint import CheckpointStore, JsonFileCheckpoint, MemoryCheckpoint, SqlCheckpoint, SqliteCheckpoint
from .client import ODataClient
from .errors import AuthError, ODataError, RetryExhausted
from .incremental import SyncResult, sync_incremental
from .reconcile import DayReport, ReconcileReport, reconcile_by_keys
from .sinks import MySQLSink, PostgresSink, SqliteSink, SqlSink

__version__ = "1.0.0"

__all__ = [
    "AuthError",
    "CheckpointStore",
    "DayReport",
    "JsonFileCheckpoint",
    "MemoryCheckpoint",
    "MySQLSink",
    "ODataClient",
    "ODataError",
    "PostgresSink",
    "ReconcileReport",
    "RetryExhausted",
    "SqlCheckpoint",
    "SqlSink",
    "SqliteCheckpoint",
    "SqliteSink",
    "SyncResult",
    "__version__",
    "reconcile_by_keys",
    "sync_incremental",
]
