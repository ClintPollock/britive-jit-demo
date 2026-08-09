-- ============================================================================
-- britive-jit-demo — RDS MySQL demo seed
-- ----------------------------------------------------------------------------
-- Target: britive-pov-mysql.<...>.rds.amazonaws.com / database `demo`
-- Run with EPHEMERAL credentials checked out from Britive:
--     Resources/AWS-RDS-MySQL-Demo/MySQL DBA
-- i.e. the demo's own setup never uses a stored master password.
--
-- Idempotent: drops & recreates every table, so re-running gives a clean state.
-- ============================================================================

SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS shipments;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS employees;
DROP TABLE IF EXISTS customers;
SET FOREIGN_KEY_CHECKS = 1;

-- ── Dimension: customers ────────────────────────────────────────────────────
CREATE TABLE customers (
  id          INT PRIMARY KEY,
  name        VARCHAR(80)  NOT NULL,
  email       VARCHAR(120) NOT NULL,
  region      VARCHAR(20)  NOT NULL,
  signup_date DATE         NOT NULL
);

INSERT INTO customers (id, name, email, region, signup_date) VALUES
  (1,  'Alice Chen',     'alice@example.com',    'us-west',     '2025-01-15'),
  (2,  'Bob Mendez',     'bob@example.com',      'us-east',     '2025-02-03'),
  (3,  'Cara Singh',     'cara@example.com',     'eu-west',     '2025-02-21'),
  (4,  'Diego Park',     'diego@example.com',    'ap-south',    '2025-03-10'),
  (5,  'Eva Larsen',     'eva@example.com',      'eu-north',    '2025-03-28'),
  (6,  'Frank Osei',     'frank@example.com',    'us-east',     '2024-11-12'),
  (7,  'Grace Kim',      'grace@example.com',    'ap-northeast','2024-12-01'),
  (8,  'Hassan Ali',     'hassan@example.com',   'eu-west',     '2025-01-08'),
  (9,  'Ines Costa',     'ines@example.com',     'sa-east',     '2025-02-14'),
  (10, 'Jamal Brooks',   'jamal@example.com',    'us-west',     '2025-03-02'),
  (11, 'Kira Volkov',    'kira@example.com',     'eu-north',    '2024-10-19'),
  (12, 'Liam Murphy',    'liam@example.com',     'us-east',     '2024-09-30'),
  (13, 'Mei Tanaka',     'mei@example.com',      'ap-northeast','2025-01-22'),
  (14, 'Noah Bauer',     'noah@example.com',     'eu-west',     '2025-02-27'),
  (15, 'Olivia Reyes',   'olivia@example.com',   'us-west',     '2024-12-15'),
  (16, 'Pavel Novak',    'pavel@example.com',    'eu-north',    '2025-03-19'),
  (17, 'Quinn Adebayo',  'quinn@example.com',    'us-east',     '2024-11-27'),
  (18, 'Rosa Iglesias',  'rosa@example.com',     'sa-east',     '2025-01-31'),
  (19, 'Sven Eriksson',  'sven@example.com',     'eu-north',    '2024-10-05'),
  (20, 'Tara Nair',      'tara@example.com',     'ap-south',    '2025-02-09'),
  (21, 'Umar Farooq',    'umar@example.com',     'ap-south',    '2025-03-25'),
  (22, 'Vera Lindqvist', 'vera@example.com',     'eu-north',    '2024-12-22'),
  (23, 'Will Hughes',    'will@example.com',     'us-west',     '2025-01-11'),
  (24, 'Xena Popov',     'xena@example.com',     'eu-west',     '2025-02-18'),
  (25, 'Yusuf Demir',    'yusuf@example.com',    'eu-west',     '2024-11-03'),
  (26, 'Zoe Martin',     'zoe@example.com',      'us-east',     '2025-03-07'),
  (27, 'Aaron Webb',     'aaron@example.com',    'us-west',     '2024-09-14'),
  (28, 'Bella Rossi',    'bella@example.com',    'eu-west',     '2025-01-26'),
  (29, 'Caleb Stone',    'caleb@example.com',    'us-east',     '2025-02-05'),
  (30, 'Dina Haddad',    'dina@example.com',     'eu-west',     '2024-12-09'),
  (31, 'Elias Berg',     'elias@example.com',    'eu-north',    '2025-03-14'),
  (32, 'Farah Khan',     'farah@example.com',    'ap-south',    '2025-01-19'),
  (33, 'Gabe Torres',    'gabe@example.com',     'sa-east',     '2024-10-28'),
  (34, 'Hana Suzuki',    'hana@example.com',     'ap-northeast','2025-02-23'),
  (35, 'Igor Petrov',    'igor@example.com',     'eu-north',    '2024-11-16'),
  (36, 'Julia Santos',   'julia@example.com',    'sa-east',     '2025-03-21'),
  (37, 'Karl Schmidt',   'karl@example.com',     'eu-west',     '2025-01-04'),
  (38, 'Lena Fischer',   'lena@example.com',     'eu-west',     '2024-12-30'),
  (39, 'Marco Bellini',  'marco@example.com',    'eu-west',     '2025-02-12'),
  (40, 'Nadia Aziz',     'nadia@example.com',    'ap-south',    '2025-03-30');

