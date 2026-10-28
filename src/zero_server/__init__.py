"""Community Python server SDK for Rocicorp Zero. Not affiliated with Rocicorp.

The SQLAlchemy transaction provider lives in `zero_server.server.adapters.sqlalchemy` and needs the
`zero-server[sqlalchemy]` extra.
"""

from zero_server.mutators import define_mutator, define_mutators, get_mutator, must_get_mutator
from zero_server.protocol.application_error import ApplicationError
from zero_server.protocol.ast import AST
from zero_server.protocol.error import ErrorReason, ServerPushFailed
from zero_server.protocol.mutate_server import MutateSuccess
from zero_server.protocol.mutation import (
    CustomMutation,
    MutationAppError,
    MutationOk,
    MutationResponse,
    MutationZeroError,
)
from zero_server.protocol.push import PushBody
from zero_server.query.builder import Builder, OneQuery, Query, create_builder
from zero_server.query.expression import ExpressionBuilder
from zero_server.query.registry import QueryParseError, define_queries, define_query, get_query, must_get_query
from zero_server.schema import Schema, load_schema
from zero_server.server.process_mutations import (
    MutateResponse,
    OutOfOrderMutation,
    TransactFn,
    handle_mutate_request,
)
from zero_server.server.process_queries import QueryRequestHandler, handle_query_request, server_ast
from zero_server.server.push_processor import PushProcessor
from zero_server.server.transaction import RetryPredicate
from zero_server.server.transaction_options import IsolationLevel
from zero_server.shared.json import JSONValue

__version__ = "0.0.5"

__all__ = [
    "AST",
    "ApplicationError",
    "Builder",
    "CustomMutation",
    "ErrorReason",
    "ExpressionBuilder",
    "IsolationLevel",
    "JSONValue",
    "MutateResponse",
    "MutateSuccess",
    "MutationAppError",
    "MutationOk",
    "MutationResponse",
    "MutationZeroError",
    "OneQuery",
    "OutOfOrderMutation",
    "PushBody",
    "PushProcessor",
    "Query",
    "QueryParseError",
    "QueryRequestHandler",
    "RetryPredicate",
    "Schema",
    "ServerPushFailed",
    "TransactFn",
    "create_builder",
    "define_mutator",
    "define_mutators",
    "define_queries",
    "define_query",
    "get_mutator",
    "get_query",
    "handle_mutate_request",
    "handle_query_request",
    "load_schema",
    "must_get_mutator",
    "must_get_query",
    "server_ast",
]
