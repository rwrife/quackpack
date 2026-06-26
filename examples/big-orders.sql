-- big-orders: every order at or above a revenue floor you pass in.
-- The :min placeholder is bound at run time (and prompted for if omitted).
-- Run it:  quackpack run big-orders --file examples/sales.csv --param min=300
SELECT order_date,
       region,
       product,
       amount
FROM sales
WHERE amount >= :min
ORDER BY amount DESC;
