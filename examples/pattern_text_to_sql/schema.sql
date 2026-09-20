CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ordered_at TEXT NOT NULL,
    status TEXT NOT NULL,
    total_amount REAL NOT NULL
);

INSERT INTO customers (id, name, region, created_at) VALUES
    (1, 'Acme Labs', 'North', '2025-01-12'),
    (2, 'Globex', 'South', '2025-02-03'),
    (3, 'Initech', 'North', '2025-03-18'),
    (4, 'Umbrella Retail', 'West', '2025-04-21');

INSERT INTO orders (id, customer_id, ordered_at, status, total_amount) VALUES
    (101, 1, '2026-01-05', 'completed', 1250.00),
    (102, 1, '2026-02-11', 'completed', 830.50),
    (103, 2, '2026-02-18', 'cancelled', 410.00),
    (104, 3, '2026-03-02', 'completed', 2200.00),
    (105, 4, '2026-03-15', 'pending', 725.25),
    (106, 3, '2026-04-09', 'completed', 560.00);

