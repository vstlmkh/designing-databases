-- Legacy OLTP schema (PostgreSQL), pre-audit. Loads cleanly; every defect is deliberate.
CREATE TABLE customers (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    email character varying(255),
    company_id integer,
    created_at timestamp without time zone,
    metadata jsonb
);

CREATE TABLE orders (
    id integer NOT NULL PRIMARY KEY,
    customer_id uuid REFERENCES customers(id),
    total double precision,
    status character varying(20),
    deleted_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE order_items (
    order_id integer NOT NULL REFERENCES orders(id),
    item_id bigint NOT NULL,
    unit_price numeric(10,2) NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (order_id, item_id)
);

-- Child FK column with no index at all.
CREATE TABLE payments (
    id bigserial PRIMARY KEY,
    order_id integer NOT NULL REFERENCES orders(id),
    amount double precision NOT NULL,
    paid_at timestamp without time zone
);

CREATE TABLE product_attributes (
    id bigserial PRIMARY KEY,
    entity_id bigint NOT NULL,
    attribute_name text NOT NULL,
    value text
);

CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE INDEX idx_orders_customer_status ON orders (customer_id, status);
CREATE INDEX idx_customers_email ON customers (email);
