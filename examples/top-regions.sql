-- top-regions: total revenue by region, biggest first.
-- Run it:  quackpack run top-regions --file examples/sales.csv
SELECT region,
       sum(amount) AS revenue,
       sum(units)  AS units
FROM sales
GROUP BY region
ORDER BY revenue DESC;
