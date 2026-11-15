"""validate_generated_sql enforces required and default filters structurally, failing closed."""

from __future__ import annotations

import pytest

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.semantics.contracts import SemanticLayerDefinition
from algen_agent_runtime.semantics.layer import SemanticLayer


@pytest.fixture
def layer() -> SemanticLayer:
    definition = SemanticLayerDefinition.model_validate(
        {
            "name": "s",
            "version": "1",
            "description": "d",
            "models": [
                {
                    "name": "orders",
                    "description": "o",
                    "table": "sales.orders",
                    "primary_key": ["id"],
                    "dimensions": [
                        {
                            "name": "tenant_id",
                            "label": "t",
                            "description": "t",
                            "expression": "tenant_id",
                            "type": "string",
                        },
                        {
                            "name": "is_deleted",
                            "label": "d",
                            "description": "d",
                            "expression": "is_deleted",
                            "type": "boolean",
                        },
                    ],
                    "metrics": [
                        {
                            "name": "revenue",
                            "label": "r",
                            "description": "r",
                            "expression": "amount",
                            "aggregation": "sum",
                            "certification": "certified",
                        }
                    ],
                    "required_filter_dimensions": ["tenant_id"],
                    "default_filters": [{"field": "is_deleted", "operator": "eq", "value": False}],
                }
            ],
        }
    )
    return SemanticLayer(definition)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sum(amount) FROM sales.orders WHERE tenant_id = 'acme' AND is_deleted = false",
        'SELECT sum(amount) FROM "sales"."orders" WHERE "tenant_id" = $1 AND is_deleted IS FALSE;',
        "SELECT sum(o.amount) FROM sales.orders AS o WHERE o.tenant_id = :tenant "
        "AND o.is_deleted = false GROUP BY 1 ORDER BY 1 LIMIT 10",
        "SELECT sum(amount) FROM sales.orders o WHERE (o.tenant_id IN ('a', 'b') "
        "AND o.is_deleted = false AND (amount > 1 OR amount < 0))",
        "SELECT sum(amount) FROM sales.orders WHERE tenant_id BETWEEN ? AND ? "
        "AND is_deleted = false AND created_at >= current_date - interval '7 days'",
        "SELECT sum(amount) FROM sales.orders WHERE tenant_id = 'it''s' AND is_deleted = false",
        "SELECT sum(amount) FROM sales.orders WHERE tenant_id = $1::text AND is_deleted = false",
    ],
)
def test_governed_queries_are_accepted(layer: SemanticLayer, sql: str) -> None:
    layer.validate_generated_sql(("revenue",), (), sql)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        # Predicates inside comments are not predicates.
        (
            "SELECT sum(amount) FROM sales.orders /* WHERE tenant_id = 'a' AND is_deleted = false */",
            "comments",
        ),
        (
            "SELECT sum(amount) FROM sales.orders WHERE is_deleted = false -- AND tenant_id = 'a'",
            "comments",
        ),
        # Tautologies.
        (
            "SELECT sum(amount) FROM sales.orders WHERE tenant_id = tenant_id AND is_deleted = false",
            "predicate on 'tenant_id'",
        ),
        (
            "SELECT sum(amount) FROM sales.orders WHERE tenant_id = 'a' OR true "
            "AND is_deleted = false",
            "OR is not allowed",
        ),
        (
            "SELECT sum(amount) FROM sales.orders WHERE (tenant_id = 'a' OR 1 = 1) "
            "AND is_deleted = false",
            "predicate on 'tenant_id'",
        ),
        # A predicate only inside a string literal or subquery.
        (
            "SELECT sum(amount) FROM sales.orders WHERE amount > 0 AND "
            "'tenant_id = 1' = 'tenant_id = 1' AND is_deleted = false",
            "predicate on 'tenant_id'",
        ),
        (
            "SELECT sum(amount) FROM (SELECT * FROM sales.orders WHERE tenant_id = 'a' "
            "AND is_deleted = false) x",
            "not a subquery",
        ),
        (
            "SELECT sum(amount) FROM sales.orders WHERE tenant_id = 'a' AND is_deleted = false "
            "UNION ALL SELECT sum(amount) FROM sales.orders",
            "exactly once",
        ),
        (
            "SELECT sum(amount) FROM sales.orders WHERE tenant_id = 'a' AND is_deleted = false "
            "UNION ALL SELECT 1",
            "set operations",
        ),
        (
            "SELECT sum(o.amount) FROM sales.orders o JOIN tenants t ON true "
            "WHERE t.tenant_id = 'a' AND o.is_deleted = false",
            "predicate on 'tenant_id'",
        ),
        ("SELECT sum(amount) FROM sales.orders", "WHERE"),
        (
            "SELECT sum(amount) FROM sales.orders WHERE tenant_id = 'a' AND is_deleted = false; "
            "SELECT 1",
            "one statement",
        ),
        (
            "SELECT sum(amount) FROM sales.orders WHERE tenant_id = E'\\' AND is_deleted = false'",
            "backslashes",
        ),
        (
            "SELECT sum(amount) FROM sales.orders WHERE tenant_id = $$a$$ AND is_deleted = false",
            "dollar-quoted",
        ),
        (
            "SELECT sum(amount) FROM sales.orders WHERE tenant_id = 'a' AND is_deleted = true",
            "is_deleted=False",
        ),
        ("SELECT sum(amount) FROM sales.orders WHERE tenant_id = 'a'", "is_deleted=False"),
        ("SELECT sum(amount) FROM customers WHERE tenant_id = 'a'", "must reference"),
    ],
)
def test_bypasses_are_refused(layer: SemanticLayer, sql: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        layer.validate_generated_sql(("revenue",), (), sql)
