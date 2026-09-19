"""PostgreSQL text: what counts as SQL, what each statement reads/writes/declares, and
which diagnostics leave an analysis complete."""
from __future__ import annotations

import importlib.util
import logging
import unittest

from repolens.analysis import Analysis
from repolens.core.findings import Finding, ToolRun
from repolens.impact.config import Config
from repolens.impact.model import Graph, Node
from repolens.impact.postgres import (
    add_sql,
    add_sql_file,
    looks_like_sql,
    non_postgres_dialect,
    plpgsql_outside_block,
    psql_variables,
    split_statements,
)

HAS_SQLGLOT = importlib.util.find_spec("sqlglot") is not None


class LooksLikeSqlTests(unittest.TestCase):
    def test_sql_shapes_are_sql(self):
        for text in ("SELECT 1", "select now()", "SELECT * FROM users", "SELECT a.id FROM accounts a JOIN b ON a.x = b.x",
                     "  -- leading comment\n SELECT count(*) FROM t", "(SELECT 1) UNION (SELECT 2)",
                     "INSERT INTO t (a) VALUES ($1)", "INSERT INTO archive SELECT * FROM live",
                     "UPDATE accounts SET x = 1", "DELETE FROM sessions", "DELETE FROM t WHERE id = ?",
                     "MERGE INTO target t USING s ON true", "TRUNCATE staging", "TRUNCATE TABLE a, b",
                     "COPY audit_log FROM STDIN", "CREATE TABLE x (id int)", "CREATE UNIQUE INDEX CONCURRENTLY ix ON t (a)",
                     "ALTER TABLE items ENABLE ROW LEVEL SECURITY", "CREATE POLICY own ON items USING (true)",
                     "DROP TABLE IF EXISTS x", "CREATE MATERIALIZED VIEW mv AS SELECT 1",
                     "CREATE OR REPLACE FUNCTION touch() RETURNS trigger AS $$ $$",
                     "WITH x AS (SELECT 1) SELECT * FROM x", "GRANT SELECT ON reports TO analyst",
                     "EXPLAIN ANALYZE SELECT * FROM t"):
            self.assertTrue(looks_like_sql(text), text)

    def test_english_ui_strings_are_not_sql(self):
        for text in ("Delete this item?", "Update your profile now", "Delete the draft?", "Select a file from the list",
                     "Select an item", "Select from the list below", "select-user", "Select one", "Insert into cart",
                     "Update profile", "Delete item", "Create a new account", "Update available!",
                     "Please select from the menu and update your profile", "Delete from cart?", "", "SELECT"):
            self.assertFalse(looks_like_sql(text), text)

    def test_comments_quoted_identifiers_short_aliases_and_other_statement_kinds_are_sql(self):
        for text in ("SELECT * FROM users -- fetch the rows\nWHERE id = $1", "SELECT an.id FROM accounts an",
                     'SELECT "the name" FROM t', "SELECT id /* the id */ FROM orders", "SELECT id FROM orders WHERE note = 'the thing'",
                     "REFRESH MATERIALIZED VIEW order_totals", "REFRESH MATERIALIZED VIEW CONCURRENTLY app.order_totals",
                     "CALL refresh_all()", "VALUES (1, 2)", "TABLE users", "table only invoices"):
            self.assertTrue(looks_like_sql(text), text)
        for text in ("Table settings", "Call support()", "Values (for the team)"):
            self.assertFalse(looks_like_sql(text), text)

    def test_capitalised_statement_verbs_without_sql_punctuation_are_ui_copy(self):
        for text in ("Copy link to clipboard", "Grant access on request", "Delete from history", "Select name from list",
                     "Values (1)", "Select id From users"):
            self.assertFalse(looks_like_sql(text), text)
        for text in ("delete from history", "DELETE FROM history", "Select id, name From users", "Select * From users",
                     "Delete from sessions where id = $1", "COPY t TO STDOUT", "copy t from stdin", "values (1)"):
            self.assertTrue(looks_like_sql(text), text)

    def test_prose_commas_later_sentences_and_copy_without_a_source_are_not_sql(self):
        for text in ("Copy link to clipboard, then share", "Grant access on files, folders", "Select name, email from list",
                     "Delete from history; this cannot be undone", "Truncate history", "Copy text to clipboard (Ctrl+C)",
                     "Copy (1) link", "Delete from history, then retry"):
            self.assertFalse(looks_like_sql(text), text)
        for text in ("COPY t (a, b) FROM '/data/t.csv' WITH (FORMAT csv)", "COPY (SELECT 1) TO STDOUT",
                     "COPY t FROM PROGRAM 'gunzip -c t.gz'", "TRUNCATE history", "Truncate history, sessions;"):
            self.assertEqual(looks_like_sql(text), text != "Truncate history, sessions;", text)


