FROM mysql:8.4.0 AS mysql-client
FROM postgres@sha256:714b08dd70c05160db032e4321033ac4e5b6328935b2d03ed419c3a898aa8219 AS postgres16-client
FROM postgres@sha256:b939b3851e2cccb017dc4497af63b15e34efa57fba036548773c53b2f16a8871 AS postgres-client
FROM wordpress:cli-2.12.0-php8.2 AS wordpress-cli

FROM python:3.11-slim-bookworm

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PATH="/usr/local/lib/postgresql/18/bin:${PATH}"

# Install only the runtime libraries required by the version-pinned clients.
RUN apt-get update && apt-get install -y \
    gcc \
    git \
    libncurses6 \
    libpq5 \
    libssl3 \
    libstdc++6 \
    libtinfo6 \
    mariadb-client-core \
    php-cli \
    php-mysql \
    util-linux \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mysql-client /usr/bin/mysql /usr/local/bin/mysql
COPY --from=mysql-client /usr/bin/mysqldump /usr/local/bin/mysqldump
COPY --from=postgres16-client /usr/lib/postgresql/16/bin/ /usr/local/lib/postgresql/16/bin/
COPY --from=postgres-client /usr/lib/postgresql/18/bin/ /usr/local/lib/postgresql/18/bin/
COPY --from=postgres-client /usr/lib/x86_64-linux-gnu/libpq.so.5.18 /usr/local/lib/libpq.so.5.18
COPY --from=wordpress-cli /usr/local/bin/wp /usr/local/bin/wp
RUN ln -s /usr/bin/mariadb-check /usr/local/bin/mysqlcheck \
    && ln -s /usr/local/lib/libpq.so.5.18 /usr/local/lib/libpq.so.5 \
    && ldconfig

# Copy dependency files
COPY pyproject.toml ./

# Install Python dependencies
RUN pip install --no-cache-dir -e .

# Copy application code
COPY app/ ./app/
# Copy migrations directory
COPY migrations/ ./migrations/

# Create non-root user
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=5).close()" || exit 1

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
