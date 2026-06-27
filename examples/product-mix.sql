-- product-mix: units and revenue per product, with a running revenue share.
-- A slightly meatier query to show quackpack happily stores real SQL.
-- Run it:  quackpack run product-mix --file examples/sales.csv
SELECT product,
       sum(units)                                   AS units,
       sum(amount)                                  AS revenue,
       round(100.0 * sum(amount)
             / sum(sum(amount)) OVER (), 1)         AS pct_of_revenue
FROM sales
GROUP BY product
ORDER BY revenue DESC;