class SplitStatementsTests(unittest.TestCase):
    def test_lines_quotes_comments_and_dollar_bodies(self):
        text = ("-- SELECT * FROM commented_out;\n"
                "CREATE TABLE a (note text DEFAULT 'x;y');\n"
                "/* block; comment */\n"
                "CREATE FUNCTION f() RETURNS trigger AS $body$\nBEGIN\n  UPDATE a SET note = 'z';\n  RETURN NEW;\nEND;\n$body$ LANGUAGE plpgsql;\n"
                "\\connect other\n"
                "COPY a (note) FROM stdin;\nrow;one\n\\.\n"
                "SELECT 1;")
        statements = split_statements(text)
        self.assertEqual([line for line, _ in statements], [2, 4, 11, 14])
        self.assertIn("'x;y'", statements[0][1])
        self.assertIn("RETURN NEW;", statements[1][1])
        self.assertNotIn("commented_out", " ".join(s for _, s in statements))

    def test_a_multi_line_string_and_an_escape_string_do_not_end_statements_early(self):
        text = ("RAISE NOTICE '\nline one;\nline two;\n';\n"
                "SELECT E'it\\'s; fine' AS note;\n"
                "SELECT 2;")
        self.assertEqual([line for line, _ in split_statements(text)], [1, 5, 6])

    def test_begin_atomic_bodies_and_rule_action_lists_are_one_statement(self):
        text = ("CREATE FUNCTION add_line() RETURNS void LANGUAGE sql\nBEGIN ATOMIC\n"
                "  INSERT INTO order_lines VALUES (1);\n  SELECT CASE WHEN true THEN 1 END;\nEND;\n"
                "CREATE RULE audit AS ON INSERT TO orders DO ALSO (INSERT INTO audit VALUES (1); INSERT INTO audit_copy VALUES (2));\n"
                "SELECT 2;")
        statements = split_statements(text)
        self.assertEqual([line for line, _ in statements], [1, 6, 7])
        self.assertTrue(statements[0][1].endswith("END"))
        self.assertIn("audit_copy", statements[1][1])

    def test_a_column_named_end_is_not_the_end_of_an_atomic_body(self):
        text = "CREATE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT t.end FROM t; SELECT 2; END;\nSELECT 3;"
        self.assertEqual([line for line, _ in split_statements(text)], [1, 2])
        self.assertTrue(split_statements(text)[0][1].endswith("END"))

    def test_copy_data_without_a_terminator_runs_to_the_end_of_the_text(self):
        # psql reads the rows to `\.` or to the end of the script: they are never statements.
        self.assertEqual(split_statements("SELECT 0;\nCOPY t FROM stdin;\n1\t2\nSELECT 9;\n"),
                         [(1, "SELECT 0"), (2, "COPY t FROM stdin")])

    def test_psql_meta_commands_are_skipped_and_query_sending_ones_end_the_statement(self):
        text = ("\\set ON_ERROR_STOP on\n"
                "SELECT id FROM orders WHERE id = :'order_id' \\gset\n"
                "SELECT count(*) FROM invoices \\gexec\n"
                "  \\echo done\n"
                "SELECT 1 FROM items \\g\n"
                "SELECT 2;")
        self.assertEqual(split_statements(text), [(2, "SELECT id FROM orders WHERE id = :'order_id'"),
                                                  (3, "SELECT count(*) FROM invoices"), (5, "SELECT 1 FROM items"),
                                                  (6, "SELECT 2")])


    def test_rows_after_a_psql_copy_from_stdin_are_data(self):
        self.assertEqual(split_statements("SELECT 0;\n\\copy t FROM stdin\n1\t2\n\\.\nSELECT 8;\n"),
                         [(1, "SELECT 0"), (5, "SELECT 8")])


