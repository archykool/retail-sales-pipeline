-- Initialize PostgreSQL with separate dev and prod databases.
-- This script runs only on first container creation.
-- To re-run: docker compose down -v, then docker compose up -d

CREATE DATABASE sales_dev OWNER postgres;
CREATE DATABASE sales_prod OWNER postgres;
