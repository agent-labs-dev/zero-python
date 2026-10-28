"""Upstream's valita schemas for the requests zero-cache sends, which give a refused request its message.

Ported from rocicorp/mono `packages/shared/src/json-schema.ts`, `packages/zero-protocol/src/data.ts`,
`packages/zero-protocol/src/primary-key.ts`, `packages/zero-protocol/src/mutation.ts`,
`packages/zero-protocol/src/push.ts`,
`packages/zero-protocol/src/mutate-server.ts` and `packages/zero-protocol/src/custom-queries.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.

The Pydantic models beside them decide what parses; `tests/wire/test_requests.py` holds the two to the same
answer. These only run once a model has refused a value.
"""

from zero_server.shared import valita as v
from zero_server.shared.valita import Mode

# Upstream's `jsonSchema` also checks the value is JSON, and its `valueSchema` allows `undefined` besides,
# neither of which a value decoded from JSON can fail.
json_schema = v.unknown
row_schema = v.record(v.unknown)

primary_key_schema = v.tuple_([v.string]).concat(v.array(v.string))
primary_key_value_schema = v.union(v.string, v.number, v.boolean)
primary_key_value_record_schema = v.record(primary_key_value_schema)

cleanup_results_arg_schema = v.union(
    v.object_({"clientGroupID": v.string, "clientID": v.string, "upToMutationID": v.number}),
    v.object_(
        {"type": v.literal("single"), "clientGroupID": v.string, "clientID": v.string, "upToMutationID": v.number}
    ),
    v.object_(
        {
            "type": v.literal("bulk"),
            "clientGroupID": v.string,
            "clientIDs": v.tuple_([v.string]).concat(v.array(v.string)),
        }
    ),
)


def _op_schema(op: str, value: v.Type) -> v.Type:
    return v.object_({"op": v.literal(op), "tableName": v.string, "primaryKey": primary_key_schema, "value": value})


crud_op_schema = v.union(
    _op_schema("insert", row_schema),
    _op_schema("upsert", row_schema),
    _op_schema("update", row_schema),
    _op_schema("delete", primary_key_value_record_schema),
)
crud_arg_schema = v.object_({"ops": v.array(crud_op_schema)})

crud_mutation_schema = v.object_(
    {
        "type": v.literal("crud"),
        "id": v.number,
        "clientID": v.string,
        "name": v.literal("_zero_crud"),
        "args": v.tuple_([crud_arg_schema]),
        "timestamp": v.number,
    }
)
custom_mutation_schema = v.object_(
    {
        "type": v.literal("custom"),
        "id": v.number,
        "clientID": v.string,
        "name": v.string,
        "args": v.array(json_schema),
        "timestamp": v.number,
    }
)
mutation_schema = v.union(crud_mutation_schema, custom_mutation_schema)

push_body_schema = v.object_(
    {
        "clientGroupID": v.string,
        "mutations": v.array(mutation_schema),
        "pushVersion": v.number,
        "schemaVersion": v.number.optional(),
        "timestamp": v.number,
        "requestID": v.string,
        "auth": v.string.optional(),
        "traceparent": v.string.optional(),
    }
)

mutate_params_schema = v.object_({"schema": v.string, "appID": v.string})

transform_request_body_schema = v.array(v.object_({"id": v.string, "name": v.string, "args": v.array(json_schema)}))
transform_request_message_schema = v.tuple_([v.literal("transform"), transform_request_body_schema])


def refusal(value: object, schema: v.Type, mode: Mode | None, fallback: str) -> str:
    """The message upstream's `v.parse(value, schema, mode)` throws.

    `fallback`, the Pydantic error, stands in should these accept a value the model refused, which
    `tests/wire/test_requests.py` checks never happens for JSON.
    """
    message = v.parse_error(value, schema, mode)
    return fallback if message is None else message
