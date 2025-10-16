FROM timescale/timescaledb:latest-pg16

# Copy schema initialization script
COPY schema.sql /docker-entrypoint-initdb.d/01-init-schema.sql

# Scripts in /docker-entrypoint-initdb.d/ run automatically
# only if the database is empty (first initialization)
# TimescaleDB is already installed and configured in this image
