FROM mysql@sha256:3e5649c69e6d75cf88fc6f8f39f877453faa4e5167b5e648007e45f54bb17f6b AS mysql-client
FROM postgres@sha256:714b08dd70c05160db032e4321033ac4e5b6328935b2d03ed419c3a898aa8219 AS postgres16-client
FROM postgres@sha256:b939b3851e2cccb017dc4497af63b15e34efa57fba036548773c53b2f16a8871 AS postgres-client
FROM wordpress:cli-2.12.0-php8.2 AS wordpress-cli

FROM python@sha256:bb3a5d38989ec658710f06b08bc23cb78d079eb852405e42b124fdf430281454

ADD --checksum=sha256:5e9576a3e65d1f21d6879882e5c4e73b63b3ac49b6356a171b68b0be7f342621 \
    https://repo.mysql.com/apt/debian/pool/mysql-8.4-lts/m/mysql-shell/mysql-shell_8.4.0-1debian12_amd64.deb \
    /tmp/mysql-shell.deb

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
    locales \
    libpq5 \
    libssl3 \
    libstdc++6 \
    libtinfo6 \
    mariadb-client-core \
    php-cli \
    php-mysql \
    util-linux \
    zlib1g \
    /tmp/mysql-shell.deb \
    && localedef -i en_US -f UTF-8 en_US.UTF-8 \
    && test "$(dpkg-query -W -f='${Version} ${Architecture}' mysql-shell)" = \
      "8.4.0-1debian12 amd64" \
    && mysqlsh --version | grep -F '8.4.0' \
    && rm -f /tmp/mysql-shell.deb \
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