class PsqlVariableTests(unittest.TestCase):
    def test_psql_variables_become_placeholders_outside_strings(self):
        self.assertEqual(psql_variables("SELECT :'name', :\"col\", :limit FROM t WHERE a = ':x' AND b::int = 1"),
                         ("SELECT $1, $2, $3 FROM t WHERE a = ':x' AND b::int = 1", True))
        self.assertEqual(psql_variables("SELECT a::text FROM t"), ("SELECT a::text FROM t", False))

    def test_escape_strings_and_array_slices_are_not_variables(self):
        self.assertEqual(psql_variables("SELECT E'it\\'s :x', :v FROM t"), ("SELECT E'it\\'s :x', $1 FROM t", True))
        self.assertEqual(psql_variables("SELECT arr[:hi], arr[1:hi] FROM t"), ("SELECT arr[:hi], arr[1:hi] FROM t", False))


class DialectTests(unittest.TestCase):
    def test_other_dialects_and_templates_are_named(self):
        for text, dialect in (("DECLARE @total int;\nSELECT 1;", "T-SQL"), ("DELIMITER //\nSELECT 1 //", "MySQL"),
                              ("SELECT `id` FROM `orders`;", "MySQL"), ("select * from {{ ref('orders') }}", "Jinja"),
                              ("{% if full %}select 1{% endif %}", "Jinja")):
            self.assertIn(dialect, non_postgres_dialect(text)[0], text)
        for text in ("SELECT 'DECLARE @x' FROM t; -- `quoted`\n", "SELECT to_tsvector(body) @@ q FROM dbo.docs;",
                     "CREATE FUNCTION f() RETURNS text AS $$ SELECT '{{ x }}' $$ LANGUAGE sql;"):
            self.assertIsNone(non_postgres_dialect(text), text)

    def test_valid_postgresql_that_resembles_another_dialect_is_postgresql(self):
        for text in ("COPY t FROM '/x.csv'\n DELIMITER ',' CSV;", "SELECT id,\ngo\nFROM t;", "CREATE TABLE t (autoincrement int);",
                     "SELECT nvarchar(10);", "SELECT top (5) FROM t;", "SELECT a, auto_increment FROM t;",
                     "SELECT id FROM t WHERE engine = other;", "SELECT pragma\nFROM t;", "SELECT id FROM t\nGO"):
            self.assertIsNone(non_postgres_dialect(text), text)
        for text, dialect in (("CREATE TABLE t (id INT IDENTITY(1,1), name NVARCHAR(50));\nGO\n", "T-SQL"),
                              ("SELECT TOP 5 * FROM [dbo].[orders];", "T-SQL"),
                              ("CREATE TABLE t (id int NOT NULL AUTO_INCREMENT, PRIMARY KEY (id)) ENGINE=InnoDB;", "MySQL"),
                              ("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT);", "SQLite"),
                              ("PRAGMA foreign_keys = ON;", "SQLite"), ("CREATE TABLE t (name VARCHAR2(10));", "Oracle")):
            self.assertEqual(non_postgres_dialect(text)[0], dialect, text)

    def test_common_t_sql_scripts_are_named_without_a_second_weak_signal(self):
        for text in ("USE shop;\nGO\nCREATE TABLE dbo.orders (id INT);\nGO\n", "SELECT TOP 5 * FROM orders ORDER BY id DESC;\n",
                     "CREATE TABLE [orders] ([id] INT NOT NULL);\n", "CREATE PROCEDURE p AS\nBEGIN\n  SET NOCOUNT ON;\n  SELECT 1;\nEND\n",
                     "EXEC sp_rename 'orders.total', 'amount', 'COLUMN';\n", "SELECT 1\nGO\nSELECT 2\nGO\n"):
            self.assertEqual(non_postgres_dialect(text)[0], "T-SQL", text)
        for text in ("SELECT arr[1], ARRAY[a, b] FROM t;", "SELECT (items)[1] FROM t;", "SELECT use FROM t;",
                     "EXECUTE sp_plan(1);", "SELECT top (5) FROM t;"):
            self.assertIsNone(non_postgres_dialect(text), text)


