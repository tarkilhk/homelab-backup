"""Bounded PostgreSQL client boundary shared by database-backed plugins."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Literal, Mapping, TypeAlias, cast

from app.core.plugins.artifacts import (
    ArtifactPublicationResult,
    PendingBackupArtifact,
    PrevalidatedArtifactPublication,
    complete_prevalidated_artifact_publication,
    prepare_prevalidated_artifact_publication,
    publish_prevalidated_backup_artifact,
)
from app.core.plugins.base import BackupContext, BackupPlugin
from app.core.subprocesses import run_process_with_timeout

CONNECT_TIMEOUT_SECONDS = 30.0
MAX_PROBE_BYTES = 256 * 1024
BACKUP_TIMEOUT_SECONDS = 3600.0
PUBLICATION_TIMEOUT_SECONDS = 300.0
WORKER_STOP_TIMEOUT_SECONDS = 5.0
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_TOC_BYTES = 4 * 1024 * 1024
MAX_SCHEMA_BYTES = 16 * 1024 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024
ISOLATED_RESTORE_ENV = "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE"
RESTORE_ALLOWLIST_ENV = "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS"
DEFAULT_RESTORE_SENTINEL = "homelab-backup:postgresql-restore:v1"
RESTORE_TIMEOUT_SECONDS = 3600.0
POSTGRESQL_16_BIN = "/usr/local/lib/postgresql/16/bin"
PSQL16 = f"{POSTGRESQL_16_BIN}/psql"
PG_DUMP16 = f"{POSTGRESQL_16_BIN}/pg_dump"
PG_RESTORE16 = f"{POSTGRESQL_16_BIN}/pg_restore"
PRLIMIT = "/usr/bin/prlimit"
SHA256SUM = "/usr/bin/sha256sum"
PostgreSQLMode: TypeAlias = Literal["source", "restore_destination"]
_CONFIG_KEYS = frozenset({"mode", "host", "port", "database", "user", "password"})
_DATABASE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")

_IDENTITY_SQL = """
SELECT json_build_object(
  'server_version_num', current_setting('server_version_num')::integer,
  'server_version', current_setting('server_version'),
  'database', current_database(),
  'server_encoding', current_setting('server_encoding'),
  'lc_collate', (
    SELECT d.datcollate FROM pg_database AS d WHERE d.datname = current_database()
  ),
  'lc_ctype', (
    SELECT d.datctype FROM pg_database AS d WHERE d.datname = current_database()
  ),
  'database_comment', (
    SELECT shobj_description(d.oid, 'pg_database')
    FROM pg_database AS d WHERE d.datname = current_database()
  ),
  'database_owner', (
    SELECT pg_get_userbyid(d.datdba)
    FROM pg_database AS d WHERE d.datname = current_database()
  ),
  'current_user', current_user,
  'other_connections', (
    SELECT count(*)::integer
    FROM pg_stat_activity AS activity
    WHERE activity.datname = current_database()
      AND activity.pid <> pg_backend_pid()
  ),
  'schemas', (
    SELECT COALESCE(json_agg(n.nspname ORDER BY n.nspname), '[]'::json)
    FROM pg_namespace AS n
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
  ),
  'extensions', (
    SELECT COALESCE(
      json_agg(
        json_build_object(
          'name', e.extname,
          'schema', n.nspname,
          'version', e.extversion
        ) ORDER BY e.extname
      ),
      '[]'::json
    )
    FROM pg_extension AS e
    JOIN pg_namespace AS n ON n.oid = e.extnamespace
  ),
  'relations', (
    SELECT COALESCE(
      json_agg(
        json_build_object('schema', n.nspname, 'name', c.relname, 'kind', c.relkind)
        ORDER BY n.nspname, c.relname
      ),
      '[]'::json
    )
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  ),
  'sequences', (
    SELECT COALESCE(
      json_agg(
        json_build_object('schema', n.nspname, 'name', c.relname)
        ORDER BY n.nspname, c.relname
      ),
      '[]'::json
    )
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND c.relkind = 'S'
  ),
  'indexes', (
    SELECT COALESCE(
      json_agg(
        json_build_object(
          'schema', n.nspname,
          'table', t.relname,
          'name', i.relname
        ) ORDER BY n.nspname, t.relname, i.relname
      ),
      '[]'::json
    )
    FROM pg_index AS state
    JOIN pg_class AS i ON i.oid = state.indexrelid
    JOIN pg_class AS t ON t.oid = state.indrelid
    JOIN pg_namespace AS n ON n.oid = t.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND NOT EXISTS (
        SELECT 1 FROM pg_constraint AS constraint_state
        WHERE constraint_state.conindid = state.indexrelid
          AND constraint_state.conrelid = state.indrelid
      )
  ),
  'constraints', (
    SELECT COALESCE(
      json_agg(
        json_build_object(
          'schema', n.nspname,
          'table', t.relname,
          'name', c.conname,
          'type', c.contype,
          'definition', pg_get_constraintdef(c.oid, true),
          'validated', c.convalidated
        ) ORDER BY n.nspname, t.relname, c.conname
      ),
      '[]'::json
    )
    FROM pg_constraint AS c
    JOIN pg_class AS t ON t.oid = c.conrelid
    JOIN pg_namespace AS n ON n.oid = t.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
  ),
  'routines', (
    SELECT COALESCE(
      json_agg(
        json_build_object(
          'schema', n.nspname,
          'name', p.proname,
          'kind', p.prokind,
          'identity_arguments', pg_get_function_identity_arguments(p.oid),
          'toc_identity_arguments', oidvectortypes(p.proargtypes)
        ) ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
      ),
      '[]'::json
    )
    FROM pg_proc AS p
    JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND NOT EXISTS (
        SELECT 1
        FROM pg_depend AS dependency
        WHERE dependency.classid = 'pg_proc'::regclass
          AND dependency.objid = p.oid
          AND dependency.deptype = 'e'
      )
  ),
  'types', (
    SELECT COALESCE(
      json_agg(
        json_build_object(
          'schema', n.nspname,
          'name', t.typname,
          'kind', t.typtype
        ) ORDER BY n.nspname, t.typname
      ),
      '[]'::json
    )
    FROM pg_type AS t
    JOIN pg_namespace AS n ON n.oid = t.typnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND t.typrelid = 0
      AND t.typcategory <> 'A'
      AND NOT EXISTS (
        SELECT 1
        FROM pg_depend AS dependency
        WHERE dependency.classid = 'pg_type'::regclass
          AND dependency.objid = t.oid
          AND dependency.deptype = 'e'
      )
  ),
  'rls_tables', (
    SELECT COALESCE(
      json_agg(format('%I.%I', n.nspname, c.relname) ORDER BY n.nspname, c.relname),
      '[]'::json
    )
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND c.relkind IN ('r', 'p')
      AND c.relrowsecurity
  ),
  'large_objects', (
    SELECT COALESCE(
      json_agg(
        json_build_object(
          'oid', m.oid::bigint,
          'owner', pg_get_userbyid(m.lomowner),
          'readable', EXISTS (
            SELECT 1
            FROM aclexplode(COALESCE(m.lomacl, acldefault('L', m.lomowner))) AS acl
            WHERE acl.privilege_type = 'SELECT'
              AND (
                acl.grantee = 0
                OR pg_has_role(current_user, acl.grantee, 'USAGE')
              )
          )
        ) ORDER BY m.oid
      ),
      '[]'::json
    )
    FROM pg_largeobject_metadata AS m
  ),
  'invalid_indexes', (
    SELECT COALESCE(
      json_agg(format('%I.%I', n.nspname, i.relname) ORDER BY n.nspname, i.relname),
      '[]'::json
    )
    FROM pg_index AS state
    JOIN pg_class AS i ON i.oid = state.indexrelid
    JOIN pg_class AS t ON t.oid = state.indrelid
    JOIN pg_namespace AS n ON n.oid = t.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND NOT state.indisvalid
  ),
  'invalid_constraints', (
    SELECT COALESCE(
      json_agg(format('%I.%I', n.nspname, c.conname) ORDER BY n.nspname, c.conname),
      '[]'::json
    )
    FROM pg_constraint AS c
    JOIN pg_namespace AS n ON n.oid = c.connamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND NOT c.convalidated
  ),
  'event_triggers', (
    SELECT COALESCE(json_agg(e.evtname ORDER BY e.evtname), '[]'::json)
    FROM pg_event_trigger AS e
  ),
  'system_namespace_user_objects', (
    SELECT COALESCE(json_agg(objects.identity ORDER BY objects.identity), '[]'::json)
    FROM (
      SELECT format('relation:%I.%I', n.nspname, c.relname) AS identity
      FROM pg_class AS c
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      WHERE n.nspname IN ('pg_catalog', 'information_schema')
        AND c.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS dependency
          WHERE dependency.classid = 'pg_class'::regclass
            AND dependency.objid = c.oid
            AND dependency.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format(
        'routine:%I.%I(%s)',
        n.nspname,
        p.proname,
        pg_get_function_identity_arguments(p.oid)
      )
      FROM pg_proc AS p
      JOIN pg_namespace AS n ON n.oid = p.pronamespace
      WHERE n.nspname IN ('pg_catalog', 'information_schema')
        AND p.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS dependency
          WHERE dependency.classid = 'pg_proc'::regclass
            AND dependency.objid = p.oid
            AND dependency.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('type:%I.%I', n.nspname, t.typname)
      FROM pg_type AS t
      JOIN pg_namespace AS n ON n.oid = t.typnamespace
      WHERE n.nspname IN ('pg_catalog', 'information_schema')
        AND t.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS dependency
          WHERE dependency.classid = 'pg_type'::regclass
            AND dependency.objid = t.oid
            AND dependency.deptype IN ('p', 'e')
        )
    ) AS objects
  ),
  'unsupported_database_objects', (
    SELECT COALESCE(json_agg(objects.identity ORDER BY objects.identity), '[]'::json)
    FROM (
      SELECT format('trigger:%I.%I', n.nspname, t.tgname) AS identity
      FROM pg_trigger AS t
      JOIN pg_class AS c ON c.oid = t.tgrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      WHERE NOT t.tgisinternal
        AND n.nspname <> 'information_schema'
        AND n.nspname !~ '^pg_(catalog|toast|temp)'
      UNION ALL
      SELECT format('rule:%I.%I', n.nspname, r.rulename)
      FROM pg_rewrite AS r
      JOIN pg_class AS c ON c.oid = r.ev_class
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      WHERE r.rulename <> '_RETURN'
        AND n.nspname <> 'information_schema'
        AND n.nspname !~ '^pg_(catalog|toast|temp)'
      UNION ALL
      SELECT format('policy:%I.%I', n.nspname, p.polname)
      FROM pg_policy AS p
      JOIN pg_class AS c ON c.oid = p.polrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      WHERE n.nspname <> 'information_schema'
        AND n.nspname !~ '^pg_(catalog|toast|temp)'
      UNION ALL
      SELECT format('statistics:%I.%I', n.nspname, s.stxname)
      FROM pg_statistic_ext AS s
      JOIN pg_namespace AS n ON n.oid = s.stxnamespace
      WHERE n.nspname <> 'information_schema'
        AND n.nspname !~ '^pg_(catalog|toast|temp)'
      UNION ALL
      SELECT format('foreign-data-wrapper:%I', f.fdwname)
      FROM pg_foreign_data_wrapper AS f
      WHERE NOT EXISTS (
        SELECT 1 FROM pg_depend AS d
        WHERE d.classid = 'pg_foreign_data_wrapper'::regclass
          AND d.objid = f.oid
          AND d.deptype IN ('p', 'e')
      )
      UNION ALL
      SELECT format('server:%I', s.srvname)
      FROM pg_foreign_server AS s
      UNION ALL
      SELECT format('user-mapping:%s@%I', u.umuser, u.srvname)
      FROM pg_user_mappings AS u
      UNION ALL
      SELECT format('publication:%I', p.pubname)
      FROM pg_publication AS p
      UNION ALL
      SELECT format('subscription:%I', s.subname)
      FROM pg_subscription AS s
      UNION ALL
      SELECT format('cast:%s', c.oid)
      FROM pg_cast AS c
      WHERE c.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_cast'::regclass
            AND d.objid = c.oid
            AND d.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('collation:%I.%I', n.nspname, c.collname)
      FROM pg_collation AS c
      JOIN pg_namespace AS n ON n.oid = c.collnamespace
      WHERE c.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_collation'::regclass
            AND d.objid = c.oid
            AND d.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('operator:%I.%I', n.nspname, o.oprname)
      FROM pg_operator AS o
      JOIN pg_namespace AS n ON n.oid = o.oprnamespace
      WHERE o.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_operator'::regclass
            AND d.objid = o.oid
            AND d.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('operator-class:%I.%I', n.nspname, o.opcname)
      FROM pg_opclass AS o
      JOIN pg_namespace AS n ON n.oid = o.opcnamespace
      WHERE o.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_opclass'::regclass
            AND d.objid = o.oid
            AND d.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('operator-family:%I.%I', n.nspname, o.opfname)
      FROM pg_opfamily AS o
      JOIN pg_namespace AS n ON n.oid = o.opfnamespace
      WHERE o.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_opfamily'::regclass
            AND d.objid = o.oid
            AND d.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('conversion:%I.%I', n.nspname, c.conname)
      FROM pg_conversion AS c
      JOIN pg_namespace AS n ON n.oid = c.connamespace
      WHERE c.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_conversion'::regclass
            AND d.objid = c.oid
            AND d.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('transform:%s', t.oid)
      FROM pg_transform AS t
      WHERE NOT EXISTS (
        SELECT 1 FROM pg_depend AS d
        WHERE d.classid = 'pg_transform'::regclass
          AND d.objid = t.oid
          AND d.deptype IN ('p', 'e')
      )
      UNION ALL
      SELECT format('access-method:%I', a.amname)
      FROM pg_am AS a
      WHERE a.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_am'::regclass
            AND d.objid = a.oid
            AND d.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('text-search-config:%I.%I', n.nspname, t.cfgname)
      FROM pg_ts_config AS t
      JOIN pg_namespace AS n ON n.oid = t.cfgnamespace
      WHERE t.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_ts_config'::regclass
            AND d.objid = t.oid
            AND d.deptype IN ('p', 'e')
        )
      UNION ALL
      SELECT format('text-search-dictionary:%I.%I', n.nspname, t.dictname)
      FROM pg_ts_dict AS t
      JOIN pg_namespace AS n ON n.oid = t.dictnamespace
      WHERE t.oid >= 16384
        AND NOT EXISTS (
          SELECT 1 FROM pg_depend AS d
          WHERE d.classid = 'pg_ts_dict'::regclass
            AND d.objid = t.oid
            AND d.deptype IN ('p', 'e')
        )
    ) AS objects
  ),
  'security_definer_routines', (
    SELECT COALESCE(
      json_agg(
        format('%I.%I(%s)', n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
        ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
      ),
      '[]'::json
    )
    FROM pg_proc AS p
    JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE p.prosecdef
      AND p.oid >= 16384
      AND has_function_privilege(p.oid, 'EXECUTE')
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend AS d
        WHERE d.classid = 'pg_proc'::regclass
          AND d.objid = p.oid
          AND d.deptype IN ('p', 'e')
      )
  ),
  'role_superuser', (SELECT r.rolsuper FROM pg_roles AS r WHERE r.rolname = current_user),
  'role_bypassrls', (SELECT r.rolbypassrls FROM pg_roles AS r WHERE r.rolname = current_user),
  'role_createdb', (SELECT r.rolcreatedb FROM pg_roles AS r WHERE r.rolname = current_user),
  'role_createrole', (SELECT r.rolcreaterole FROM pg_roles AS r WHERE r.rolname = current_user),
  'role_replication', (SELECT r.rolreplication FROM pg_roles AS r WHERE r.rolname = current_user),
  'dangerous_role_memberships', (
    SELECT COALESCE(json_agg(r.rolname ORDER BY r.rolname), '[]'::json)
    FROM pg_roles AS r
    WHERE r.rolname NOT IN (current_user, 'pg_database_owner')
      AND pg_has_role(current_user, r.oid, 'MEMBER')
  ),
  'unrelated_database_privileges', (
    SELECT COALESCE(json_agg(d.datname ORDER BY d.datname), '[]'::json)
    FROM pg_database AS d
    WHERE d.datallowconn
      AND d.datname <> current_database()
      AND (
        has_database_privilege(d.oid, 'CONNECT')
        OR has_database_privilege(d.oid, 'CREATE')
        OR has_database_privilege(d.oid, 'TEMPORARY')
      )
  ),
  'database_create', has_database_privilege(current_database(), 'CREATE'),
  'database_temporary', has_database_privilege(current_database(), 'TEMPORARY'),
  'schema_create', (
    SELECT COALESCE(json_agg(n.nspname ORDER BY n.nspname), '[]'::json)
    FROM pg_namespace AS n
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND has_schema_privilege(n.oid, 'CREATE')
  ),
  'unusable_schemas', (
    SELECT COALESCE(json_agg(n.nspname ORDER BY n.nspname), '[]'::json)
    FROM pg_namespace AS n
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND NOT has_schema_privilege(n.oid, 'USAGE')
  ),
  'unreadable_relations', (
    SELECT COALESCE(
      json_agg(format('%I.%I', n.nspname, c.relname) ORDER BY n.nspname, c.relname),
      '[]'::json
    )
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND NOT has_table_privilege(c.oid, 'SELECT')
  ),
  'writable_relations', (
    SELECT COALESCE(
      json_agg(format('%I.%I', n.nspname, c.relname) ORDER BY n.nspname, c.relname),
      '[]'::json
    )
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND (
        has_table_privilege(c.oid, 'INSERT')
        OR has_table_privilege(c.oid, 'UPDATE')
        OR has_table_privilege(c.oid, 'DELETE')
        OR has_table_privilege(c.oid, 'TRUNCATE')
        OR has_table_privilege(c.oid, 'REFERENCES')
        OR has_table_privilege(c.oid, 'TRIGGER')
      )
  ),
  'unusable_sequences', (
    SELECT COALESCE(
      json_agg(format('%I.%I', n.nspname, c.relname) ORDER BY n.nspname, c.relname),
      '[]'::json
    )
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND CASE
        WHEN c.relkind = 'S' THEN NOT has_sequence_privilege(c.oid, 'SELECT')
        ELSE false
      END
  ),
  'writable_sequences', (
    SELECT COALESCE(
      json_agg(format('%I.%I', n.nspname, c.relname) ORDER BY n.nspname, c.relname),
      '[]'::json
    )
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_(catalog|toast|temp)'
      AND CASE
        WHEN c.relkind = 'S' THEN (
          has_sequence_privilege(c.oid, 'USAGE')
          OR has_sequence_privilege(c.oid, 'UPDATE')
        )
        ELSE false
      END
  ),
  'unreadable_large_objects', (
    SELECT COALESCE(json_agg(m.oid::bigint ORDER BY m.oid), '[]'::json)
    FROM pg_largeobject_metadata AS m
    WHERE NOT EXISTS (
      SELECT 1
      FROM aclexplode(COALESCE(m.lomacl, acldefault('L', m.lomowner))) AS acl
      WHERE acl.privilege_type = 'SELECT'
        AND (
          acl.grantee = 0
          OR pg_has_role(current_user, acl.grantee, 'USAGE')
        )
    )
  )
);
""".strip()


def validate_postgresql_config(config: object) -> bool:
    """Validate the exact flat PG16 source or restore-destination shape."""
    if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
        return False
    if config.get("mode") not in {"source", "restore_destination"}:
        return False

    host = config.get("host")
    if (
        not isinstance(host, str)
        or host != host.strip()
        or not host
        or any(unicodedata.category(character) == "Cc" for character in host)
        or "://" in host
        or "/" in host
        or any(character.isspace() for character in host)
    ):
        return False

    port = config.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return False

    database = config.get("database")
    if not isinstance(database, str) or _DATABASE_NAME_PATTERN.fullmatch(database) is None:
        return False

    user = config.get("user")
    if (
        not isinstance(user, str)
        or user != user.strip()
        or not user
        or any(unicodedata.category(character) == "Cc" for character in user)
        or any(character.isspace() for character in user)
    ):
        return False

    password = config.get("password")
    return (
        isinstance(password, str)
        and bool(password)
        and not password.isspace()
        and not any(unicodedata.category(character) == "Cc" for character in password)
    )


@dataclass(frozen=True, slots=True)
class PostgreSQLTarget:
    """One named PostgreSQL database and its private client identity."""

    mode: PostgreSQLMode
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "PostgreSQLTarget":
        """Create a typed target after an adapter has validated its config."""
        mode = config.get("mode")
        if mode not in {"source", "restore_destination"}:
            raise ValueError("PostgreSQL target mode is invalid")
        return cls(
            mode=cast(PostgreSQLMode, mode),
            host=str(config["host"]),
            port=int(config["port"]),
            database=str(config["database"]),
            user=str(config["user"]),
            password=str(config["password"]),
        )


@dataclass(frozen=True, slots=True)
class PostgreSQLIdentity:
    """Secret-free evidence returned by the PostgreSQL identity probe."""

    server_version_num: int
    server_version: str
    database: str
    server_encoding: str
    lc_collate: str
    lc_ctype: str
    catalog: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PostgreSQLArchiveEvidence:
    """Secret-free validation evidence for one custom PostgreSQL archive."""

    source_identity_sha256: str
    source_catalog_sha256: str
    archive_catalog_sha256: str
    toc_sha256: str
    catalog_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class VerifiedRestoreArtifact:
    """Bound identity and sidecar evidence for one RestoreService staging copy."""

    size_bytes: int
    sha256: str
    device: int
    inode: int
    sidecar: Mapping[str, object] = field(repr=False)


def _publication_process_worker(
    request: PrevalidatedArtifactPublication,
    connection: Connection,
) -> None:
    try:
        result = publish_prevalidated_backup_artifact(request)
        connection.send(("ok", result))
    except BaseException as exc:
        try:
            connection.send(("error", str(exc)[:512]))
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        connection.close()


def _start_publication_process(
    request: PrevalidatedArtifactPublication,
) -> tuple[BaseProcess, Connection]:
    process_context = multiprocessing.get_context("spawn")
    receiving, sending = process_context.Pipe(duplex=False)
    process = process_context.Process(
        target=_publication_process_worker,
        args=(request, sending),
        name="postgresql-artifact-publication",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


async def _join_publication_process(process: BaseProcess, timeout_seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while process.is_alive() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    process.join(0)


async def _stop_publication_process(process: BaseProcess) -> None:
    if not process.is_alive():
        await _join_publication_process(process, WORKER_STOP_TIMEOUT_SECONDS)
        if process.exitcode is None:
            raise RuntimeError("PostgreSQL publication worker could not be reaped")
        return
    process.terminate()
    await _join_publication_process(process, WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        await _join_publication_process(process, WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise RuntimeError("PostgreSQL publication worker could not be stopped")


async def _stop_publication_process_before_return(process: BaseProcess) -> None:
    stop_task = asyncio.create_task(_stop_publication_process(process))
    cancellation_seen = False
    while not stop_task.done():
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            cancellation_seen = True
    stop_task.result()
    if cancellation_seen:
        raise asyncio.CancelledError


async def _await_publication_process(
    process: BaseProcess,
    connection: Connection,
) -> ArtifactPublicationResult:
    try:
        await _join_publication_process(process, PUBLICATION_TIMEOUT_SECONDS)
        if process.is_alive():
            await _stop_publication_process_before_return(process)
            raise RuntimeError("PostgreSQL artifact publication timed out")
        try:
            received = connection.recv() if connection.poll() else None
        except (EOFError, OSError):
            received = None
        if not isinstance(received, tuple) or len(received) != 2:
            raise RuntimeError("PostgreSQL publication worker returned no result")
        kind, payload = received
        if kind != "ok":
            raise RuntimeError(
                payload
                if isinstance(payload, str) and payload
                else "PostgreSQL artifact publication failed"
            )
        if not isinstance(payload, ArtifactPublicationResult) or process.exitcode != 0:
            raise RuntimeError("PostgreSQL publication worker returned an invalid result")
        return payload
    except asyncio.CancelledError:
        await _stop_publication_process_before_return(process)
        raise
    except BaseException:
        if process.is_alive():
            await _stop_publication_process_before_return(process)
        raise
    finally:
        connection.close()


async def publish_postgresql_artifact(
    artifact: PendingBackupArtifact,
    plugin: BackupPlugin,
    context: BackupContext,
) -> None:
    """Publish a validated archive in a bounded, killable worker process."""
    request = prepare_prevalidated_artifact_publication(artifact, plugin, context)
    process, connection = _start_publication_process(request)
    result = await _await_publication_process(process, connection)
    complete_prevalidated_artifact_publication(artifact, result)


def _escape_pgpass(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace(":", "\\:")


def _password_file(target: PostgreSQLTarget) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix="homelab-backup-postgresql-pgpass-")
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        line = ":".join(
            _escape_pgpass(value)
            for value in (
                target.host,
                target.port,
                target.database,
                target.user,
                target.password,
            )
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as password_file:
            password_file.write(f"{line}\n")
            password_file.flush()
            os.fsync(password_file.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return path


def _environment(password_file: Path) -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PGPASSFILE": str(password_file),
        "PGCONNECT_TIMEOUT": str(int(CONNECT_TIMEOUT_SECONDS)),
        "PGOPTIONS": f"-c statement_timeout={int(CONNECT_TIMEOUT_SECONDS * 1000)}",
    }


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_identity(
    payload: bytes,
    target: PostgreSQLTarget,
    *,
    expected_state: Literal["source", "fresh_destination", "restored_destination"],
    allowed_unsupported_database_objects: frozenset[str] = frozenset(),
    restore_sentinel: str = DEFAULT_RESTORE_SENTINEL,
) -> PostgreSQLIdentity:
    if len(payload) > MAX_PROBE_BYTES:
        raise RuntimeError("PostgreSQL identity response exceeded its safety limit")
    try:
        value = json.loads(payload.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("PostgreSQL returned an invalid identity response") from exc
    if not isinstance(value, dict):
        raise RuntimeError("PostgreSQL returned an invalid identity response")
    server_version_num = value.get("server_version_num")
    server_version = value.get("server_version")
    database = value.get("database")
    server_encoding = value.get("server_encoding")
    lc_collate = value.get("lc_collate")
    lc_ctype = value.get("lc_ctype")
    if (
        isinstance(server_version_num, bool)
        or not isinstance(server_version_num, int)
        or not isinstance(server_version, str)
        or not server_version
        or not isinstance(database, str)
        or not database
        or not isinstance(server_encoding, str)
        or not server_encoding
        or not isinstance(lc_collate, str)
        or not lc_collate
        or not isinstance(lc_ctype, str)
        or not lc_ctype
    ):
        raise RuntimeError("PostgreSQL returned an invalid identity response")
    catalog_keys = (
        "schemas",
        "extensions",
        "relations",
        "sequences",
        "indexes",
        "constraints",
        "routines",
        "types",
        "rls_tables",
        "large_objects",
        "invalid_indexes",
        "invalid_constraints",
        "event_triggers",
        "system_namespace_user_objects",
        "unsupported_database_objects",
        "security_definer_routines",
        "schema_create",
        "unusable_schemas",
        "unreadable_relations",
        "writable_relations",
        "unusable_sequences",
        "writable_sequences",
        "unreadable_large_objects",
        "dangerous_role_memberships",
        "unrelated_database_privileges",
    )
    if any(not isinstance(value.get(key), list) for key in catalog_keys):
        raise RuntimeError("PostgreSQL returned an invalid catalog response")
    privilege_keys = (
        "role_superuser",
        "role_bypassrls",
        "role_createdb",
        "role_createrole",
        "role_replication",
        "database_create",
        "database_temporary",
    )
    if any(not isinstance(value.get(key), bool) for key in privilege_keys):
        raise RuntimeError("PostgreSQL returned an invalid privilege response")
    semantic_server_version = f"{server_version_num // 10000}.{server_version_num % 10000}"
    if server_version != semantic_server_version and not server_version.startswith(
        f"{semantic_server_version} "
    ):
        raise RuntimeError("PostgreSQL server version evidence was inconsistent")
    catalog = {key: value[key] for key in (*catalog_keys, *privilege_keys)}
    identity = PostgreSQLIdentity(
        server_version_num=server_version_num,
        server_version=semantic_server_version,
        database=database,
        server_encoding=server_encoding,
        lc_collate=lc_collate,
        lc_ctype=lc_ctype,
        catalog=catalog,
    )
    if identity.server_version_num // 10000 != 16:
        raise RuntimeError("PostgreSQL server major version must be 16")
    if identity.database != target.database:
        raise RuntimeError("PostgreSQL database identity did not match the target")
    if identity.server_encoding != "UTF8":
        raise RuntimeError("PostgreSQL database encoding must be UTF8")
    if expected_state == "source":
        _validate_source_catalog(
            identity.catalog,
            allowed_unsupported_database_objects=allowed_unsupported_database_objects,
        )
    else:
        _validate_restore_destination(
            value,
            identity.catalog,
            target,
            require_fresh=expected_state == "fresh_destination",
            restore_sentinel=restore_sentinel,
            allowed_unsupported_database_objects=allowed_unsupported_database_objects,
        )
    return identity


def _validate_source_catalog(
    catalog: Mapping[str, object],
    *,
    allowed_unsupported_database_objects: frozenset[str] = frozenset(),
) -> None:
    privileged = (
        "role_superuser",
        "role_bypassrls",
        "role_createdb",
        "role_createrole",
        "role_replication",
        "database_create",
    )
    if any(catalog[key] is True for key in privileged) or catalog["schema_create"]:
        raise RuntimeError("PostgreSQL backup identity has excessive privileges")
    if catalog["database_temporary"] is True:
        raise RuntimeError("PostgreSQL backup identity has temporary database privilege")
    if catalog["rls_tables"]:
        raise RuntimeError("PostgreSQL source contains unsupported RLS tables")
    if catalog["invalid_indexes"] or catalog["invalid_constraints"]:
        raise RuntimeError("PostgreSQL source contains invalid catalog objects")
    if catalog["event_triggers"] or catalog["system_namespace_user_objects"]:
        raise RuntimeError("PostgreSQL source contains unsupported executable objects")
    unsupported = catalog["unsupported_database_objects"]
    if not isinstance(unsupported, list) or any(
        not isinstance(value, str) for value in unsupported
    ):
        raise RuntimeError("PostgreSQL source returned an invalid catalog response")
    observed_unsupported = frozenset(unsupported)
    if len(observed_unsupported) != len(unsupported):
        raise RuntimeError("PostgreSQL source returned an ambiguous catalog response")
    if observed_unsupported != allowed_unsupported_database_objects:
        raise RuntimeError("PostgreSQL source contains unsupported database objects")
    if catalog["security_definer_routines"]:
        raise RuntimeError("PostgreSQL backup identity can execute security definer routines")
    if catalog["unusable_schemas"]:
        raise RuntimeError("PostgreSQL backup identity cannot use every schema")
    if catalog["unreadable_relations"]:
        raise RuntimeError("PostgreSQL backup identity cannot read every relation")
    if catalog["writable_relations"]:
        raise RuntimeError("PostgreSQL backup identity has relation write privileges")
    if catalog["unusable_sequences"]:
        raise RuntimeError("PostgreSQL backup identity cannot read every sequence")
    if catalog["writable_sequences"]:
        raise RuntimeError("PostgreSQL backup identity can mutate sequence state")
    if catalog["unreadable_large_objects"]:
        raise RuntimeError("PostgreSQL backup identity cannot read every large object")
    if catalog["dangerous_role_memberships"] or catalog["unrelated_database_privileges"]:
        raise RuntimeError("PostgreSQL backup identity has authority outside the target database")


def _validate_restore_destination(
    value: Mapping[str, object],
    catalog: Mapping[str, object],
    target: PostgreSQLTarget,
    *,
    require_fresh: bool,
    restore_sentinel: str,
    allowed_unsupported_database_objects: frozenset[str],
) -> None:
    if value.get("database_comment") != restore_sentinel:
        raise RuntimeError("PostgreSQL restore destination sentinel did not match")
    if value.get("database_owner") != target.user or value.get("current_user") != target.user:
        raise RuntimeError("PostgreSQL restore identity must own the destination database")
    other_connections = value.get("other_connections")
    if isinstance(other_connections, bool) or other_connections != 0:
        raise RuntimeError("PostgreSQL restore destination has another active connection")
    cluster_privileges = (
        "role_superuser",
        "role_bypassrls",
        "role_createdb",
        "role_createrole",
        "role_replication",
    )
    if any(catalog[key] is True for key in cluster_privileges):
        raise RuntimeError("PostgreSQL restore identity has cluster-wide privileges")
    if catalog["dangerous_role_memberships"] or catalog["unrelated_database_privileges"]:
        raise RuntimeError("PostgreSQL restore identity has authority outside the target database")
    if catalog["database_create"] is not True:
        raise RuntimeError("PostgreSQL restore identity cannot create the restored schema")
    if catalog["unusable_schemas"]:
        raise RuntimeError("PostgreSQL restore identity cannot use every schema")
    if require_fresh:
        if catalog["schema_create"] != ["public"]:
            raise RuntimeError("PostgreSQL restore identity cannot create the restored schema")
        extensions = catalog["extensions"]
        if (
            not isinstance(extensions, list)
            or len(extensions) != 1
            or not isinstance(extensions[0], dict)
            or extensions[0].get("name") != "plpgsql"
        ):
            raise RuntimeError("PostgreSQL restore destination has unexpected extensions")
        if catalog["schemas"] != ["public"]:
            raise RuntimeError("PostgreSQL restore destination has unexpected schemas")
        must_be_empty = (
            "relations",
            "sequences",
            "routines",
            "types",
            "rls_tables",
            "large_objects",
            "invalid_indexes",
            "invalid_constraints",
            "event_triggers",
            "system_namespace_user_objects",
            "unsupported_database_objects",
            "security_definer_routines",
            "unreadable_relations",
            "writable_relations",
            "unusable_sequences",
            "writable_sequences",
            "unreadable_large_objects",
        )
        if any(catalog[key] for key in must_be_empty):
            raise RuntimeError("PostgreSQL restore destination must be fresh and empty")
        return
    restored_schemas = catalog["schemas"]
    writable_schemas = catalog["schema_create"]
    if (
        not isinstance(restored_schemas, list)
        or not isinstance(writable_schemas, list)
        or any(not isinstance(value, str) for value in (*restored_schemas, *writable_schemas))
        or set(writable_schemas) != set(restored_schemas)
    ):
        raise RuntimeError("PostgreSQL restore identity cannot create the restored schema")
    if catalog["rls_tables"]:
        raise RuntimeError("PostgreSQL restored database contains unsupported RLS tables")
    if catalog["event_triggers"] or catalog["system_namespace_user_objects"]:
        raise RuntimeError("PostgreSQL restored database contains executable system objects")
    unsupported = catalog["unsupported_database_objects"]
    if (
        not isinstance(unsupported, list)
        or any(not isinstance(value, str) for value in unsupported)
        or len(frozenset(unsupported)) != len(unsupported)
        or frozenset(unsupported) != allowed_unsupported_database_objects
    ):
        raise RuntimeError("PostgreSQL restored database contains unsupported database objects")
    if catalog["invalid_indexes"] or catalog["invalid_constraints"]:
        raise RuntimeError("PostgreSQL restored database contains invalid catalog objects")
    if (
        catalog["unreadable_relations"]
        or catalog["unusable_sequences"]
        or catalog["unreadable_large_objects"]
    ):
        raise RuntimeError("PostgreSQL restore identity cannot validate all restored objects")


async def probe_postgresql(
    target: PostgreSQLTarget,
    *,
    expected_state: Literal["source", "fresh_destination", "restored_destination"] | None = None,
    allowed_unsupported_database_objects: frozenset[str] = frozenset(),
    restore_sentinel: str = DEFAULT_RESTORE_SENTINEL,
) -> PostgreSQLIdentity:
    """Probe one exact PostgreSQL 16 database using private libpq authentication."""
    password_file = _password_file(target)
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                PSQL16,
                "-X",
                "-h",
                target.host,
                "-p",
                str(target.port),
                "-U",
                target.user,
                "--dbname",
                target.database,
                "--set",
                "ON_ERROR_STOP=on",
                "-tA",
                "-c",
                _IDENTITY_SQL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_environment(password_file),
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("PostgreSQL 16 psql client is unavailable") from exc
        except OSError as exc:
            raise ConnectionError("Unable to connect to the PostgreSQL database") from exc
        stdout, stderr = await _communicate_with_limits(
            process,
            operation="PostgreSQL identity probe",
            timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            stdout_limit_bytes=MAX_PROBE_BYTES,
            stderr_limit_bytes=MAX_PROBE_BYTES,
        )
        if process.returncode != 0:
            raise ConnectionError("Unable to connect to the PostgreSQL database")
        if stderr:
            raise RuntimeError("PostgreSQL identity probe emitted diagnostics")
        state = expected_state or ("source" if target.mode == "source" else "fresh_destination")
        return _parse_identity(
            stdout,
            target,
            expected_state=state,
            allowed_unsupported_database_objects=allowed_unsupported_database_objects,
            restore_sentinel=restore_sentinel,
        )
    finally:
        password_file.unlink(missing_ok=True)


async def query_postgresql_json(
    target: PostgreSQLTarget,
    query: str,
    *,
    operation: str,
    max_bytes: int = MAX_PROBE_BYTES,
) -> Mapping[str, object]:
    """Run one adapter-owned read-only JSON query through private PG16 auth."""
    password_file = _password_file(target)
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                PSQL16,
                "-X",
                "-h",
                target.host,
                "-p",
                str(target.port),
                "-U",
                target.user,
                "--dbname",
                target.database,
                "--set",
                "ON_ERROR_STOP=on",
                "-tA",
                "-c",
                query,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_environment(password_file),
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("PostgreSQL 16 psql client is unavailable") from exc
        except OSError as exc:
            raise ConnectionError("Unable to connect to the PostgreSQL database") from exc
        stdout, stderr = await _communicate_with_limits(
            process,
            operation=operation,
            timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            stdout_limit_bytes=max_bytes,
            stderr_limit_bytes=MAX_PROBE_BYTES,
        )
        if process.returncode != 0:
            raise ConnectionError("Unable to query the PostgreSQL database")
        if stderr:
            raise RuntimeError(f"{operation} emitted diagnostics")
        try:
            value = json.loads(stdout.decode("utf-8").strip())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{operation} returned an invalid response") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{operation} returned an invalid response")
        return value
    finally:
        password_file.unlink(missing_ok=True)


async def postgresql_archive_schema_sha256(descriptor: int) -> str:
    """Render and hash normalized schema SQL from one bound custom archive."""
    inspector = await asyncio.create_subprocess_exec(
        PG_RESTORE16,
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "--file=-",
        f"/proc/self/fd/{descriptor}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(descriptor,),
    )
    payload, diagnostics = await _communicate_with_limits(
        inspector,
        operation="PostgreSQL archive schema inspection",
        timeout_seconds=CONNECT_TIMEOUT_SECONDS,
        stdout_limit_bytes=MAX_SCHEMA_BYTES,
        stderr_limit_bytes=MAX_PROBE_BYTES,
    )
    if inspector.returncode != 0 or diagnostics:
        raise RuntimeError("PostgreSQL archive schema inspection failed")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("PostgreSQL archive schema was malformed") from exc
    normalized_lines: list[str] = []
    restrict_count = 0
    unrestrict_count = 0
    for line in text.splitlines():
        if re.fullmatch(r"\\restrict [A-Za-z0-9]+", line):
            restrict_count += 1
            continue
        if re.fullmatch(r"\\unrestrict [A-Za-z0-9]+", line):
            unrestrict_count += 1
            continue
        normalized_lines.append(line)
    if restrict_count != 1 or unrestrict_count != 1 or "\x00" in text:
        raise RuntimeError("PostgreSQL archive schema was malformed")
    normalized = ("\n".join(normalized_lines) + "\n").encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _validate_archive_toc(
    payload: bytes,
    identity: PostgreSQLIdentity,
    *,
    require_catalog_match: bool = True,
) -> tuple[str, str]:
    if not payload or len(payload) > MAX_TOC_BYTES:
        raise RuntimeError("PostgreSQL archive TOC exceeded its safety limit")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("PostgreSQL archive returned a malformed TOC") from exc
    if f"Dumped from database version: {identity.server_version}" not in text:
        raise RuntimeError("PostgreSQL archive server version did not match the source")
    if f"Dumped by pg_dump version: {identity.server_version}" not in text:
        raise RuntimeError("PostgreSQL archive client version did not match the source")
    entries = [line for line in text.splitlines() if line and not line.startswith(";")]
    if not entries or any(";" not in line for line in entries):
        raise RuntimeError("PostgreSQL archive returned a malformed TOC")
    archive_catalog_sha256, object_counts = _archive_catalog_evidence(entries)
    if require_catalog_match:
        if object_counts != _expected_archive_object_counts(identity.catalog):
            raise RuntimeError("PostgreSQL archive objects did not match the source catalog")
        _require_archive_objects_match_catalog(entries, identity.catalog)
    return hashlib.sha256(payload).hexdigest(), archive_catalog_sha256


def _archive_catalog_sha256(entries: list[str]) -> str:
    digest, _ = _archive_catalog_evidence(entries)
    return digest


_TOC_ENTRY = re.compile(r"^(?P<id>[1-9][0-9]*); [0-9]+ [0-9]+ (?P<body>.+)$")
_TOC_DESCRIPTORS = (
    "MATERIALIZED VIEW DATA",
    "MATERIALIZED VIEW",
    "SEQUENCE OWNED BY",
    "FOREIGN TABLE",
    "FK CONSTRAINT",
    "TABLE ATTACH",
    "TABLE DATA",
    "INDEX ATTACH",
    "BLOB COMMENTS",
    "SEQUENCE SET",
    "CONSTRAINT",
    "PROCEDURE",
    "AGGREGATE",
    "SEARCHPATH",
    "STDSTRINGS",
    "EXTENSION",
    "FUNCTION",
    "SEQUENCE",
    "ENCODING",
    "COMMENT",
    "DEFAULT",
    "TRIGGER",
    "SCHEMA",
    "TABLE",
    "INDEX",
    "DOMAIN",
    "BLOBS",
    "BLOB",
    "VIEW",
    "TYPE",
)
_PRIMARY_TOC_CLASSES = {
    "SCHEMA": "schemas",
    "EXTENSION": "extensions",
    "TABLE": "relations",
    "VIEW": "relations",
    "FOREIGN TABLE": "relations",
    "MATERIALIZED VIEW": "relations",
    "SEQUENCE": "sequences",
    "INDEX": "indexes",
    "CONSTRAINT": "constraints",
    "FK CONSTRAINT": "constraints",
    "FUNCTION": "routines",
    "PROCEDURE": "routines",
    "AGGREGATE": "routines",
    "TYPE": "types",
    "DOMAIN": "types",
    "BLOB": "large_objects",
}


def _archive_catalog_evidence(entries: list[str]) -> tuple[str, Mapping[str, int]]:
    canonical_entries: set[str] = set()
    toc_ids: set[int] = set()
    counts = {key: 0 for key in set(_PRIMARY_TOC_CLASSES.values())}

    for entry in entries:
        match = _TOC_ENTRY.fullmatch(entry)
        if match is None:
            raise RuntimeError("PostgreSQL archive returned a malformed TOC")
        toc_id = int(match.group("id"))
        if toc_id in toc_ids:
            raise RuntimeError("PostgreSQL archive returned an ambiguous TOC")
        toc_ids.add(toc_id)
        body = match.group("body")
        descriptor = next(
            (
                candidate
                for candidate in _TOC_DESCRIPTORS
                if body == candidate or body.startswith(f"{candidate} ")
            ),
            None,
        )
        if descriptor is None:
            raise RuntimeError("PostgreSQL archive contains an unsafe TOC descriptor")
        if body in canonical_entries:
            raise RuntimeError("PostgreSQL archive returned an ambiguous TOC")
        canonical_entries.add(body)
        category = _PRIMARY_TOC_CLASSES.get(descriptor)
        if category is not None and not (
            descriptor == "EXTENSION" and body.strip() == "EXTENSION - plpgsql"
        ):
            counts[category] += 1
    return _canonical_sha256(sorted(canonical_entries)), counts


def _expected_archive_object_counts(catalog: Mapping[str, object]) -> Mapping[str, int]:
    schemas = _catalog_list(catalog, "schemas")
    extensions = _catalog_list(catalog, "extensions")
    constraints = _catalog_list(catalog, "constraints")
    return {
        "schemas": sum(value != "public" for value in schemas),
        "extensions": sum(
            isinstance(value, dict) and value.get("name") != "plpgsql" for value in extensions
        ),
        "relations": len(_catalog_list(catalog, "relations")),
        "sequences": len(_catalog_list(catalog, "sequences")),
        "indexes": len(_catalog_list(catalog, "indexes")),
        "constraints": sum(
            isinstance(value, dict) and value.get("type") != "c" for value in constraints
        ),
        "routines": len(_catalog_list(catalog, "routines")),
        "types": len(_catalog_list(catalog, "types")),
        "large_objects": len(_catalog_list(catalog, "large_objects")),
    }


def _require_archive_objects_match_catalog(
    entries: list[str],
    catalog: Mapping[str, object],
) -> None:
    def require_identifier(value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or any(character.isspace() for character in value)
        ):
            raise RuntimeError("PostgreSQL source catalog cannot be bound unambiguously")
        return value

    def matches_complete_authority(body: str, prefix: str, descriptor: str) -> bool:
        if not body.startswith(prefix):
            return False
        owner = body[len(prefix) :]
        if descriptor == "EXTENSION":
            return not owner
        return bool(owner) and not any(character.isspace() for character in owner)

    expected: dict[str, set[str]] = {descriptor: set() for descriptor in _PRIMARY_TOC_CLASSES}
    for schema in _catalog_list(catalog, "schemas"):
        schema = require_identifier(schema)
        if schema != "public":
            expected["SCHEMA"].add(f"SCHEMA - {schema} ")
    for value in _catalog_list(catalog, "extensions"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        name = require_identifier(value.get("name"))
        if name != "plpgsql":
            expected["EXTENSION"].add(f"EXTENSION - {name} ")

    relation_descriptors = {
        "r": "TABLE",
        "p": "TABLE",
        "v": "VIEW",
        "m": "MATERIALIZED VIEW",
        "f": "FOREIGN TABLE",
    }
    for value in _catalog_list(catalog, "relations"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = require_identifier(value.get("schema"))
        name = require_identifier(value.get("name"))
        kind = value.get("kind")
        descriptor = relation_descriptors.get(kind) if isinstance(kind, str) else None
        if descriptor is None:
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        expected[descriptor].add(f"{descriptor} {schema} {name} ")

    for key, descriptor in (("sequences", "SEQUENCE"), ("indexes", "INDEX")):
        for value in _catalog_list(catalog, key):
            if not isinstance(value, dict):
                raise RuntimeError("PostgreSQL source returned an invalid catalog response")
            schema = require_identifier(value.get("schema"))
            name = require_identifier(value.get("name"))
            expected[descriptor].add(f"{descriptor} {schema} {name} ")

    for value in _catalog_list(catalog, "constraints"):
        if not isinstance(value, dict) or value.get("type") == "c":
            continue
        schema = require_identifier(value.get("schema"))
        table = require_identifier(value.get("table"))
        name = require_identifier(value.get("name"))
        descriptor = "FK CONSTRAINT" if value.get("type") == "f" else "CONSTRAINT"
        expected[descriptor].add(f"{descriptor} {schema} {table} {name} ")

    routine_descriptors = {
        "a": "AGGREGATE",
        "f": "FUNCTION",
        "p": "PROCEDURE",
        "w": "FUNCTION",
    }
    for value in _catalog_list(catalog, "routines"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = require_identifier(value.get("schema"))
        name = require_identifier(value.get("name"))
        arguments = value.get("toc_identity_arguments")
        kind = value.get("kind")
        descriptor = routine_descriptors.get(kind) if isinstance(kind, str) else None
        if not isinstance(arguments, str) or descriptor is None:
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        expected[descriptor].add(f"{descriptor} {schema} {name}({arguments}) ")

    for value in _catalog_list(catalog, "types"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = require_identifier(value.get("schema"))
        name = require_identifier(value.get("name"))
        descriptor = "DOMAIN" if value.get("kind") == "d" else "TYPE"
        expected[descriptor].add(f"{descriptor} {schema} {name} ")

    for value in _catalog_list(catalog, "large_objects"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        oid = value.get("oid")
        if isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0:
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        expected["BLOB"].add(f"BLOB - {oid} ")

    for entry in entries:
        match = _TOC_ENTRY.fullmatch(entry)
        if match is None:
            raise RuntimeError("PostgreSQL archive returned a malformed TOC")
        body = match.group("body")
        candidates = [
            (descriptor, prefix)
            for descriptor, prefixes in expected.items()
            for prefix in prefixes
            if matches_complete_authority(body, prefix, descriptor)
        ]
        if not candidates:
            if body.strip() == "EXTENSION - plpgsql":
                continue
            descriptor = next(
                (
                    candidate
                    for candidate in _TOC_DESCRIPTORS
                    if body == candidate or body.startswith(f"{candidate} ")
                ),
                None,
            )
            if descriptor in _PRIMARY_TOC_CLASSES:
                raise RuntimeError(
                    f"PostgreSQL archive {descriptor} objects did not match the source catalog"
                )
            continue
        if len(candidates) != 1:
            raise RuntimeError("PostgreSQL archive returned an ambiguous TOC")
        descriptor, prefix = candidates[0]
        expected[descriptor].remove(prefix)
    if any(prefixes for prefixes in expected.values()):
        raise RuntimeError("PostgreSQL archive objects did not match the source catalog")


def _catalog_projection(catalog: Mapping[str, object]) -> Mapping[str, object]:
    schemas: set[str] = set()
    for value in _catalog_list(catalog, "schemas"):
        if not isinstance(value, str) or not value:
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schemas.add(value)

    relations: set[tuple[str, str, str]] = set()
    relation_types = {
        "r": "TABLE",
        "p": "TABLE",
        "v": "VIEW",
        "m": "MATERIALIZED VIEW",
        "f": "FOREIGN TABLE",
    }
    for value in _catalog_list(catalog, "relations"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = value.get("schema")
        name = value.get("name")
        relation_kind = value.get("kind")
        if (
            not isinstance(schema, str)
            or not isinstance(name, str)
            or relation_kind not in relation_types
        ):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        relations.add((relation_types[relation_kind], schema, name))

    sequences: set[tuple[str, str]] = set()
    for value in _catalog_list(catalog, "sequences"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = value.get("schema")
        name = value.get("name")
        if not isinstance(schema, str) or not isinstance(name, str):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        sequences.add((schema, name))

    indexes: set[tuple[str, str]] = set()
    for value in _catalog_list(catalog, "indexes"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = value.get("schema")
        name = value.get("name")
        if not isinstance(schema, str) or not isinstance(name, str):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        if (schema, name) in indexes:
            raise RuntimeError("PostgreSQL source returned an ambiguous catalog response")
        indexes.add((schema, name))

    constraints: set[tuple[str, str, str, str, str, bool]] = set()
    constraint_types = {"c", "f", "p", "u", "x"}
    for value in _catalog_list(catalog, "constraints"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = value.get("schema")
        table = value.get("table")
        name = value.get("name")
        constraint_type = value.get("type")
        definition = value.get("definition")
        validated = value.get("validated")
        if (
            not isinstance(schema, str)
            or not isinstance(table, str)
            or not isinstance(name, str)
            or constraint_type not in constraint_types
            or not isinstance(definition, str)
            or not definition
            or not isinstance(validated, bool)
        ):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        descriptor = (constraint_type, schema, table, name, definition, validated)
        if descriptor in constraints:
            raise RuntimeError("PostgreSQL source returned an ambiguous catalog response")
        constraints.add(descriptor)

    routines: set[tuple[str, str, str, str]] = set()
    routine_types = {
        "a": "AGGREGATE",
        "f": "FUNCTION",
        "p": "PROCEDURE",
        "w": "FUNCTION",
    }
    for value in _catalog_list(catalog, "routines"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = value.get("schema")
        name = value.get("name")
        kind = value.get("kind")
        arguments = value.get("identity_arguments")
        toc_arguments = value.get("toc_identity_arguments")
        if (
            not isinstance(schema, str)
            or not isinstance(name, str)
            or kind not in routine_types
            or not isinstance(arguments, str)
            or not isinstance(toc_arguments, str)
        ):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        routine_descriptor = (
            routine_types[kind],
            schema,
            f"{name}({arguments})",
            f"{name}({toc_arguments})",
        )
        if routine_descriptor in routines:
            raise RuntimeError("PostgreSQL source returned an ambiguous catalog response")
        routines.add(routine_descriptor)

    types: set[tuple[str, str, str]] = set()
    for value in _catalog_list(catalog, "types"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        schema = value.get("schema")
        name = value.get("name")
        kind = value.get("kind")
        if (
            not isinstance(schema, str)
            or not isinstance(name, str)
            or not isinstance(kind, str)
            or kind not in {"b", "c", "d", "e", "m", "p", "r"}
        ):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        type_descriptor = ("DOMAIN" if kind == "d" else "TYPE", schema, name)
        if type_descriptor in types:
            raise RuntimeError("PostgreSQL source returned an ambiguous catalog response")
        types.add(type_descriptor)

    extensions: set[str] = set()
    for value in _catalog_list(catalog, "extensions"):
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        if value["name"] != "plpgsql":
            extensions.add(value["name"])

    large_objects: set[int] = set()
    for value in _catalog_list(catalog, "large_objects"):
        if not isinstance(value, dict):
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        oid = value.get("oid")
        if isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0:
            raise RuntimeError("PostgreSQL source returned an invalid catalog response")
        large_objects.add(oid)
    return {
        "schemas": sorted(schemas),
        "extensions": sorted(extensions),
        "relations": [list(value) for value in sorted(relations)],
        "sequences": [list(value) for value in sorted(sequences)],
        "indexes": [list(value) for value in sorted(indexes)],
        "constraints": [list(value) for value in sorted(constraints)],
        "routines": [list(value) for value in sorted(routines)],
        "types": [list(value) for value in sorted(types)],
        "large_objects": sorted(large_objects),
    }


def _catalog_list(catalog: Mapping[str, object], key: str) -> list[object]:
    value = catalog.get(key)
    if not isinstance(value, list):
        raise RuntimeError("PostgreSQL source returned an invalid catalog response")
    return value


async def write_postgresql_archive(
    target: PostgreSQLTarget,
    identity: PostgreSQLIdentity,
    artifact: PendingBackupArtifact,
    *,
    allowed_unsupported_database_objects: frozenset[str] = frozenset(),
) -> PostgreSQLArchiveEvidence:
    """Stream, bind and inspect one custom archive into a pending artifact."""
    password_file = _password_file(target)
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(artifact.temporary_path, flags, 0o600)
        with tempfile.TemporaryFile(mode="w+b") as error_file:
            try:
                process = await asyncio.create_subprocess_exec(
                    PRLIMIT,
                    f"--fsize={MAX_ARCHIVE_BYTES}:{MAX_ARCHIVE_BYTES}",
                    "--",
                    PG_DUMP16,
                    "-h",
                    target.host,
                    "-p",
                    str(target.port),
                    "-U",
                    target.user,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    target.database,
                    stdout=descriptor,
                    stderr=error_file,
                    env=_environment(password_file),
                )
            except FileNotFoundError as exc:
                raise FileNotFoundError("PostgreSQL 16 pg_dump client is unavailable") from exc
            returncode = await run_process_with_timeout(
                process,
                process.wait(),
                operation="PostgreSQL pg_dump backup",
                timeout_seconds=BACKUP_TIMEOUT_SECONDS,
            )
            error_file.seek(0)
            diagnostics = error_file.read(MAX_PROBE_BYTES + 1)
        if returncode != 0:
            raise RuntimeError("PostgreSQL pg_dump failed")
        if diagnostics:
            raise RuntimeError("PostgreSQL pg_dump emitted diagnostics")
        artifact.publication_fd = descriptor
        descriptor = None
        opened = os.fstat(artifact.publication_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_size > MAX_ARCHIVE_BYTES
        ):
            raise RuntimeError("PostgreSQL archive identity changed after capture")
        streamed_sha256 = await _hash_open_file_cancellable(
            artifact.publication_fd,
            timeout_seconds=PUBLICATION_TIMEOUT_SECONDS,
        )

        inspector = await asyncio.create_subprocess_exec(
            PG_RESTORE16,
            "--list",
            f"/proc/self/fd/{artifact.publication_fd}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(artifact.publication_fd,),
        )
        toc, inspector_stderr = await _communicate_with_limits(
            inspector,
            operation="PostgreSQL archive inspection",
            timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            stdout_limit_bytes=MAX_TOC_BYTES,
            stderr_limit_bytes=MAX_PROBE_BYTES,
        )
        if inspector.returncode != 0 or inspector_stderr:
            raise RuntimeError("PostgreSQL pg_dump produced an invalid archive")
        toc_sha256, catalog_sha256 = _validate_archive_toc(toc, identity)
        source_catalog_sha256 = _expected_archive_catalog_sha256(identity.catalog)
        stable_identity = await probe_postgresql(
            target,
            allowed_unsupported_database_objects=allowed_unsupported_database_objects,
        )
        if _expected_archive_catalog_sha256(stable_identity.catalog) != source_catalog_sha256:
            raise RuntimeError("PostgreSQL source catalog changed during capture")
        artifact.publication_sha256 = streamed_sha256
        catalog_counts = {
            key: len(identity.catalog[key])  # type: ignore[arg-type]
            for key in (
                "schemas",
                "extensions",
                "relations",
                "sequences",
                "indexes",
                "constraints",
                "routines",
                "types",
                "large_objects",
            )
        }
        source_identity = {
            "host": target.host,
            "port": target.port,
            "database": target.database,
            "user": target.user,
        }
        return PostgreSQLArchiveEvidence(
            source_identity_sha256=_canonical_sha256(source_identity),
            source_catalog_sha256=source_catalog_sha256,
            archive_catalog_sha256=catalog_sha256,
            toc_sha256=toc_sha256,
            catalog_counts=catalog_counts,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        password_file.unlink(missing_ok=True)


def _expected_archive_catalog_sha256(catalog: Mapping[str, object]) -> str:
    return _canonical_sha256(_catalog_projection(catalog))


def postgresql_catalog_sha256(identity: PostgreSQLIdentity) -> str:
    """Return the normalized non-secret object-catalog digest for an identity."""
    return _expected_archive_catalog_sha256(identity.catalog)


def _require_restore_metadata(
    metadata: Mapping[str, object],
    source_identity: Mapping[str, object],
    *,
    validation: str,
) -> VerifiedRestoreArtifact:
    artifact_bytes = metadata.get("artifact_bytes")
    artifact_sha256 = metadata.get("artifact_sha256")
    staged_device = metadata.get("staged_artifact_device")
    staged_inode = metadata.get("staged_artifact_inode")
    sidecar = metadata.get("artifact_sidecar")
    if (
        isinstance(artifact_bytes, bool)
        or not isinstance(artifact_bytes, int)
        or artifact_bytes <= 0
        or not isinstance(artifact_sha256, str)
        or len(artifact_sha256) != 64
        or isinstance(staged_device, bool)
        or not isinstance(staged_device, int)
        or staged_device < 0
        or isinstance(staged_inode, bool)
        or not isinstance(staged_inode, int)
        or staged_inode <= 0
        or not isinstance(sidecar, dict)
    ):
        raise ValueError("PostgreSQL restore requires verified staged artifact metadata")
    required_sidecar = {
        "postgresql_server_version",
        "postgresql_server_version_num",
        "server_encoding",
        "lc_collate",
        "lc_ctype",
        "rls_table_count",
        "source_identity_sha256",
        "source_catalog_sha256",
        "archive_catalog_sha256",
        "toc_sha256",
        "catalog_counts",
        "validation",
    }
    if not required_sidecar <= set(sidecar):
        raise ValueError("PostgreSQL restore requires complete archive provenance")
    if sidecar.get("validation") != validation:
        raise ValueError("PostgreSQL restore archive validation provenance is invalid")
    if sidecar.get("source_identity_sha256") != _canonical_sha256(source_identity):
        raise ValueError("PostgreSQL restore source provenance did not match")
    if not all(character in "0123456789abcdef" for character in artifact_sha256):
        raise ValueError("PostgreSQL restore artifact hash is invalid")
    for key in ("server_encoding", "lc_collate", "lc_ctype"):
        value = sidecar.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError("PostgreSQL restore archive provenance is invalid")
    rls_table_count = sidecar.get("rls_table_count")
    if isinstance(rls_table_count, bool) or rls_table_count != 0:
        raise ValueError("PostgreSQL restore archive RLS provenance is invalid")
    catalog_counts = sidecar.get("catalog_counts")
    expected_count_keys = {
        "schemas",
        "extensions",
        "relations",
        "sequences",
        "indexes",
        "constraints",
        "routines",
        "types",
        "large_objects",
    }
    if not isinstance(catalog_counts, dict) or set(catalog_counts) != expected_count_keys:
        raise ValueError("PostgreSQL restore archive object counts are invalid")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in catalog_counts.values()
    ):
        raise ValueError("PostgreSQL restore archive object counts are invalid")
    for key in ("source_catalog_sha256", "archive_catalog_sha256", "toc_sha256"):
        value = sidecar.get(key)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or not all(character in "0123456789abcdef" for character in value)
        ):
            raise ValueError("PostgreSQL restore archive provenance is invalid")
    return VerifiedRestoreArtifact(
        size_bytes=artifact_bytes,
        sha256=artifact_sha256,
        device=staged_device,
        inode=staged_inode,
        sidecar=sidecar,
    )


async def _hash_open_file_cancellable(
    descriptor: int,
    *,
    timeout_seconds: float,
) -> str:
    """Hash an already-bound descriptor in a killable subprocess."""
    try:
        process = await asyncio.create_subprocess_exec(
            SHA256SUM,
            "--binary",
            f"/proc/self/fd/{descriptor}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(descriptor,),
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            },
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError("SHA-256 validation tool is unavailable") from exc
    stdout, stderr = await _communicate_with_limits(
        process,
        operation="PostgreSQL artifact hashing",
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=256,
        stderr_limit_bytes=MAX_PROBE_BYTES,
    )
    fields = stdout.decode("ascii", errors="strict").split()
    if (
        process.returncode != 0
        or stderr
        or len(fields) < 1
        or re.fullmatch(r"[0-9a-f]{64}", fields[0]) is None
    ):
        raise RuntimeError("PostgreSQL artifact hashing failed")
    return fields[0]


async def _read_limited_stream(
    stream: asyncio.StreamReader,
    *,
    limit_bytes: int,
    label: str,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await stream.read(min(64 * 1024, limit_bytes + 1 - total)):
        total += len(chunk)
        if total > limit_bytes:
            raise RuntimeError(f"{label} exceeded its safety limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _communicate_with_limits(
    process: asyncio.subprocess.Process,
    *,
    operation: str,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError(f"{operation} did not provide bounded output streams")
    stdout_stream = process.stdout
    stderr_stream = process.stderr

    async def read_and_wait() -> tuple[bytes, bytes]:
        stdout, stderr = await asyncio.gather(
            _read_limited_stream(
                stdout_stream,
                limit_bytes=stdout_limit_bytes,
                label=f"{operation} output",
            ),
            _read_limited_stream(
                stderr_stream,
                limit_bytes=stderr_limit_bytes,
                label=f"{operation} diagnostics",
            ),
        )
        await process.wait()
        return stdout, stderr

    return await run_process_with_timeout(
        process,
        read_and_wait(),
        operation=operation,
        timeout_seconds=timeout_seconds,
    )


async def restore_postgresql_archive(
    target: PostgreSQLTarget,
    pre_restore_identity: PostgreSQLIdentity,
    artifact_path: Path,
    metadata: Mapping[str, object],
    *,
    validation: str = "postgresql-custom-v1",
    restore_sentinel: str = DEFAULT_RESTORE_SENTINEL,
    allowed_unsupported_database_objects: frozenset[str] = frozenset(),
    expected_schema_sha256: str | None = None,
) -> PostgreSQLIdentity:
    """Validate and transactionally restore one staged custom archive descriptor."""
    source_identity = metadata.get("source_database_identity")
    if not isinstance(source_identity, dict):
        raise ValueError("PostgreSQL restore requires exact source database provenance")
    verified_artifact = _require_restore_metadata(
        metadata,
        source_identity,
        validation=validation,
    )
    sidecar = verified_artifact.sidecar
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(artifact_path, flags)
    except FileNotFoundError as exc:
        raise FileNotFoundError("PostgreSQL restore artifact was not found") from exc
    password_file: Path | None = None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (verified_artifact.device, verified_artifact.inode)
            or opened.st_size != verified_artifact.size_bytes
            or await _hash_open_file_cancellable(
                descriptor,
                timeout_seconds=PUBLICATION_TIMEOUT_SECONDS,
            )
            != verified_artifact.sha256
        ):
            raise ValueError("PostgreSQL verified staging identity did not match")
        if (
            expected_schema_sha256 is not None
            and await postgresql_archive_schema_sha256(descriptor) != expected_schema_sha256
        ):
            raise ValueError("PostgreSQL restore archive schema did not match its provenance")
        inspector = await asyncio.create_subprocess_exec(
            PG_RESTORE16,
            "--list",
            f"/proc/self/fd/{descriptor}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(descriptor,),
        )
        toc, inspector_stderr = await _communicate_with_limits(
            inspector,
            operation="PostgreSQL restore archive inspection",
            timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            stdout_limit_bytes=MAX_TOC_BYTES,
            stderr_limit_bytes=MAX_PROBE_BYTES,
        )
        if inspector.returncode != 0 or inspector_stderr:
            raise ValueError("PostgreSQL staged artifact is not a valid custom archive")
        toc_sha256, archive_catalog_sha256 = _validate_archive_toc(
            toc,
            pre_restore_identity,
            require_catalog_match=False,
        )
        if toc_sha256 != sidecar["toc_sha256"]:
            raise ValueError("PostgreSQL restore archive TOC did not match its provenance")
        if archive_catalog_sha256 != sidecar["archive_catalog_sha256"]:
            raise ValueError("PostgreSQL restore archive catalog did not match its provenance")
        if (
            sidecar["postgresql_server_version"] != pre_restore_identity.server_version
            or sidecar["postgresql_server_version_num"] != pre_restore_identity.server_version_num
            or sidecar["server_encoding"] != pre_restore_identity.server_encoding
            or sidecar["lc_collate"] != pre_restore_identity.lc_collate
            or sidecar["lc_ctype"] != pre_restore_identity.lc_ctype
        ):
            raise ValueError("PostgreSQL restore archive version did not match the destination")

        password_file = _password_file(target)
        with tempfile.TemporaryFile(mode="w+b") as error_file:
            process = await asyncio.create_subprocess_exec(
                PG_RESTORE16,
                "-h",
                target.host,
                "-p",
                str(target.port),
                "-U",
                target.user,
                "--dbname",
                target.database,
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                f"/proc/self/fd/{descriptor}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=error_file,
                env=_environment(password_file),
                pass_fds=(descriptor,),
            )
            returncode = await run_process_with_timeout(
                process,
                process.wait(),
                operation="PostgreSQL transactional restore",
                timeout_seconds=RESTORE_TIMEOUT_SECONDS,
            )
            error_file.seek(0)
            diagnostics = error_file.read(MAX_PROBE_BYTES + 1)
        if returncode != 0:
            raise RuntimeError("PostgreSQL pg_restore failed")
        if diagnostics:
            raise RuntimeError("PostgreSQL pg_restore emitted diagnostics")
        restored = await probe_postgresql(
            target,
            expected_state="restored_destination",
            allowed_unsupported_database_objects=allowed_unsupported_database_objects,
            restore_sentinel=restore_sentinel,
        )
        if _expected_archive_catalog_sha256(restored.catalog) != sidecar["source_catalog_sha256"]:
            raise RuntimeError("PostgreSQL restored catalog did not match the archive")
        return restored
    finally:
        os.close(descriptor)
        if password_file is not None:
            password_file.unlink(missing_ok=True)


def authorize_postgresql_restore(
    target: PostgreSQLTarget,
    *,
    source_identity: object,
    source_target_id: str,
    destination_target_id: str,
    restore_allowlist_env: str = RESTORE_ALLOWLIST_ENV,
) -> None:
    """Authorize one create-only PostgreSQL restore without performing I/O."""
    if target.mode != "restore_destination":
        raise ValueError("PostgreSQL restore requires a restore-destination configuration")
    if os.environ.get(ISOLATED_RESTORE_ENV) != "1":
        raise ValueError("PostgreSQL restore is disabled outside an isolated local drill")
    destination = f"{target.host.lower()}:{target.port}/{target.database}"
    raw_allowlist = os.environ.get(restore_allowlist_env, "")
    allowed: set[str] = set()
    for raw_entry in raw_allowlist.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            host_and_port, database = entry.split("/", 1)
            host, port_text = host_and_port.rsplit(":", 1)
            port = int(port_text)
        except (TypeError, ValueError):
            continue
        if host and 1 <= port <= 65535 and database:
            allowed.add(f"{host.lower()}:{port}/{database}")
    if destination not in allowed:
        raise ValueError("PostgreSQL restore destination is not in the exact allowlist")
    if source_target_id == destination_target_id:
        raise ValueError("PostgreSQL restore requires distinct source and destination targets")
    if not isinstance(source_identity, dict) or set(source_identity) != {
        "host",
        "port",
        "database",
        "user",
    }:
        raise ValueError("PostgreSQL restore requires exact source database provenance")
    source_host = source_identity.get("host")
    source_port = source_identity.get("port")
    source_database = source_identity.get("database")
    source_user = source_identity.get("user")
    if (
        not isinstance(source_host, str)
        or not source_host
        or isinstance(source_port, bool)
        or not isinstance(source_port, int)
        or not isinstance(source_database, str)
        or not source_database
        or not isinstance(source_user, str)
        or not source_user
    ):
        raise ValueError("PostgreSQL restore requires exact source database provenance")
    source_database_identity = (source_host.lower(), source_port, source_database)
    destination_database_identity = (target.host.lower(), target.port, target.database)
    if source_database_identity == destination_database_identity:
        raise ValueError("PostgreSQL restore requires a distinct database identity")
