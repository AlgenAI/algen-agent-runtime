from __future__ import annotations

from algen_agent_runtime.retrieval.contracts import SourceDocument

TENANT_ID = "text-to-sql-example"


def schema_documents() -> tuple[SourceDocument, ...]:
    """Independent schema objects improve retrieval precision over one large schema dump."""
    shared = {"tenant_id": TENANT_ID, "knowledge_type": "database_schema"}
    return (
        SourceDocument(
            id="table-customers",
            source="catalog://analytics/customers",
            title="customers table",
            text=(
                "Table customers stores one row per customer. Columns: id INTEGER primary key; "
                "name TEXT customer display name; region TEXT sales region; created_at TEXT ISO date. "
                "Join customers.id to orders.customer_id."
            ),
            metadata={**shared, "object_type": "table", "table": "customers"},
        ),
        SourceDocument(
            id="table-orders",
            source="catalog://analytics/orders",
            title="orders table",
            text=(
                "Table orders stores customer orders. Columns: id INTEGER primary key; customer_id "
                "INTEGER foreign key to customers.id; ordered_at TEXT ISO date; status TEXT; "
                "total_amount REAL order revenue. Allowed status values are completed, pending, "
                "and cancelled. Completed orders means orders.status = 'completed'."
            ),
            metadata={**shared, "object_type": "table", "table": "orders"},
        ),
        SourceDocument(
            id="metric-completed-revenue",
            source="catalog://analytics/metrics/completed-revenue",
            title="completed revenue metric",
            text=(
                "Completed revenue is SUM(orders.total_amount) filtered to "
                "orders.status = 'completed'. Join orders.customer_id = customers.id when grouping "
                "by customer name or region. Do not include pending or cancelled orders."
            ),
            metadata={**shared, "object_type": "metric", "metric": "completed_revenue"},
        ),
        SourceDocument(
            id="sql-dialect",
            source="catalog://analytics/sql-dialect",
            title="analytics SQL rules",
            text=(
                "The example query engine uses SQLite SQL. Generate exactly one SELECT or WITH "
                "statement. Use explicit columns, named parameters where useful, and never use DDL, "
                "DML, PRAGMA, ATTACH, or multiple statements. Query results are capped at 200 rows."
            ),
            metadata={**shared, "object_type": "policy"},
        ),
        SourceDocument(
            id="example-revenue-by-customer",
            source="catalog://analytics/examples/revenue-by-customer",
            title="revenue by customer example",
            text=(
                "Question pattern: completed revenue by customer, highest first. SQL pattern: "
                "SELECT c.name, SUM(o.total_amount) AS total_revenue FROM customers c JOIN orders o "
                "ON o.customer_id = c.id WHERE o.status = 'completed' GROUP BY c.id, c.name "
                "ORDER BY total_revenue DESC."
            ),
            metadata={**shared, "object_type": "example"},
        ),
    )