-- ── Dimension: products (with on-hand inventory) ────────────────────────────
CREATE TABLE products (
  sku          VARCHAR(12) PRIMARY KEY,
  product_name VARCHAR(80) NOT NULL,
  category     VARCHAR(30) NOT NULL,
  unit_price   DECIMAL(10,2) NOT NULL,
  qty_on_hand  INT NOT NULL,
  warehouse    VARCHAR(12) NOT NULL
);

INSERT INTO products (sku, product_name, category, unit_price, qty_on_hand, warehouse) VALUES
  ('SKU-1001', 'Aurora Wireless Headset',    'Electronics', 129.99, 420, 'WH-WEST'),
  ('SKU-1002', 'Nimbus Mechanical Keyboard', 'Electronics',  89.50, 280, 'WH-WEST'),
  ('SKU-1003', 'Vertex 27in 4K Monitor',     'Electronics', 349.00,  64, 'WH-EAST'),
  ('SKU-1004', 'Pulse USB-C Hub',            'Electronics',  39.99, 510, 'WH-EAST'),
  ('SKU-1005', 'Drift Ergonomic Mouse',      'Electronics',  44.95, 195, 'WH-WEST'),
  ('SKU-1006', 'Cobalt Laptop Sleeve 15in',  'Office',       24.99, 730, 'WH-EU'),
  ('SKU-1007', 'Atlas Standing Desk',        'Office',      459.00,  22, 'WH-EAST'),
  ('SKU-1008', 'Halo LED Desk Lamp',         'Office',       32.50, 340, 'WH-EU'),
  ('SKU-1009', 'Quill Notebook 3-Pack',      'Office',       12.99, 980, 'WH-EU'),
  ('SKU-1010', 'Terra Insulated Bottle 1L',  'Outdoor',      27.00, 605, 'WH-WEST'),
  ('SKU-1011', 'Summit Trail Backpack 30L',  'Outdoor',      98.00,  88, 'WH-WEST'),
  ('SKU-1012', 'Ridge Down Jacket',          'Apparel',     179.00,  47, 'WH-EU'),
  ('SKU-1013', 'Coast Merino Beanie',        'Apparel',      28.00, 410, 'WH-EU'),
  ('SKU-1014', 'Stride Running Shoe',        'Apparel',     119.99, 156, 'WH-EAST'),
  ('SKU-1015', 'Pace GPS Watch',             'Electronics', 219.00,  73, 'WH-EAST'),
  ('SKU-1016', 'Ember Smart Mug',            'Home',         99.95,  18, 'WH-WEST'),
  ('SKU-1017', 'Lumen Smart Bulb 4-Pack',    'Home',         49.99, 265, 'WH-EAST'),
  ('SKU-1018', 'Haven Air Purifier',         'Home',        189.00,  31, 'WH-EAST'),
  ('SKU-1019', 'Cove Bluetooth Speaker',     'Electronics',  69.99, 302, 'WH-WEST'),
  ('SKU-1020', 'Flux Fast Charger 65W',      'Electronics',  34.99, 588, 'WH-WEST'),
  ('SKU-1021', 'Grove Bamboo Cutting Board', 'Home',         29.95, 224, 'WH-EU'),
  ('SKU-1022', 'Meridian Chef Knife 8in',    'Home',         74.00,  96, 'WH-EU'),
  ('SKU-1023', 'Orbit Drone Mini',           'Electronics', 299.00,  12, 'WH-EAST'),
  ('SKU-1024', 'Beacon Headlamp 400lm',      'Outdoor',      38.50, 177, 'WH-WEST'),
  ('SKU-1025', 'Canyon Camp Stove',          'Outdoor',     112.00,  29, 'WH-WEST');

-- ── Dimension: employees (SENSITIVE — salary column drives Act 2 approval) ───
CREATE TABLE employees (
  emp_id     INT PRIMARY KEY,
  name       VARCHAR(80) NOT NULL,
  department VARCHAR(30) NOT NULL,
  role       VARCHAR(50) NOT NULL,
  salary     DECIMAL(10,2) NOT NULL,
  hire_date  DATE NOT NULL
);