class PlpgsqlOutsideBlockTests(unittest.TestCase):
    def test_plpgsql_only_statement_openings_are_named(self):
        for statement, keyword in (("RAISE NOTICE 'x'", "RAISE"), ("  raise exception 'boom'", "RAISE"),
                                   ("PERFORM pg_sleep(1)", "PERFORM"), ("RETURN NEW", "RETURN"),
                                   ("DECLARE total int", "DECLARE"), ("IF found THEN UPDATE t SET a = 1", "IF"),
                                   ("END IF", "END IF"), ("END  LOOP", "END LOOP"), ("LOOP", "LOOP"), ("EXIT WHEN done", "EXIT"),
                                   ("GET DIAGNOSTICS n = ROW_COUNT", "GET DIAGNOSTICS"), ("ASSERT n > 0", "ASSERT"),
                                   ("FOR r IN SELECT * FROM t LOOP", "FOR"), ("-- note\nRAISE NOTICE 'x'", "RAISE")):
            self.assertEqual(plpgsql_outside_block(statement), keyword, statement)

    def test_valid_top_level_sql_is_not_plpgsql(self):
        for statement in ("BEGIN", "END", "COMMIT", "DECLARE c CURSOR FOR SELECT 1", "DECLARE c NO SCROLL CURSOR FOR SELECT 1",
                          "EXECUTE prepared_plan(1)", "FETCH NEXT FROM c", "CLOSE c", "CALL refresh()",
                          "SELECT 'RAISE NOTICE'", "DO $$ BEGIN RAISE NOTICE 'x'; END $$", "SELECT returned FROM t",
                          "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END $$ LANGUAGE plpgsql",
                          "UPDATE t SET if_flag = 1", "SELECT 'IF x THEN'", "DECLARE @total int"):
            self.assertIsNone(plpgsql_outside_block(statement), statement)


