"""Collations that behave the same whatever locale the database was created with."""

# PostgreSQL's builtin provider (PG 17+) maps case for all of Unicode in every
# UTF-8 database. Under the "C" locale, which pgEdge images use, the default
# collation folds only ASCII, so ILIKE and lower() leave Cyrillic alone.
UNICODE = "pg_unicode_fast"