INSERT INTO employees (emp_id, name, department, role, salary, hire_date) VALUES
  (101, 'Priya Raman',     'Engineering', 'Staff Engineer',        178000, '2021-06-14'),
  (102, 'Tom Becker',      'Engineering', 'Senior Engineer',       152000, '2022-02-28'),
  (103, 'Sara Lindholm',   'Engineering', 'Engineer',              128000, '2023-08-07'),
  (104, 'Derek Vance',     'Sales',       'Account Executive',      98000, '2022-11-03'),
  (105, 'Monica Flores',   'Sales',       'Sales Director',        165000, '2020-04-19'),
  (106, 'Owen Pratt',      'Sales',       'SDR',                    62000, '2024-01-22'),
  (107, 'Lily Zhang',      'Finance',     'Controller',            142000, '2021-09-30'),
  (108, 'Raj Kapoor',      'Finance',     'Financial Analyst',      95000, '2023-03-15'),
  (109, 'Hannah Wolfe',    'HR',          'HR Business Partner',    104000, '2022-07-11'),
  (110, 'Greg Munoz',      'HR',          'Recruiter',              78000, '2023-10-02'),
  (111, 'Aisha Bello',     'Warehouse',   'Warehouse Manager',      89000, '2021-12-06'),
  (112, 'Carl Eriksen',    'Warehouse',   'Fulfillment Lead',       64000, '2023-05-29'),
  (113, 'Donna Pierce',    'Warehouse',   'Shipping Clerk',         52000, '2024-02-18'),
  (114, 'Felix Nguyen',    'Engineering', 'Engineering Manager',   188000, '2019-10-21'),
  (115, 'Gina Marlowe',    'Marketing',   'Marketing Manager',     121000, '2022-03-08'),
  (116, 'Hugo Silva',      'Marketing',   'Content Strategist',     86000, '2023-11-27'),
  (117, 'Iris Chen',       'Finance',     'CFO',                   245000, '2018-05-14'),
  (118, 'Jorge Ramirez',   'Sales',       'Account Executive',      96000, '2023-01-09'),
  (119, 'Katie Donovan',   'HR',          'Chief People Officer',  198000, '2019-02-25'),
  (120, 'Leo Fitzgerald',  'Warehouse',   'Receiving Clerk',        49000, '2024-04-01');

-- ── Fact: orders (generated, references customers + products) ────────────────
CREATE TABLE orders (
  order_id    INT PRIMARY KEY,
  customer_id INT NOT NULL,
  sku         VARCHAR(12) NOT NULL,
  qty         INT NOT NULL,
  amount      DECIMAL(10,2) NOT NULL,
  order_date  DATE NOT NULL,
  status      VARCHAR(12) NOT NULL,
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id),
  CONSTRAINT fk_orders_product  FOREIGN KEY (sku)         REFERENCES products(sku)
);

INSERT INTO orders (order_id, customer_id, sku, qty, amount, order_date, status)
WITH RECURSIVE seq(n) AS (
  SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 180
)
SELECT
  n,
  ((n * 7) % 40) + 1,
  p.sku,
  (n % 4) + 1,
  ((n % 4) + 1) * p.unit_price,
  DATE_ADD('2025-01-01', INTERVAL (n * 73) % 140 DAY),
  -- status keyed off n%7 (coprime to the 40-customer cycle) so a customer's
  -- orders span multiple statuses instead of all landing on one.
  ELT((n % 7) + 1, 'DELIVERED', 'DELIVERED', 'SHIPPED', 'DELIVERED', 'PLACED', 'SHIPPED', 'CANCELLED')
FROM seq
JOIN products p ON p.sku = CONCAT('SKU-', LPAD(1001 + ((n * 3) % 25), 4, '0'));

-- ── Fact: shipments (generated for non-PLACED/non-CANCELLED orders) ──────────
CREATE TABLE shipments (
  shipment_id    INT PRIMARY KEY,
  order_id       INT NOT NULL,
  carrier        VARCHAR(12) NOT NULL,
  tracking_no    VARCHAR(24) NOT NULL,
  shipped_date   DATE NOT NULL,
  delivered_date DATE NULL,
  status         VARCHAR(12) NOT NULL,
  CONSTRAINT fk_ship_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

INSERT INTO shipments (shipment_id, order_id, carrier, tracking_no, shipped_date, delivered_date, status)
SELECT
  o.order_id,
  o.order_id,
  ELT((o.order_id % 3) + 1, 'UPS', 'FedEx', 'DHL'),
  CONCAT('1Z', LPAD(o.order_id, 6, '0'), 'X', LPAD((o.order_id * 17) % 1000, 3, '0')),
  DATE_ADD(o.order_date, INTERVAL 1 DAY),
  CASE WHEN o.status = 'DELIVERED'
       THEN DATE_ADD(o.order_date, INTERVAL ((o.order_id % 5) + 3) DAY)
       ELSE NULL END,
  CASE WHEN o.status = 'DELIVERED' THEN 'DELIVERED' ELSE 'IN_TRANSIT' END
FROM orders o
WHERE o.status IN ('SHIPPED', 'DELIVERED');

-- ── Quick sanity summary ────────────────────────────────────────────────────
SELECT 'customers' AS tbl, COUNT(*) AS rows_count FROM customers
UNION ALL SELECT 'products',  COUNT(*) FROM products
UNION ALL SELECT 'employees', COUNT(*) FROM employees
UNION ALL SELECT 'orders',    COUNT(*) FROM orders
UNION ALL SELECT 'shipments', COUNT(*) FROM shipments;
