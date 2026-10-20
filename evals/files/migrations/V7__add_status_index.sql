ALTER TABLE orders ADD COLUMN status_code varchar(20) DEFAULT 'new';
ALTER TABLE orders ALTER COLUMN status_code SET NOT NULL;
CREATE INDEX idx_orders_status_code ON orders (status_code);
ALTER TABLE orders ADD CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) REFERENCES customers(id);
UPDATE orders SET status_code = status;
