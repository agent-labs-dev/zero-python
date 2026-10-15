"""Community Python server SDK for Rocicorp Zero. Not affiliated with Rocicorp.

The SQLAlchemy transaction provider lives in `zero_server.server.adapters.sqlalchemy` and needs the
`zero-server[sqlalchemy]` extra.
"""

from zero_server.mutators import define_mutator, define_mutators, get_mutator, must_get_mutator
from zero_server.protocol.application_error import ApplicationError
from zero_server.query.builder import Query, create_builder
from zero_server.query.registry import QueryParseError, define_queries, define_query, get_query, must_get_query
from zero_server.schema import Schema, load_schema
from zero_server.server.process_mutations import handle_mutate_request
from zero_server.server.process_queries import handle_query_request
from zero_server.server.push_processor import PushProcessor

__version__ = "0.0.2"

__all__ = [
    "ApplicationError",
    "PushProcessor",
    "Query",
    "QueryParseError",
    "Schema",
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
]