@unittest.skipUnless(HAS_SQLGLOT, "install repolens[stack]")
class AddSqlTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph("/repo")
        self.graph.add_node(Node("src", "symbol", "src"))

    def tables(self) -> dict[tuple[str, str], str]:
        """(table, location) -> relationships from the edge detail."""
        out = {}
        for edge in self.graph.edges:
            detail = edge.detail.split("(", 1)[1].split(");", 1)[0]
            out[(self.graph.nodes[edge.target].label, edge.evidence)] = detail
        return out

    def names(self) -> dict[str, str]:
        return {name: relationship for (name, _), relationship in self.tables().items()}

    def codes(self) -> list[tuple[str, str]]:
        return [(issue.code, issue.severity) for issue in self.graph.issues]

    def complete(self) -> bool:
        return Analysis(self.graph, [], Config()).complete

    def test_rls_policy_and_trigger_ddl_declare_tables_and_leave_the_analysis_complete(self):
        text = ("CREATE TABLE items (id int);\n"
                "ALTER TABLE items ENABLE ROW LEVEL SECURITY;\n"
                "CREATE POLICY own ON items USING (true);\n"
                "CREATE TRIGGER t BEFORE UPDATE ON public.customers FOR EACH ROW EXECUTE FUNCTION touch();\n"
                "ALTER TABLE IF EXISTS ONLY Things FORCE ROW LEVEL SECURITY;\n")
        with self.assertNoLogs("sqlglot", level=logging.WARNING):
            add_sql_file(self.graph, "src", text, "db/rls.sql")
        self.assertEqual(self.tables(), {("items", "db/rls.sql:1"): "declares", ("items", "db/rls.sql:2"): "declares",
                                         ("items", "db/rls.sql:3"): "declares",
                                         ("public.customers", "db/rls.sql:4"): "declares",
                                         ("things", "db/rls.sql:5"): "declares"})
        self.assertEqual(set(self.codes()), {("SQL_UNSUPPORTED_STATEMENT", "info")})
        self.assertTrue(self.complete())

    def test_gated_prose_is_ignored_and_unparseable_gated_sql_is_only_info(self):
        for prose in ("Delete this item?", "Update your profile now", "Select a file from the list"):
            add_sql(self.graph, "src", prose, "ui.py:1", gated=True)
        self.assertEqual(self.graph.issues, [])
        add_sql(self.graph, "src", "SELECT * FROM WHERE", "ui.py:2", gated=True)
        self.assertEqual(self.codes(), [("SQL_NOT_PARSED", "info")])
        self.assertTrue(self.complete())

    def test_a_broken_statement_in_a_sql_file_is_a_located_gap_and_keeps_its_neighbours(self):
        add_sql_file(self.graph, "src", "CREATE TABLE good_before (id int);\nCREATE TABLE (;\nCREATE TABLE good_after (id int);\n",
                     "db/broken_mid.sql")
        self.assertEqual(set(self.tables()), {("good_before", "db/broken_mid.sql:1"), ("good_after", "db/broken_mid.sql:3")})
        self.assertEqual([(i.code, i.evidence) for i in self.graph.issues], [("SQL_PARSE_ERROR", "db/broken_mid.sql:2")])
        self.assertFalse(self.complete())

    def test_a_comparison_as_a_generated_column_expression_parses(self):
        # sqlglot 28-30 rejects `GENERATED ALWAYS AS (s >= 0.995)`; valid PostgreSQL
        # must not become a SQL_PARSE_ERROR that marks the whole analysis incomplete.
        text = ("CREATE TABLE ai.edit_feedback (\n  similarity numeric(5,4),\n"
                "  accepted boolean GENERATED ALWAYS AS (similarity >= 0.995) STORED,\n"
                "  note text DEFAULT 'GENERATED ALWAYS AS (x' \n);\n")
        add_sql_file(self.graph, "src", text, "db/gen.sql")
        self.assertEqual(self.tables(), {("ai.edit_feedback", "db/gen.sql:1"): "declares"})
        self.assertEqual(self.codes(), [])
        self.assertTrue(self.complete())

    def test_merge_truncate_grant_and_copy_are_recorded(self):
        add_sql(self.graph, "src", "MERGE INTO target t USING source s ON t.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v", "q.py:1")
        add_sql(self.graph, "src", "TRUNCATE staging", "q.py:2")
        add_sql(self.graph, "src", "GRANT SELECT ON reports TO analyst", "q.py:3")
        add_sql(self.graph, "src", "GRANT SELECT, INSERT ON inventory, reservations TO analyst", "q.py:4")
        add_sql(self.graph, "src", "COPY audit_log FROM STDIN", "q.py:5")
        add_sql(self.graph, "src", "COPY exports TO STDOUT", "q.py:6")
        self.assertEqual(self.names(), {"target": "writes", "source": "reads", "staging": "writes", "reports": "declares",
                                        "inventory": "declares", "reservations": "declares", "audit_log": "writes", "exports": "reads"})
        self.assertTrue(self.complete())

    def test_a_statement_kind_that_is_not_modelled_is_reported_not_dropped(self):
        add_sql(self.graph, "src", "VACUUM foo", "q.py:1")
        add_sql(self.graph, "src", "SELECT 1", "q.py:2")
        add_sql(self.graph, "src", "SET search_path TO app", "q.py:3")
        self.assertEqual([(i.code, i.severity, i.evidence) for i in self.graph.issues],
                         [("SQL_UNSUPPORTED_STATEMENT", "info", "q.py:1")])
        self.assertTrue(self.complete())

    def test_a_data_modifying_cte_writes_its_target_and_its_alias_is_not_a_table(self):
        add_sql(self.graph, "src", "WITH moved AS (DELETE FROM queue RETURNING *) INSERT INTO done SELECT * FROM moved", "q.py:1")
        self.assertEqual(self.names(), {"queue": "writes", "done": "writes"})

    def test_a_function_signature_is_not_a_table_and_its_body_is_read(self):
        add_sql_file(self.graph, "src",
                     "CREATE OR REPLACE FUNCTION touch() RETURNS trigger AS $$\nBEGIN\n"
                     "  IF NEW.id IS NOT NULL THEN\n    UPDATE public.customers SET updated_at = now() WHERE id = NEW.id;\n  END IF;\n"
                     "  RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql;\n"
                     "CREATE INDEX ix_a ON accounts (email);\n", "db/schema.sql")
        self.assertEqual(self.tables(), {("public.customers", "db/schema.sql:1"): "writes in function body",
                                         ("accounts", "db/schema.sql:9"): "declares"})

    def test_unquoted_names_fold_to_lower_case_and_quoted_names_keep_theirs(self):
        add_sql(self.graph, "src", 'SELECT * FROM Users', "q.py:1")
        add_sql(self.graph, "src", 'SELECT * FROM users', "q.py:2")
        add_sql(self.graph, "src", 'SELECT * FROM "Sales"."Orders"', "q.py:3")
        self.assertEqual({n.label for n in self.graph.nodes.values() if n.kind == "postgres_table"}, {"users", "Sales.Orders"})

    def test_several_relationships_at_one_location_share_one_edge(self):
        add_sql(self.graph, "src", "CREATE TABLE a (id int); INSERT INTO a VALUES (1)", "q.py:1")
        add_sql(self.graph, "src", "SELECT * FROM a", "q.py:1")
        self.assertEqual(len(self.graph.edges), 1)
        self.assertEqual(self.tables(), {("a", "q.py:1"): "declares, reads, writes"})

    def test_parameterized_values_are_read_and_a_substituted_name_is_dynamic(self):
        add_sql(self.graph, "src", "SELECT * FROM accounts WHERE id = $1", "q.ts:1", parameterized=True)
        self.assertEqual(self.names(), {"accounts": "reads"})
        for location, sql in (("q.ts:2", "SELECT * FROM $1"), ("q.ts:3", "SELECT * FROM app.$1"),
                              ("q.ts:4", "UPDATE t SET $1 = 2"), ("q.ts:5", "SELEC broken $1")):
            add_sql(self.graph, "src", sql, location, parameterized=True)
        self.assertEqual(self.names(), {"accounts": "reads"})
        self.assertEqual([i.code for i in self.graph.issues], ["DYNAMIC_SQL"] * 4)
        self.assertTrue(self.complete())

    def test_an_optimizer_failure_degrades_to_the_table_walk(self):
        # sqlglot's scope analysis raised "Alias already used: t" on `FOR UPDATE OF t`,
        # which failed the whole .ts file (FILE_SCAN_FAILED) and dropped every table in it.
        sql = ("SELECT t.id FROM invoices t WHERE t.id IN ( SELECT $1::text UNION ALL "
               "SELECT l.source_invoice_id FROM invoice_links l WHERE l.destination_invoice_id = $2 "
               "UNION ALL SELECT x.linked_invoice_id FROM invoices x WHERE x.id = $3 ) ORDER BY t.id FOR UPDATE OF t")
        add_sql(self.graph, "src", sql, "x.ts:1", gated=True, parameterized=True)
        self.assertEqual(self.names(), {"invoices": "reads", "invoice_links": "reads"})
        self.assertEqual(self.graph.issues, [])

    def test_plpgsql_after_commit_is_a_script_error_not_a_parse_gap(self):
        text = ("BEGIN;\nUPDATE accounts SET balance = -balance WHERE balance > 0;\nCOMMIT;\n\n"
                "RAISE NOTICE '\n=====\nDONE; next steps:\n1. SELECT * FROM progress;\n=====\n';\n"
                "SELECT 1 FROM after_notice;\n")
        add_sql_file(self.graph, "src", text, "db/fix.sql")
        issues = [(i.code, i.severity, i.evidence) for i in self.graph.issues]
        self.assertEqual(issues, [("SQL_PLPGSQL_OUTSIDE_BLOCK", "warning", "db/fix.sql:5")])
        self.assertIn("RAISE", self.graph.issues[0].message)
        self.assertIn("DO $$ BEGIN ... END $$;", self.graph.issues[0].recommendation)
        self.assertEqual(set(self.tables()), {("accounts", "db/fix.sql:2"), ("after_notice", "db/fix.sql:11")})
        self.assertTrue(self.complete())

    def test_sql_shaped_text_the_gate_rejects_is_reported_not_dropped(self):
        add_sql(self.graph, "src", "UPDATE orders o JOIN order_lines l ON l.order_id = o.id SET o.total = 1", "q.ts:1", gated=True)
        self.assertEqual(self.codes(), [("SQL_NOT_PARSED", "info")])
        self.assertEqual(self.graph.edges, [])
        add_sql(self.graph, "src", "SELECT * FROM users -- fetch the rows\nWHERE id = $1", "q.ts:2", gated=True)
        add_sql(self.graph, "src", "SELECT an.id FROM accounts an", "q.ts:3", gated=True)
        self.assertEqual(self.names(), {"users": "reads", "accounts": "reads"})
        self.assertTrue(self.complete())

    def test_text_the_command_fallback_accepts_is_a_parse_error_unless_it_is_a_known_statement(self):
        add_sql_file(self.graph, "src", "ALTER TABLE orders ADD COLUMN (;\nGRANT SELECT ON orders TO ;\nFOOBAR orders;\n"
                                        "ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;\nGRANT SELECT ON items TO analyst;\n",
                     "db/mixed.sql")
        self.assertEqual([(i.code, i.evidence) for i in self.graph.issues],
                         [("SQL_PARSE_ERROR", "db/mixed.sql:1"), ("SQL_PARSE_ERROR", "db/mixed.sql:2"),
                          ("SQL_PARSE_ERROR", "db/mixed.sql:3"), ("SQL_UNSUPPORTED_STATEMENT", "db/mixed.sql:4")])
        self.assertEqual(self.names(), {"invoices": "declares", "items": "declares"})
        self.assertFalse(self.complete())

    def test_a_statement_the_parser_raises_a_non_parse_error_on_stays_local(self):
        # sqlglot raises ValueError ("Cannot convert empty name into var") on `GRANT;`,
        # which escaped `add_sql` and failed the whole file, losing `b`.
        add_sql_file(self.graph, "src", "CREATE TABLE a(id int); GRANT; CREATE TABLE b(id int);", "db/grant.sql")
        self.assertEqual([i.code for i in self.graph.issues], ["SQL_PARSE_ERROR"])
        self.assertEqual(set(self.names()), {"a", "b"})
        for text in ("REVOKE;", "GRANT ON t TO x;"):
            add_sql_file(self.graph, "src", text, "db/bad.sql")
        self.assertEqual([i.code for i in self.graph.issues], ["SQL_PARSE_ERROR"] * 3)
        add_sql(self.graph, "src", "DO $$ BEGIN GRANT; END $$", "q.py:1")
        add_sql(self.graph, "src", "GRANT;", "q.py:2", gated=True)
        self.assertEqual(set(self.names()), {"a", "b"})

    def test_a_file_in_another_dialect_is_one_info_diagnostic(self):
        for name, text in (("tsql", "DECLARE @total int;\nIF @total > 0 BEGIN SELECT 1 END;\nRAISERROR('x', 16, 1);\n"),
                           ("mysql", "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END //\n"),
                           ("backticks", "SELECT `id` FROM `orders`;\nRAISE NOTICE 'x';\n"),
                           ("dbt", "select * from {{ ref('orders') }}\n"), ("jinja", "{% if full %}\nselect 1;\n{% endif %}\n")):
            add_sql_file(self.graph, "src", text, f"db/{name}.sql")
        self.assertEqual({(i.code, i.severity) for i in self.graph.issues}, {("SQL_DIALECT_NOT_POSTGRES", "info")})
        self.assertEqual(len(self.graph.issues), 5)
        self.assertEqual(self.graph.edges, [])
        self.assertTrue(self.complete())

    def test_begin_atomic_bodies_and_rule_actions_keep_their_tables(self):
        add_sql_file(self.graph, "src",
                     "CREATE FUNCTION add_line() RETURNS void LANGUAGE sql BEGIN ATOMIC INSERT INTO order_lines VALUES (1); "
                     "SELECT 1 FROM orders; END;\n"
                     "CREATE RULE audit AS ON INSERT TO orders DO ALSO (INSERT INTO audit VALUES (1); INSERT INTO audit_copy VALUES (2));\n",
                     "db/rules.sql")
        self.assertEqual(self.tables(), {("order_lines", "db/rules.sql:1"): "writes in function body",
                                         ("orders", "db/rules.sql:1"): "reads in function body",
                                         ("orders", "db/rules.sql:2"): "declares",
                                         ("audit", "db/rules.sql:2"): "writes in rule action",
                                         ("audit_copy", "db/rules.sql:2"): "writes in rule action"})
        self.assertEqual({i.code for i in self.graph.issues}, {"SQL_UNSUPPORTED_STATEMENT"})

    def test_a_rule_event_keyword_is_never_a_table(self):
        add_sql_file(self.graph, "src", "CREATE RULE r AS ON UPDATE TO orders DO INSTEAD NOTHING;\n"
                                        "CREATE OR REPLACE RULE r2 AS ON DELETE TO ONLY invoices DO ALSO NOTIFY invoices;\n",
                     "db/rules.sql")
        self.assertEqual(self.names(), {"orders": "declares", "invoices": "declares"})

    def test_psql_variables_and_meta_commands_leave_statements_readable(self):
        add_sql_file(self.graph, "src", "\\set ON_ERROR_STOP on\nSELECT id FROM orders WHERE id = :'order_id' \\gset\n"
                                        "SELECT * FROM :\"table_name\";\nSELECT :columns FROM invoices;\n\\echo done\n",
                     "db/report.sql")
        self.assertEqual(self.tables(), {("orders", "db/report.sql:2"): "reads", ("invoices", "db/report.sql:4"): "reads"})
        self.assertEqual([(i.code, i.evidence) for i in self.graph.issues], [("DYNAMIC_SQL", "db/report.sql:3")])

    def test_a_psql_statement_that_does_not_parse_is_a_parse_error_not_dynamic_sql(self):
        add_sql_file(self.graph, "src", "SELECT id FROM orders WHERE id = :id AND (;\nSELECT * FROM :\"table_name\";\n", "db/r.sql")
        self.assertEqual([(i.code, i.evidence) for i in self.graph.issues],
                         [("SQL_PARSE_ERROR", "db/r.sql:1"), ("DYNAMIC_SQL", "db/r.sql:2")])

    def test_a_failure_in_the_readers_own_helper_is_not_reported_as_the_targets_parse_error(self):
        from unittest import mock
        with mock.patch("repolens.impact.postgres._one_statement_the_parser_splits", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                add_sql(self.graph, "src", "SELECT 1 FROM orders", "q.py:1")
        self.assertEqual(self.graph.issues, [])

    def test_plpgsql_loops_keep_the_tables_after_the_first_statement_keyword(self):
        add_sql_file(self.graph, "src", "CREATE FUNCTION copy_rows() RETURNS void AS $$\nDECLARE r record;\nBEGIN\n"
                                        "  FOR r IN SELECT * FROM inventory LOOP\n    UPDATE inventory_totals SET qty = r.qty;\n"
                                        "  END LOOP;\nEND $$ LANGUAGE plpgsql;\n"
                                        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM carts) THEN DELETE FROM cart_items; END IF; END $$;\n",
                     "db/loops.sql")
        self.assertEqual(self.tables(), {("inventory", "db/loops.sql:1"): "reads in function body",
                                         ("inventory_totals", "db/loops.sql:1"): "writes in function body",
                                         ("carts", "db/loops.sql:8"): "reads in function body",
                                         ("cart_items", "db/loops.sql:8"): "writes in function body"})

    def test_dynamic_keeps_its_meaning(self):
        add_sql(self.graph, "src", "", "q.py:1", dynamic=True)
        self.assertEqual((self.codes(), self.graph.edges), ([("DYNAMIC_SQL", "info")], []))


class CompletenessTests(unittest.TestCase):
    def test_a_could_not_scan_finding_makes_the_analysis_incomplete(self):
        graph = Graph("/repo")
        clean = ToolRun("security", findings=[Finding("security", "security/sql-injection", "high", "m")])
        self.assertTrue(Analysis(graph, [clean], Config()).complete)
        hole = ToolRun("performance", findings=[Finding("performance", "performance/could-not-scan", "info", "m")])
        self.assertFalse(Analysis(graph, [clean, hole], Config()).complete)


if __name__ == "__main__":
    unittest.main()
