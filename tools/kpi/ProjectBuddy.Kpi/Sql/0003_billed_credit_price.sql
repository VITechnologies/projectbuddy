-- kpi_v1 billed credit price (#371): GitHub's billing usage report prices a Copilot AI credit at 0.01 USD gross
-- (grossAmount / quantity), and the account quota's used-plus-overage counter tracks the billed quantity, so one AIU is
-- one AI credit. 0001 seeded usd_per_unit at 0.04, which overstated every AI cost fourfold.

UPDATE kpi_v1.settings SET value = 0.01 WHERE key = 'usd_per_unit';
