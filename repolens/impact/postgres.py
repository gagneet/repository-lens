"""PostgreSQL syntax references, never a live schema or query-plan assertion."""
from __future__ import annotations

import contextlib
import logging
import re
from typing import Iterator

from ..core.files import is_test_path
from .model import Edge, Graph, Issue, Node, stable_id


# ── is this text SQL at all? ─────────────────────────────────────────────────────
# Callers that pick SQL out of source code (a string passed to `.execute`/`.query`)
# see UI copy as often as queries: `log.query("Update your profile now")`. A keyword
# test sent that to the parser, and its SQL_PARSE_ERROR marked the whole analysis
# incomplete. The shapes below require the grammar AROUND the keyword, not the keyword.
_NAME = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
_IDENT = rf"{_NAME}(?:\s*\.\s*{_NAME}){{0,2}}"
# Determiners and pronouns: English puts them after a verb, SQL rarely names a table or
# column with them. `a` is left out because it is the most common table alias. `an`
# is an alias too (`FROM accounts an WHERE`), so a determiner followed by a SQL clause
# keyword is not prose. Only the opening tokens are tested: a long query may use any
# word further on, and a sentence gives itself away early.
_PROSE = re.compile(r"\b(?:the|this|that|these|those|your|my|our|their|an|please|here|there)\s+(?P<next>[A-Za-z]\w*)", re.I)
_CLAUSE_WORDS = frozenset({
    "where", "join", "inner", "left", "right", "full", "cross", "natural", "lateral", "on", "using", "set", "group",
    "order", "limit", "offset", "having", "window", "union", "intersect", "except", "returning", "for", "as", "from",
    "values", "select", "into", "and", "or", "not", "is", "in", "fetch", "tablesample", "default", "with",
})
_OPENING_TOKENS = 8
# SQL does not end a word with `?` or `!`; a sentence does ("Delete the draft?").
# A placeholder is `= ?`/`(?)`, never glued to a word.
_SENTENCE_END = re.compile(r"[A-Za-z][?!.]\s*$")
_OBJECT_KINDS = (r"(?:MATERIALIZED\s+VIEW|FOREIGN\s+TABLE|TABLE|INDEX|VIEW|SCHEMA|FUNCTION|PROCEDURE|POLICY|TRIGGER|"
                 r"TYPE|SEQUENCE|EXTENSION|DOMAIN|ROLE|RULE|AGGREGATE|DATABASE|PUBLICATION|SUBSCRIPTION)")
_SQL_SHAPES = [re.compile(pattern, re.I | re.S) for pattern in (
    rf"^INSERT\s+INTO\s+{_IDENT}\s*(?:\(|(?:AS\s+{_NAME}\s+)?(?:VALUES|SELECT|DEFAULT\s+VALUES|OVERRIDING|WITH)\b)",
    rf"^UPDATE\s+(?:ONLY\s+)?{_IDENT}(?:\s+(?:AS\s+)?{_NAME})?\s+SET\b",
    rf"^DELETE\s+FROM\s+(?:ONLY\s+)?{_IDENT}(?:\s|;|$)",
    rf"^MERGE\s+INTO\s+{_IDENT}",
    rf"^TRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?{_IDENT}\s*(?:,|;|$|(?:CASCADE|RESTRICT|RESTART|CONTINUE)\b)",
    # "Copy text to clipboard": COPY moves rows to or from a file, STDIN/STDOUT or a program.
    rf"^COPY\s+(?:\(.+\)\s*|{_IDENT}\s*(?:\([^)]*\)\s*)?)(?:FROM|TO)\s+(?:''|(?:STDIN|STDOUT|PROGRAM)\b)",
    rf"^(?:CREATE(?:\s+OR\s+REPLACE)?(?:\s+(?:TEMP|TEMPORARY|UNLOGGED|GLOBAL|LOCAL|UNIQUE|RECURSIVE|CONSTRAINT))*|ALTER|DROP)"
    rf"\s+{_OBJECT_KINDS}\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+|CONCURRENTLY\s+|ONLY\s+)*(?:{_IDENT}|ON\b)",
    rf"^WITH\s+(?:RECURSIVE\s+)?{_NAME}\s*(?:\([^)]*\)\s*)?AS\s+(?:NOT\s+)?(?:MATERIALIZED\s+)?\(",
    r"^(?:GRANT|REVOKE)\s+[\w\s,()]+?\s+ON\s+\S",
    rf"^COMMENT\s+ON\s+\w+\s+{_IDENT}",
    rf"^REFRESH\s+MATERIALIZED\s+VIEW\s+(?:CONCURRENTLY\s+)?{_IDENT}\s*(?:;|$|WITH\b)",
    # "Call support()" is UI copy: like `TABLE`, only an all-upper or all-lower CALL counts.
    rf"^(?-i:CALL|call)\s+{_IDENT}\s*\(",
    r"^VALUES\s*\(",
)]
# `TABLE users` is `SELECT * FROM users`. A capitalised "Table settings" is a heading,
# so only an all-upper or all-lower keyword counts.
_TABLE_SHORTHAND = re.compile(rf"^(?:TABLE|table)\s+(?:ONLY\s+|only\s+)?{_IDENT}\s*\*?\s*(?:;|$)", re.S)
# `SELECT … FROM <table>`: the select list is an expression list, not words.
_SELECT_FROM = re.compile(rf"^SELECT\s+(?P<list>.+?)\s+FROM\s+(?:ONLY\s+)?(?:\(|{_IDENT})", re.I | re.S)
# `SELECT 1`, `SELECT now()`, `SELECT $1::int`, `SELECT count(*)`: an expression that
# cannot be prose. A lone bare word (`Select one`) is not accepted.
_SELECT_EXPR = re.compile(rf"^SELECT\s+(?:DISTINCT\s+)?(?:[\d'$(*:-]|{_NAME}\s*[.(]|(?:NULL|TRUE|FALSE|CURRENT_\w+)\b)", re.I)


def _strip_leading(text: str) -> str:
    """Leading whitespace, comments, `(` and EXPLAIN do not change what a statement is."""
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"^(?:\s+|--[^\n]*|/\*.*?\*/|\()", "", text, flags=re.S)
        text = re.sub(r"^EXPLAIN\s+(?:\([^)]*\)\s*|ANALY[SZ]E\s+|VERBOSE\s+)*(?=[A-Za-z])", "", text, flags=re.I)
    return text


def _without_quoted(text: str) -> str:
    return re.sub(r"'(?:[^']|'')*'|\$((?:[A-Za-z_]\w*)?)\$.*?\$\1\$", "''", text, flags=re.S)


# String literals, dollar-quoted bodies, quoted identifiers and comments, in one pass so
# a quote inside a comment (`-- it's`) or a `--` inside a string is not misread.
_LEXEMES = re.compile(r"(?P<s>'(?:[^']|'')*')|(?P<d>\$(?P<tag>(?:[A-Za-z_]\w*)?)\$.*?\$(?P=tag)\$)"
                      r"|(?P<i>\"(?:[^\"]|\"\")+\")|(?P<c>--[^\n]*|/\*.*?\*/)", re.S)


def _normalised(text: str) -> str:
    """Strings become `''`, quoted identifiers `_q`, comments a space: the grammar is left."""
    def swap(match: re.Match) -> str:
        if match.group("s") is not None or match.group("d") is not None:
            return "''"
        return "_q" if match.group("i") is not None else " "
    return _LEXEMES.sub(swap, text)


def _mask(text: str) -> str:
    """`text` with every string, quoted identifier and comment blanked, offsets and newlines kept."""
    return _LEXEMES.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)


def _prose(text: str) -> bool:
    opening = " ".join(text.split()[:_OPENING_TOKENS])
    return any(match.group("next").lower() not in _CLAUSE_WORDS for match in _PROSE.finditer(opening))


_STATEMENT_VERB = re.compile(r"^(?:SELECT|INSERT|UPDATE|DELETE|WITH|MERGE|CREATE|ALTER|DROP|TRUNCATE|COPY|GRANT|REVOKE)\b", re.I)
_CLAUSE_KEYWORD = re.compile(r"\b(FROM|WHERE|JOIN|INTO|VALUES|SET|RETURNING|GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|TABLE|ON\s+CONFLICT)\b", re.I)


def _sql_like(text: str) -> bool:
    """Looser than `looks_like_sql`: a statement verb and two clause keywords, no prose.

    Text this accepts and the gate rejects is reported (SQL_NOT_PARSED), not dropped."""
    body = _normalised(_strip_leading(text[:20_000])).split(";", 1)[0].strip()
    if not _STATEMENT_VERB.match(body) or _prose(body) or _SENTENCE_END.search(body):
        return False
    return len({re.sub(r"\s+", " ", match.group(1).upper()) for match in _CLAUSE_KEYWORD.finditer(body)}) >= 2


#: Statement verbs that open UI copy as often as SQL ("Copy link", "Grant access").
_CASED_VERBS = frozenset({"COPY", "GRANT", "DELETE", "SELECT", "VALUES", "TRUNCATE"})
_SQL_PUNCTUATION = re.compile(r"""[=(*'"]|\$\d""")
# A comma is prose punctuation too ("Copy link, then share"): beside a capitalised verb it
# counts only with a clause keyword that is not written in lower case ("Select id, name From users").
_CASED_CLAUSE = re.compile(r"\b(?:FROM|From|ON|On|TO|To|WHERE|Where|INTO|Into|SET|Set)\b")


def looks_like_sql(text: str) -> bool:
    """True only for SQL-shaped text: grammar around the keyword, not the keyword alone.

    `SELECT 1` and `DELETE FROM sessions` are SQL; "Select an item" and "Delete this
    item?" are not. Only the first statement's opening is examined. Comments, string
    literals and quoted identifiers are removed first, so their words are not prose."""
    if not isinstance(text, str):
        return False
    body = _strip_leading(text[:20_000])
    if not body:
        return False
    first = _normalised(body).split(";", 1)[0].strip()
    if not first or _prose(first) or _SENTENCE_END.search(first):
        return False
    if (verb := re.match(r"[A-Za-z]+", first)) and verb.group(0).upper() in _CASED_VERBS \
            and not (verb.group(0).isupper() or verb.group(0).islower()):
        # "Copy link to clipboard", "Select name from list", "Values (1)": a capitalised
        # verb is a sentence unless its first statement carries SQL punctuation. VALUES'
        # own `(` is not, and text after a `;` ("Delete from history; this cannot be undone") is not either.
        rest = first[verb.end():]
        if verb.group(0).upper() == "VALUES" or not (
                _SQL_PUNCTUATION.search(rest) or ("," in rest and _CASED_CLAUSE.search(rest))):
            return False
    if _TABLE_SHORTHAND.match(first):
        return True
    if re.match(r"^SELECT\b", first, re.I):
        if match := _SELECT_FROM.match(first):
            # A select list that is only bare words with no punctuation reads as prose
            # ("Select item from list") only when it has more than one word.
            words = match.group("list").split()
            if len(words) > 1 and all(re.fullmatch(r"[A-Za-z_]\w*", w) for w in words) \
                    and not any(w.upper() in {"DISTINCT", "AS", "ALL"} for w in words):
                return False
            return True
        return bool(_SELECT_EXPR.match(first))
    return any(shape.match(first) for shape in _SQL_SHAPES)


# ── splitting a .sql file ────────────────────────────────────────────────────────
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")
_PSQL_META = re.compile(r"\\([A-Za-z]+|[!?])")
#: psql meta-commands that send the query buffer, ending the statement without a `;`.
_PSQL_SENDS = frozenset({"g", "gx", "gset", "gexec", "gdesc"})
_ROUTINE_START = re.compile(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b", re.I)
_RULE_START = re.compile(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?RULE\b", re.I)
# `t.end` and `t.case` are columns, not the keywords.
_ATOMIC_WORDS = re.compile(r"(?<!\.)(?<!\.\s)\b(BEGIN\s+ATOMIC|CASE|END)\b", re.I)


def split_statements(text: str) -> list[tuple[int, str]]:
    """`(line, statement)` for each `;`-terminated statement, 1-based line of its first token.

    Quoted strings, quoted identifiers and dollar-quoted bodies (`$$`, `$tag$`) are kept
    whole, so a function body's own `;` does not end the CREATE FUNCTION. So is a
    SQL-standard body (`BEGIN ATOMIC … END`, CASE … END counted inside it) and a rule's
    parenthesised action list (`DO ALSO (INSERT …; INSERT …)`). Comments are dropped
    (newlines kept). A psql meta-command (`\\connect`, `\\set`) is skipped to the end of
    its line wherever it appears, and `\\g`/`\\gset`/`\\gexec` end the statement they
    send. The inline data after `COPY … FROM stdin;` in a pg_dump file is skipped up to
    its `\\.` terminator (or the end of the text, as psql reads it), because those rows
    are data, not statements. `t.end` inside a BEGIN ATOMIC body is a column, not END."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start_line = 0
    line = 1
    i, n = 0, len(text)
    depth = 0  # parentheses outside quotes and comments
    kind: str | None = None  # "routine", "rule" or "" once the statement's first `;` is seen
    atomic_open = False
    atomic_level = 0
    scanned = 0  # `buf` index up to which BEGIN ATOMIC / CASE / END were counted

    def flush() -> None:
        nonlocal buf, start_line, depth, kind, atomic_open, atomic_level, scanned
        statement = "".join(buf).strip()
        if statement:
            out.append((start_line, statement))
        buf = []
        start_line = 0
        depth, kind, atomic_open, atomic_level, scanned = 0, None, False, 0, 0

    def mark() -> None:
        nonlocal start_line
        if not start_line:
            start_line = line

    def continues() -> bool:
        """Is this `;` inside a BEGIN ATOMIC body or a rule's action list?"""
        nonlocal kind, atomic_open, atomic_level, scanned
        if kind is None:
            so_far = "".join(buf)
            kind = "routine" if _ROUTINE_START.match(so_far) else "rule" if _RULE_START.match(so_far) else ""
        if kind == "routine":
            # Only the text since the last `;` is new; each chunk is counted once.
            for word in _ATOMIC_WORDS.finditer(_mask("".join(buf[scanned:]))):
                token = word.group(1).upper()
                if token.startswith("BEGIN"):
                    atomic_open, atomic_level = True, atomic_level + 1
                elif atomic_open:
                    atomic_level += 1 if token == "CASE" else -1
            scanned = len(buf)
            return atomic_open and atomic_level > 0
        return kind == "rule" and depth > 0

    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            buf.append(c)
            i += 1
            continue
        if c == "\\" and (meta := _PSQL_META.match(text, i)):
            end = text.find("\n", i)
            command = text[i:n if end < 0 else end]
            i = n if end < 0 else end
            if meta.group(1) in _PSQL_SENDS:
                flush()
            elif meta.group(1).lower() == "copy" and re.search(r"\bFROM\s+P?STDIN\b", command, re.I):
                # `\copy t FROM stdin`: the rows that follow are data, as after COPY … FROM stdin.
                flush()
                terminator = re.compile(r"^\\\.\s*$", re.M).search(text, i)
                stop = terminator.end() if terminator else n
                line += text.count("\n", i, stop)
                i = stop
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            newlines = text.count("\n", i, j)
            line += newlines
            buf.append("\n" * newlines or " ")
            i = j
            continue
        if c in "'\"":
            mark()
            j = i + 1
            # `E'it\'s'`: an escape string literal, where a backslash escapes the quote.
            escapes = c == "'" and i and text[i - 1] in "eE" and not (i > 1 and (text[i - 2].isalnum() or text[i - 2] == "_"))
            while j < n:
                if escapes and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    if text.startswith(c * 2, j):
                        j += 2
                        continue
                    break
                j += 1
            chunk = text[i:j + 1]
            line += chunk.count("\n")
            buf.append(chunk)
            i = j + 1
            continue
        if c == "$" and (tag := _DOLLAR_TAG.match(text, i)) and not (i and (text[i - 1].isalnum() or text[i - 1] == "_")):
            mark()
            end = text.find(tag.group(0), tag.end())
            j = n if end < 0 else end + len(tag.group(0))
            chunk = text[i:j]
            line += chunk.count("\n")
            buf.append(chunk)
            i = j
            continue
        if c == ";":
            if continues():
                buf.append(c)
                i += 1
                continue
            statement = "".join(buf)
            flush()
            i += 1
            if re.match(r"\s*COPY\b.*\bFROM\s+STDIN\b", statement, re.I | re.S):
                terminator = re.compile(r"^\\\.\s*$", re.M).search(text, i)
                # psql reads the rows up to `\.` or, without one, to the end of the script:
                # what follows is data either way, never statements.
                stop = terminator.end() if terminator else n
                line += text.count("\n", i, stop)
                i = stop
            continue
        if not c.isspace():
            mark()
        if c == "(":
            depth += 1
        elif c == ")" and depth:
            depth -= 1
        buf.append(c)
        i += 1
    flush()
    return out


# ── lexical recovery for statements the parser keeps as text ─────────────────────
def _name(identifier: str) -> str:
    """PostgreSQL folds unquoted names to lower case; quoted names keep their case."""
    parts = re.findall(r'"((?:[^"]|"")+)"|([A-Za-z_][\w$]*)', identifier)
    return ".".join(quoted.replace('""', '"') if quoted else bare.lower() for quoted, bare in parts)


#: Words that are never a table name where the grammar below expects one. A pattern that
#: lands on one (`CREATE RULE r AS ON UPDATE TO t` read as table `update`) misread the grammar.
_NOT_TABLE_WORDS = frozenset({
    "select", "insert", "update", "delete", "truncate", "on", "to", "from", "where", "table", "only", "as", "do",
    "also", "instead", "nothing", "for", "each", "row", "statement", "execute", "function", "procedure", "if",
    "exists", "not", "using", "with", "and", "or", "into", "values", "set", "all", "before", "after", "of",
})

# Statements PostgreSQL accepts that the parser keeps as text (a Command) or rejects,
# by complete shape. Matched against `_normalised` text, so strings are `''` and quoted
# identifiers `_q`. A statement that is not one of these, or is not complete, is not
# "unsupported valid SQL": it is a parse error.
_ROLES = rf"(?:GROUP\s+)?{_NAME}(?:\s*,\s*(?:GROUP\s+)?{_NAME})*"
_TABLE_ACTION = (
    rf"(?:(?:ENABLE|DISABLE|FORCE|NO\s+FORCE)\s+ROW\s+LEVEL\s+SECURITY"
    rf"|(?:ENABLE|DISABLE)\s+(?:REPLICA\s+|ALWAYS\s+)?(?:TRIGGER|RULE)\s+{_NAME}"
    rf"|OWNER\s+TO\s+{_NAME}|SET\s+SCHEMA\s+{_NAME}|SET\s+TABLESPACE\s+{_NAME}|SET\s+ACCESS\s+METHOD\s+{_NAME}"
    rf"|SET\s+(?:LOGGED|UNLOGGED|WITHOUT\s+CLUSTER|WITHOUT\s+OIDS)|(?:RE)?SET\s*\(.+\)"
    rf"|VALIDATE\s+CONSTRAINT\s+{_NAME}|ATTACH\s+PARTITION\s+{_IDENT}\s+(?:FOR\s+VALUES\s+.+|DEFAULT)"
    rf"|DETACH\s+PARTITION\s+{_IDENT}(?:\s+(?:CONCURRENTLY|FINALIZE))?"
    rf"|REPLICA\s+IDENTITY\s+(?:DEFAULT|FULL|NOTHING|USING\s+INDEX\s+{_NAME})|CLUSTER\s+ON\s+{_NAME}"
    rf"|(?:NO\s+)?INHERIT\s+{_IDENT}|NOT\s+OF|OF\s+{_IDENT}"
    rf"|RENAME\s+(?:COLUMN\s+|CONSTRAINT\s+)?{_NAME}\s+TO\s+{_NAME}|RENAME\s+TO\s+{_NAME}"
    rf"|(?:ADD|DROP|ALTER)\s+\S.*)"
)
_CALLS_ROUTINE = rf"EXECUTE\s+(?:FUNCTION|PROCEDURE)\s+{_IDENT}\s*\(.*\)"
_KNOWN_STATEMENTS = [re.compile(pattern + r"\s*$", re.I | re.S) for pattern in (
    r"^DO\s+(?:LANGUAGE\s+\w+\s+)?''(?:\s+LANGUAGE\s+\w+)?",
    rf"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_IDENT}\s*\*?\s+{_TABLE_ACTION}(?:\s*,\s*{_TABLE_ACTION})*",
    rf"^ALTER\s+(?:VIEW|MATERIALIZED\s+VIEW|FOREIGN\s+TABLE)\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_IDENT}\s+\S.*",
    rf"^CREATE\s+POLICY\s+{_NAME}\s+ON\s+{_IDENT}(?:\s+\S.*)?",
    rf"^ALTER\s+POLICY\s+{_NAME}\s+ON\s+{_IDENT}\s+\S.*",
    rf"^DROP\s+(?:POLICY|TRIGGER|RULE)\s+(?:IF\s+EXISTS\s+)?{_NAME}\s+ON\s+{_IDENT}(?:\s+(?:CASCADE|RESTRICT))?",
    rf"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+{_NAME}\s+(?:BEFORE|AFTER|INSTEAD\s+OF)\s+.+?"
    rf"\s+ON\s+{_IDENT}\s+.*?{_CALLS_ROUTINE}",
    rf"^ALTER\s+TRIGGER\s+{_NAME}\s+ON\s+{_IDENT}\s+(?:RENAME\s+TO\s+{_NAME}|(?:NO\s+)?DEPENDS\s+ON\s+EXTENSION\s+{_NAME})",
    rf"^CREATE\s+EVENT\s+TRIGGER\s+{_NAME}\s+ON\s+{_NAME}\s+.*?{_CALLS_ROUTINE}",
    r"^(?:ALTER|DROP)\s+EVENT\s+TRIGGER\s+\S.*",
    rf"^CREATE\s+(?:OR\s+REPLACE\s+)?RULE\s+{_NAME}\s+AS\s+ON\s+(?:SELECT|INSERT|UPDATE|DELETE)\s+TO\s+(?:ONLY\s+)?{_IDENT}"
    rf"(?:\s+WHERE\s+.+?)?\s+DO\s+(?:(?:ALSO|INSTEAD)\s+)?(?:NOTHING|\(.*\)|(?:SELECT|INSERT|UPDATE|DELETE|NOTIFY)\s+.+)",
    rf"^ALTER\s+RULE\s+{_NAME}\s+ON\s+{_IDENT}\s+RENAME\s+TO\s+{_NAME}",
    rf"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+{_IDENT}\s*\(.*",
    r"^(?:ALTER|DROP)\s+(?:FUNCTION|PROCEDURE|ROUTINE|AGGREGATE)\s+\S.*",
    rf"^GRANT\s+(?!ON\b|TO\b).+?\s+TO\s+{_ROLES}(?:\s+WITH\s+(?:GRANT|ADMIN|INHERIT|SET)\s+(?:OPTION|TRUE|FALSE))?(?:\s+GRANTED\s+BY\s+{_NAME})?",
    rf"^REVOKE\s+(?!ON\b|FROM\b).+?\s+FROM\s+{_ROLES}(?:\s+GRANTED\s+BY\s+{_NAME})?(?:\s+(?:CASCADE|RESTRICT))?",
    rf"^ALTER\s+DEFAULT\s+PRIVILEGES\s+.*?\b(?:GRANT\s+.+?\s+TO|REVOKE\s+.+?\s+FROM)\s+{_ROLES}(?:\s+\S.*)?",
    r"^COMMENT\s+ON\s+.+?\s+IS\s+(?:''|NULL)",
    r"^SECURITY\s+LABEL\s+(?:FOR\s+\w+\s+)?ON\s+.+?\s+IS\s+(?:''|NULL)",
    rf"^TABLE\s+(?:ONLY\s+)?{_IDENT}\s*\*?",
    rf"^REFRESH\s+MATERIALIZED\s+VIEW\s+(?:CONCURRENTLY\s+)?{_IDENT}(?:\s+WITH\s+(?:NO\s+)?DATA)?",
    rf"^COPY\s+(?:{_IDENT}\s*(?:\([^)]*\)\s*)?|\(.+\)\s*)(?:FROM|TO)\s+\S.*",
    rf"^CALL\s+{_IDENT}\s*\(.*\)",
    rf"^LOCK\s+(?:TABLE\s+)?(?:ONLY\s+)?{_IDENT}\s*\*?(?:\s*,\s*{_IDENT}\s*\*?)*(?:\s+IN\s+[A-Z ]+?\s+MODE)?(?:\s+NOWAIT)?",
    rf"^DROP\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|FOREIGN\s+TABLE)\s+(?:IF\s+EXISTS\s+)?{_IDENT}(?:\s*,\s*{_IDENT})*"
    rf"(?:\s+(?:CASCADE|RESTRICT))?",
    r"^(?:CREATE|ALTER|DROP)\s+(?:OR\s+REPLACE\s+)?(?:EXTENSION|TYPE|DOMAIN|ROLE|USER(?:\s+MAPPING)?|GROUP|AGGREGATE|CAST|"
    r"COLLATION|CONVERSION|SERVER|PUBLICATION|SUBSCRIPTION|SEQUENCE|FOREIGN\s+DATA\s+WRAPPER|FOREIGN\s+TABLE|"
    r"TEXT\s+SEARCH\s+\w+|(?:TRUSTED\s+)?(?:PROCEDURAL\s+)?LANGUAGE|OPERATOR(?:\s+CLASS|\s+FAMILY)?|STATISTICS|"
    r"TABLESPACE|DATABASE|SCHEMA|OWNED|ACCESS\s+METHOD|TRANSFORM|SYSTEM|LARGE\s+OBJECT)\s+\S.*",
    r"^(?:VACUUM|ANALY[SZ]E|REINDEX|CLUSTER|NOTIFY|LISTEN|UNLISTEN|DISCARD|LOAD|PREPARE|EXECUTE|DEALLOCATE|SHOW|"
    r"RESET|SET|SAVEPOINT|RELEASE|IMPORT\s+FOREIGN\s+SCHEMA|REASSIGN\s+OWNED|FETCH|MOVE|CLOSE|START\s+TRANSACTION|"
    r"ABORT|END|CHECKPOINT|BEGIN|COMMIT|ROLLBACK)\b.*",
)]
# The last word of a statement that stops mid-clause (`GRANT … TO`, `ADD COLUMN (`).
_DANGLING = re.compile(r"(?:[(,=.]|\b(?:TO|ON|FROM|IS|AS|BY|WITH|ADD|COLUMN|USING|CHECK|SET|EXECUTE|FUNCTION|PROCEDURE|"
                       r"TABLE|IN|FOR|WHERE|AND|OR|DO|INTO|CONSTRAINT|REFERENCES|RENAME|OWNER|GRANT|REVOKE|ALTER|CREATE|"
                       r"DROP|POLICY|TRIGGER|RULE|ONLY|EXISTS|IF))$", re.I)


def _complete(shape: str) -> bool:
    """Closed quotes, balanced parentheses and no clause left hanging, on `_normalised` text."""
    if re.search(r"['\"]|\$(?:[A-Za-z_]\w*)?\$", shape.replace("''", "")):
        return False
    depth = 0
    for char in shape:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    tail = shape.rstrip().rstrip(";").rstrip()
    return depth == 0 and bool(tail) and not _DANGLING.search(tail)


def known_statement(statement: str) -> bool:
    """Is `statement` a complete PostgreSQL statement of a kind the parser does not model?"""
    shape = _normalised(_strip_leading(statement)).strip().rstrip(";").strip()
    return _complete(shape) and any(pattern.match(shape) for pattern in _KNOWN_STATEMENTS)


_RECOVER = [(re.compile(pattern, re.I | re.S), relationship) for pattern, relationship in (
    (rf"^ALTER\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|FOREIGN\s+TABLE)\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?P<t>{_IDENT})", "declares"),
    (rf"^(?:CREATE\s+(?:OR\s+REPLACE\s+)?)?RULE\s+{_NAME}\s+AS\s+ON\s+(?:SELECT|INSERT|UPDATE|DELETE)\s+TO\s+(?:ONLY\s+)?(?P<t>{_IDENT})",
     "declares"),
    (rf"^(?:DROP|ALTER)\s+RULE\s+(?:IF\s+EXISTS\s+)?{_NAME}\s+ON\s+(?P<t>{_IDENT})", "declares"),
    (rf"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+{_NAME}\s+(?:BEFORE|AFTER|INSTEAD\s+OF)\s+.*?\bON\s+(?:ONLY\s+)?(?P<t>{_IDENT})",
     "declares"),
    (rf"^(?:CREATE|ALTER|DROP)\s+(?:POLICY|TRIGGER)\s+(?:IF\s+EXISTS\s+)?{_NAME}\s+ON\s+(?:ONLY\s+)?(?P<t>{_IDENT})", "declares"),
    (rf"^TABLE\s+(?:ONLY\s+)?(?P<t>{_IDENT})", "reads"),
    (rf"^COMMENT\s+ON\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|FOREIGN\s+TABLE)\s+(?P<t>{_IDENT})", "declares"),
    (rf"^CREATE\s+(?:FOREIGN\s+TABLE)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<t>{_IDENT})", "declares"),
    (rf"^REFRESH\s+MATERIALIZED\s+VIEW\s+(?:CONCURRENTLY\s+)?(?P<t>{_IDENT})", "writes"),
    (rf"^COPY\s+(?P<t>{_IDENT})\s*(?:\([^)]*\)\s*)?FROM\b", "writes"),
    (rf"^COPY\s+(?P<t>{_IDENT})\s*(?:\([^)]*\)\s*)?TO\b", "reads"),
)]
_GRANT = re.compile(rf"^(?:GRANT|REVOKE)\s+.*?\bON\s+(?:TABLE\s+)?(?P<list>{_IDENT}(?:\s*,\s*{_IDENT})*)\s+(?:TO|FROM)\b", re.I | re.S)
_GRANT_NON_TABLE = re.compile(r"\bON\s+(?:ALL\s+|SCHEMA|FUNCTION|PROCEDURE|ROUTINE|SEQUENCE|DATABASE|TYPE|DOMAIN|LANGUAGE|"
                              r"LARGE\s+OBJECT|TABLESPACE|FOREIGN\s+(?:DATA|SERVER))", re.I)
_DROP_LIST = re.compile(rf"^DROP\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|FOREIGN\s+TABLE)\s+(?:IF\s+EXISTS\s+)?"
                        rf"(?P<list>{_IDENT}(?:\s*,\s*{_IDENT})+)\s*(?:CASCADE|RESTRICT)?\s*;?\s*$", re.I | re.S)


_ATOMIC_BODY = re.compile(r"\bBEGIN\s+ATOMIC\b(?P<body>.*)\bEND\s*;?\s*$", re.I | re.S)
_RULE_ACTION = re.compile(r"\bDO\s+(?:(?:ALSO|INSTEAD)\s+)?(?=[(A-Za-z])", re.I)


def _one_statement_the_parser_splits(sql: str) -> bool:
    """A routine with a BEGIN ATOMIC body or a rule with several actions: `split_statements`
    keeps it whole, but the parser splits it at its inner `;` and loses the body."""
    masked = _mask(sql)
    if ";" not in masked.strip().rstrip(";"):
        return False
    return bool((_ROUTINE_START.match(sql) and re.search(r"\bBEGIN\s+ATOMIC\b", masked, re.I)) or _RULE_START.match(sql))


_GENERATED_AS = re.compile(r"\bGENERATED\s+ALWAYS\s+AS\s*\(", re.I)


def _parser_workaround(sql: str) -> str | None:
    """`sql` with each `GENERATED ALWAYS AS (expr)` written `((expr))`, or None if it has none.

    sqlglot (28 to at least 30.18) rejects a comparison as a generated column's whole
    expression, `GENERATED ALWAYS AS (s >= 0.995) STORED`, which is valid PostgreSQL.
    The extra parentheses mean the same thing and parse. Strings and comments are
    masked first, so text inside them is never taken for a clause."""
    masked = _mask(sql)
    spans = []
    for match in _GENERATED_AS.finditer(masked):
        depth = 0
        for index in range(match.end() - 1, len(masked)):
            depth += {"(": 1, ")": -1}.get(masked[index], 0)
            if depth == 0:
                spans.append((match.end() - 1, index))
                break
    for start, end in reversed(spans):
        sql = f"{sql[:start]}({sql[start:end + 1]}){sql[end + 1:]}"
    return sql if spans else None


def _recover(statement: str) -> list[tuple[str, str]] | None:
    """Table names from a statement the parser could not model, by fixed grammar only.

    None when the text is not a complete statement of a known kind: garbage is a parse
    error, never "unsupported" with a table read off its first words."""
    if not known_statement(statement):
        return None
    text = _strip_leading(statement)
    if re.match(r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b", text, re.I):
        atomic = _ATOMIC_BODY.search(_mask(text))
        return _body_tables(text[atomic.start("body"):atomic.end("body")]) if atomic else []
    found: list[tuple[str, str]] = []
    if _RULE_START.match(text) and (action := _RULE_ACTION.search(_mask(text))):
        # What the rule does to other tables: `DO ALSO (INSERT INTO audit …; …)`.
        body = text[action.end():].strip()
        if body.startswith("(") and body.endswith(")"):
            body = body[1:-1]
        found = [(name, relationship.replace(" in function body", " in rule action"))
                 for name, relationship in _body_tables(body)]
    if match := _GRANT.match(text):
        if not _GRANT_NON_TABLE.search(text):
            found = [(_name(t), "declares") for t in re.split(r"\s*,\s*", match.group("list"))]
    elif match := _DROP_LIST.match(text):
        found = [(_name(t), "declares") for t in re.split(r"\s*,\s*", match.group("list"))]
    else:
        for pattern, relationship in _RECOVER:
            if match := pattern.match(text):
                found = [(_name(match.group("t")), relationship), *found]
                break
    # An unquoted SQL keyword where a table belongs means the pattern misread the grammar.
    return [(name, relationship) for name, relationship in found
            if not (name.lower() in _NOT_TABLE_WORDS and name == name.lower())]


# ── reading a syntax tree ────────────────────────────────────────────────────────
_RELATION_KINDS = {"TABLE", "VIEW"}
_BODY_DML = re.compile(r"\b(?:WITH|SELECT|INSERT|UPDATE|DELETE|MERGE|TRUNCATE)\b", re.I)


@contextlib.contextmanager
def _quiet_parser() -> Iterator[None]:
    # sqlglot logs "contains unsupported syntax. Falling back to parsing as a 'Command'"
    # for valid PostgreSQL DDL (RLS, policies, triggers). The fallback is reported as a
    # diagnostic here; the log line only spams a CLI user's terminal.
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _table_name(table) -> str | None:
    from sqlglot import exp
    parts = table.parts
    if not isinstance(table.this, exp.Identifier) or not all(isinstance(p, exp.Identifier) for p in parts):
        return None
    # PostgreSQL folds unquoted identifiers: `FROM Users` and `FROM users` are one table.
    return ".".join(p.name if p.args.get("quoted") else p.name.lower() for p in parts)


def _target(node):
    from sqlglot import exp
    target = node.this
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None


def _expression_tables(expression, *, in_body: bool = False) -> tuple[list[tuple[str, str]], bool]:
    """`([(name, relationship)], understood)` for one parsed statement.

    `understood` is False when the statement is a kind this reader does not model, so
    an empty table list is not mistaken for "touches nothing"."""
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope

    roles: dict[int, tuple[object, str]] = {}
    extra: list[tuple[str, str]] = []
    understood = True

    def assign(table, relationship: str) -> None:
        if isinstance(table, exp.Table):
            roles.setdefault(id(table), (table, relationship))

    if isinstance(expression, exp.Create):
        kind = str(expression.args.get("kind") or "").upper()
        if kind in {"FUNCTION", "PROCEDURE"}:
            # The signature `touch()` is a routine, not a table. What the routine does
            # is in its body; read that instead.
            return _body_tables(expression.expression), True
        if kind == "INDEX":
            index = expression.this
            assign(index.args.get("table") if isinstance(index, exp.Index) else None, "declares")
        elif kind in _RELATION_KINDS:
            assign(_target(expression), "declares")
        else:
            # SCHEMA, SEQUENCE, DATABASE…: understood, and not a table.
            return [], True
    elif isinstance(expression, (exp.Alter, exp.Drop)):
        kind = str(expression.args.get("kind") or "").upper()
        if kind not in _RELATION_KINDS:
            return [], True
        assign(_target(expression), "declares")
        for rename in expression.find_all(exp.AlterRename):
            assign(rename.this, "declares")
    elif isinstance(expression, exp.TruncateTable):
        for table in expression.expressions:
            assign(table, "writes")
    elif isinstance(expression, (exp.Grant, exp.Revoke)):
        kind = str(expression.args.get("kind") or "TABLE").upper()
        if kind != "TABLE":
            return [], True
        assign(expression.args.get("securable"), "declares")
    elif isinstance(expression, exp.Comment):
        kind = str(expression.args.get("kind") or "").upper()
        if kind not in _RELATION_KINDS:
            return [], True
        assign(expression.this, "declares")
    elif isinstance(expression, exp.Copy):
        assign(_target(expression), "writes" if expression.args.get("kind") else "reads")
    elif isinstance(expression, (exp.Set, exp.Transaction, exp.Commit, exp.Rollback)):
        return [], True
    elif not isinstance(expression, (exp.Query, exp.Values, exp.Insert, exp.Update, exp.Delete, exp.Merge)):
        understood = False

    # A data-modifying statement anywhere in the tree writes its target, including one
    # inside a CTE (`WITH moved AS (DELETE FROM queue RETURNING *) …`).
    for node in expression.find_all(exp.Insert, exp.Update, exp.Delete, exp.Merge):
        assign(_target(node), "writes")
        if isinstance(node, exp.Merge):
            assign(node.args.get("using"), "reads")
    if not in_body:
        for into in expression.find_all(exp.Into):
            # `SELECT … INTO new_table` creates a table. In a PL/pgSQL body it assigns
            # a variable instead, so it is not read there.
            assign(into.this, "writes")
    else:
        for into in expression.find_all(exp.Into):
            if isinstance(into.this, exp.Table):
                roles.setdefault(id(into.this), (into.this, ""))

    ctes = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    # A CTE whose body is a SELECT is resolved by scope analysis. One whose body is
    # INSERT/UPDATE/DELETE is not, and scope then reports its alias as a physical table.
    dml_ctes = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)
                if not isinstance(cte.this, exp.Query)}
    # Scope analysis is an enhancement over the walk below, and sqlglot's optimizer
    # raises on valid PostgreSQL it does not model: `FOR UPDATE OF t` names the alias
    # `t` a second time ("Alias already used"). Every scope call is guarded, so one
    # confusing statement degrades to the plain walk instead of failing its whole file.
    try:
        scopes = list(traverse_scope(expression))
    except Exception:  # noqa: BLE001 - sqlglot raises OptimizeError, KeyError, ...
        scopes = []
    for scope in scopes:
        try:
            selected = list(scope.selected_sources.values())
        except Exception:  # noqa: BLE001 - same class of optimizer failure, per scope
            continue
        for _, resolved in selected:
            if isinstance(resolved, exp.Table) and not (not resolved.db and resolved.name.lower() in dml_ctes):
                assign(resolved, "reads")
    for table in expression.find_all(exp.Table):
        if id(table) in roles:
            continue
        if isinstance(table.parent, exp.Lock):
            continue  # `FOR UPDATE OF t` names a FROM item (usually an alias), not a table
        if not table.db and table.name.lower() in ctes:
            continue
        roles[id(table)] = (table, "reads")

    tables: list[tuple[str, str]] = []
    for table, relationship in roles.values():
        name = _table_name(table)
        if name and relationship:
            tables.append((name, f"{relationship} in function body" if in_body else relationship))
    return tables + extra, understood


#: PL/pgSQL words that separate one embedded SQL statement from the next without a `;`
#: (`FOR r IN SELECT … LOOP UPDATE …`).
_BODY_BREAK = re.compile(r"\b(?:THEN|LOOP|ELSE|ELSIF|ELSEIF|BEGIN|DECLARE|EXCEPTION|WHEN)\b", re.I)


def _body_statement_tables(text: str) -> list[tuple[str, str]] | None:
    """Tables of one body statement, or None when it does not parse."""
    import sqlglot
    from sqlglot import exp
    try:
        with _quiet_parser():
            parsed = sqlglot.parse(text, read="postgres", error_level=sqlglot.errors.ErrorLevel.RAISE)
    except Exception:  # noqa: BLE001 - sqlglot also raises ValueError (`GRANT;`), RecursionError, ...
        return None
    found: list[tuple[str, str]] = []
    for expression in parsed:
        if expression is not None and not isinstance(expression, exp.Command):
            try:
                found.extend(_expression_tables(expression, in_body=True)[0])
            except Exception:  # noqa: BLE001 - same: one body statement is not the routine
                return None
    return found


def _body_tables(body) -> list[tuple[str, str]]:
    """Tables a routine or DO block body reads or writes, statement by statement.

    PL/pgSQL (`IF … THEN UPDATE …; END IF;`) is not SQL, so the body is not parsed
    whole: each statement is cut at its first DML keyword and parsed alone. When that
    does not parse (`FOR r IN SELECT * FROM a LOOP UPDATE b …`), the statement is cut
    again at THEN/LOOP/ELSE/BEGIN… and each piece is parsed from its own first DML
    keyword, with a `)` that closes an outer PL/pgSQL expression trimmed. What still
    does not parse is skipped silently — the routine itself was understood, and a body
    statement the reader cannot model is not evidence of a table."""
    from sqlglot import exp
    text = body.name if isinstance(body, exp.Expression) else str(body or "")
    text = re.sub(r"^\$(\w*)\$(.*)\$\1\$$", r"\2", text.strip(), flags=re.S)
    found: list[tuple[str, str]] = []
    for _, statement in split_statements(text):
        masked = _mask(statement)
        keyword = _BODY_DML.search(masked)
        if not keyword:
            continue
        whole = _body_statement_tables(statement[keyword.start():])
        if whole is not None:
            found.extend(whole)
            continue
        cuts = [0] + [point for match in _BODY_BREAK.finditer(masked) for point in (match.start(), match.end())] + [len(masked)]
        for start, end in zip(cuts[::2], cuts[1::2], strict=False):
            keyword = _BODY_DML.search(masked, start, end)
            if not keyword:
                continue
            depth, stop = 0, end
            for index in range(keyword.start(), end):
                if masked[index] == "(":
                    depth += 1
                elif masked[index] == ")":
                    depth -= 1
                    if depth < 0:
                        stop = index
                        break
            found.extend(_body_statement_tables(statement[keyword.start():stop]) or [])
    return found


def _placeholder_identifier(expression, sql: str) -> bool:
    """Does a `$n` placeholder stand where a table, schema or column NAME goes?

    Bind parameters carry values. A template substitution in a name position
    (`FROM ${table}`) is dynamic SQL, however it was rewritten."""
    from sqlglot import exp
    if re.search(r"\$\d+\s*\.|\.\s*\$\d+", sql):
        return True
    for parameter in expression.find_all(exp.Parameter, exp.Placeholder):
        parent = parameter.parent
        if isinstance(parent, (exp.Table, exp.Column, exp.Dot)):
            return True
        if isinstance(parent, exp.Schema) and parameter.arg_key == "expressions":
            return True
        if isinstance(parent, exp.EQ) and parameter.arg_key == "this" \
                and isinstance(parent.parent, (exp.Update, exp.When, exp.Merge)):
            return True
    return False


def _issue(graph: Graph, code: str, severity: str, message: str, source: str, location: str, action: str) -> None:
    graph.issues.append(Issue(code, severity, message, [source], location, action))


_DETAIL = re.compile(r"^SQL syntax reference \(([^)]*)\)")


def _emit(graph: Graph, source: str, location: str, operations: dict[str, set[str]]) -> None:
    for name, relationships in operations.items():
        node_id = stable_id("postgres_table", name)
        graph.add_node(Node(node_id, "postgres_table", name,
                            metadata={"store": name, "dialect": "postgres", "catalog_verified": False}))
        edge = Edge(source, node_id, "TOUCHES_STORE", "probable", location, origin="sqlglot", detail="")
        if edge.key in graph._edge_keys:
            # `add_edge` keeps the first edge per (source, target, kind, evidence), so a
            # second statement on the same line would lose its relationship. Merge it.
            for existing in graph.edges:
                if existing.key == edge.key and existing.origin == "sqlglot" and existing.detail:
                    if match := _DETAIL.match(existing.detail):
                        relationships = relationships | set(match.group(1).split(", "))
                    existing.detail = _detail(relationships)
                    break
            continue
        edge.detail = _detail(relationships)
        graph.add_edge(edge)


def _detail(relationships: set[str]) -> str:
    return (f"SQL syntax reference ({', '.join(sorted(relationships))}); "
            "live schema and runtime access unverified")


def add_sql(graph: Graph, source: str, sql: str, location: str, *, dynamic: bool = False,
            gated: bool = False, parameterized: bool = False, psql: bool = False) -> None:
    """Record the tables one SQL text references, attributed to `source` at `location`.

    `gated`: the text was picked out of source code by heuristic (a string handed to
    `.execute`), so text that is not SQL-shaped is ignored and a parse failure is an
    info diagnostic, not a gap in the analysis.
    `parameterized`: the caller replaced template substitutions with `$1..$n`. Tables
    are recorded unless a placeholder sits where a name goes.
    `dynamic`: runtime interpolation that could not be rewritten; no table is recorded.
    `psql`: the placeholders stand for psql variables in a `.sql` file. The statement is
    still the file's own SQL, so one that does not parse is SQL_PARSE_ERROR, not DYNAMIC_SQL."""
    from .columns import record_search_path
    record_search_path(graph, sql, location)  # before the gate: `SET search_path` is not a shape it admits
    if gated and not dynamic and not looks_like_sql(sql):
        if _sql_like(sql):
            # A statement verb and clauses, rejected by the prose test: say so rather
            # than drop what may be a query the gate misjudged.
            _issue(graph, "SQL_NOT_PARSED", "info", "SQL-shaped string was not read: it did not pass the SQL shape test.",
                   source, location, "Check the text; if it is a query, review its tables manually.")
        return
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        _issue(graph, "SQL_PARSER_UNAVAILABLE", "warning", "PostgreSQL parser is unavailable.",
               source, location, "Install repolens[stack] to analyze SQL syntax.")
        return
    if dynamic:
        _issue(graph, "DYNAMIC_SQL", "info", "SQL contains runtime interpolation.",
               source, location, "Review parameterization and any dynamic identifiers.")
        # Dynamic identifiers cannot safely be turned into a concrete table name.
        return
    from .columns import record_statement, record_text

    def not_read() -> None:
        if parameterized and not psql:
            _issue(graph, "DYNAMIC_SQL", "info", "SQL template could not be parsed with its substitutions as parameters.",
                   source, location, "Review parameterization and any dynamic identifiers.")
        elif gated:
            _issue(graph, "SQL_NOT_PARSED", "info", "SQL-shaped string could not be parsed as PostgreSQL.",
                   source, location, "Check the dialect; the text may not be a query.")
        else:
            _issue(graph, "SQL_PARSE_ERROR", "warning", "SQL could not be parsed as PostgreSQL.",
                   source, location, "Check the SQL dialect, interpolation and parser support.")

    # Outside the `try`: a failure in this reader's own helper is not the target's parse error.
    splits = _one_statement_the_parser_splits(sql)
    try:
        if splits:
            raise sqlglot.errors.ParseError("read lexically: the parser would split this statement")
        with _quiet_parser():
            try:
                expressions = sqlglot.parse(sql, read="postgres", error_level=sqlglot.errors.ErrorLevel.RAISE)
            except Exception:  # noqa: BLE001 - same as below; retried once with a known parser gap worked around
                if (rewritten := _parser_workaround(sql)) is None:
                    raise
                expressions = sqlglot.parse(rewritten, read="postgres", error_level=sqlglot.errors.ErrorLevel.RAISE)
    except Exception:  # noqa: BLE001 - not only SqlglotError: `GRANT;` raises ValueError, deep nesting RecursionError
        # One statement the parser cannot take is that statement's diagnostic, never the
        # whole file's FILE_SCAN_FAILED with every later statement lost.
        record_text(graph, sql, location)  # a CREATE/ALTER TABLE it holds still changes what is known
        recovered = None if gated or (parameterized and not psql) else _recover(sql)
        if recovered is not None:
            # Valid PostgreSQL the parser rejects (`DROP TABLE a, b`, a BEGIN ATOMIC
            # body): a complete statement of a known kind, reported, not a gap.
            _issue(graph, "SQL_UNSUPPORTED_STATEMENT", "info", "SQL statement was read lexically, without a syntax tree.",
                   source, location, "Review this statement manually.")
            _emit(graph, source, location, _group(recovered))
        else:
            not_read()
        return
    found: list[tuple[str, str]] = []
    with _quiet_parser():
        for expression in expressions:
            if expression is None:
                continue
            if parameterized and _placeholder_identifier(expression, sql):
                _issue(graph, "DYNAMIC_SQL", "info", "SQL template substitutes a table or column name.",
                       source, location, "Review parameterization and any dynamic identifiers.")
                return
            if isinstance(expression, exp.Command):
                rest = expression.expression
                text = f"{expression.this} {rest.name if isinstance(rest, exp.Expression) else rest or ''}"
                record_text(graph, text, location)
                tables = _recover(text)
                if tables is None:
                    # The parser's Command fallback takes any text (`GRANT … TO ;`).
                    # Only a complete statement of a known kind is "unsupported".
                    not_read()
                    continue
                if str(expression.this).upper() == "DO":
                    tables += _body_tables(expression.expression)
                # Not an analysis gap: the statement parsed as a command whose effects
                # this reader does not model. Listed so a reviewer can look at it.
                _issue(graph, "SQL_UNSUPPORTED_STATEMENT", "info", "SQL statement has no supported syntax tree.",
                       source, location, "Review this statement manually.")
                found.extend(tables)
                continue
            try:
                tables, understood = _expression_tables(expression)
            except Exception:  # noqa: BLE001 - a tree reader failure of any kind degrades to the walk
                # A statement the tree readers cannot follow still names its tables.
                tables = [(name, "reads") for table in expression.find_all(exp.Table)
                          if not isinstance(table.parent, exp.Lock) and (name := _table_name(table))]
                understood = True
            record_statement(graph, source, location, expression)
            if isinstance(expression, (exp.Create, exp.Alter)):
                _foreign_keys(graph, expression, location)
            if not tables and not understood:
                # A bare expression (`FOOBAR orders` parses as an alias) is not a
                # statement; `TABLE users` and `CHECKPOINT` are.
                try:
                    text = sql if len(expressions) == 1 else expression.sql(dialect="postgres")
                except Exception:  # noqa: BLE001 - the generator can fail where the parser did not
                    text = ""
                recovered = _recover(text) if text else None
                if recovered is None:
                    not_read()
                    continue
                _issue(graph, "SQL_UNSUPPORTED_STATEMENT", "info", "SQL statement kind is not modelled.",
                       source, location, "Review this statement manually.")
                tables = recovered
            found.extend(tables)
    _emit(graph, source, location, _group(found))


def _foreign_keys(graph: Graph, expression, location: str) -> None:
    """`REFERENCES` edges from a table to each table its `CREATE TABLE`/`ALTER TABLE` foreign keys name.

    The same edge the Python reader records for SQLAlchemy foreign keys. DDL in test code or
    fixtures declares nothing, so it adds no edge. A failure here only loses the edge: it is
    never the statement's parse error."""
    from sqlglot import exp
    if is_test_path(re.sub(r":\d+(?::\d+)?$", "", location)):
        return
    try:
        if str(expression.args.get("kind") or "").upper() != "TABLE":
            return
        declaring = _target(expression)
        name = _table_name(declaring) if declaring is not None else None
        if not name:
            return
        for reference in expression.find_all(exp.Reference):
            table = reference.this.this if isinstance(reference.this, exp.Schema) else reference.this
            referenced = _table_name(table) if isinstance(table, exp.Table) else None
            if not referenced:
                continue
            ids = []
            for store in (name, referenced):
                ids.append(stable_id("postgres_table", store))
                graph.add_node(Node(ids[-1], "postgres_table", store,
                                    metadata={"store": store, "dialect": "postgres", "catalog_verified": False}))
            graph.add_edge(Edge(ids[0], ids[1], "REFERENCES", "exact", location, origin="sqlglot", detail="foreign key"))
    except Exception:  # noqa: BLE001 - advisory
        return


def _group(tables: list[tuple[str, str]]) -> dict[str, set[str]]:
    operations: dict[str, set[str]] = {}
    for name, relationship in tables:
        operations.setdefault(name, set()).add(relationship)
    return operations


def add_sql_file(graph: Graph, source: str, text: str, rel: str) -> None:
    """A `.sql` file, one statement at a time.

    Parsed whole, one statement the parser rejects discarded every table in the file,
    and every relationship carried the file's path with no line. Each statement is
    parsed alone and located at its first line; a failure is its own diagnostic.

    A PL/pgSQL statement at top level (`RAISE NOTICE '…';` after `COMMIT;`) is a real
    error in the script: PostgreSQL rejects it there. It is reported as that, not as
    SQL this reader failed to parse.

    A file in another dialect (T-SQL, MySQL, Oracle, SQLite) or a template (dbt/Jinja)
    is reported once as SQL_DIALECT_NOT_POSTGRES and not read statement by statement:
    PostgreSQL parse errors and PL/pgSQL warnings about it would be false.

    psql variables (`:name`, `:'name'`, `:"name"`) become `$n` placeholders, so the
    statement parses as a parameterized one."""
    if dialect := non_postgres_dialect(text):
        name, line, evidence = dialect
        _issue(graph, "SQL_DIALECT_NOT_POSTGRES", "info",
               f"SQL file uses {name} syntax ({evidence}); it was not read as PostgreSQL.",
               source, f"{rel}:{line}",
               "Its tables and columns are not in the graph; review the file with a tool for its dialect.")
        return
    for line, statement in split_statements(text):
        location = f"{rel}:{line}"
        if keyword := plpgsql_outside_block(statement):
            _issue(graph, "SQL_PLPGSQL_OUTSIDE_BLOCK", "warning",
                   f"{keyword} is a PL/pgSQL statement outside a DO block or function body; PostgreSQL rejects this script here.",
                   source, location,
                   "Wrap it in DO $$ BEGIN ... END $$; or remove it — PostgreSQL rejects PL/pgSQL statements at top level.")
            continue
        statement, substituted = psql_variables(statement)
        add_sql(graph, source, statement, location, parameterized=substituted, psql=substituted)


_PSQL_VARIABLE = re.compile(
    r"(?P<skip>(?<![\w$])[eE]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'|\$(?P<tag>(?:[A-Za-z_]\w*)?)\$.*?\$(?P=tag)\$|--[^\n]*|/\*.*?\*/)"
    r"|(?<![:\w\[]):(?:'(?P<literal>[A-Za-z_]\w*)'|\"(?P<identifier>[A-Za-z_]\w*)\"|(?P<bare>[A-Za-z_]\w*))"
    r"|(?P<quoted>\"(?:[^\"]|\"\")+\")", re.S)


def psql_variables(statement: str) -> tuple[str, bool]:
    """`statement` with psql variables outside strings replaced by `$1..$n`, and whether any were."""
    count = 0

    def swap(match: re.Match) -> str:
        nonlocal count
        if match.group("skip") is not None or match.group("quoted") is not None:
            return match.group(0)
        count += 1
        return f"${count}"
    replaced = _PSQL_VARIABLE.sub(swap, statement)
    return replaced, bool(count)


# Constructs PostgreSQL does not have, looked for outside strings and comments. Each is
# specific to its dialect: `@@` (a text-search operator) and `dbo.` (a legal schema
# name) are deliberately not signals. A "weak" signal is also valid PostgreSQL in some
# spelling (a column named `go` on its own line, a function `nvarchar(10)` or `top (5)`),
# so it counts only beside another signal of the same dialect.
_DIALECT_SIGNALS = [(name, re.compile(pattern, flags), dialect, weak) for name, pattern, flags, dialect, weak in (
    ("jinja", r"\{\{-?\s*[A-Za-z_]|\{%-?\s*[A-Za-z_]", 0, "a Jinja/dbt template", False),
    ("delimiter", r"^[ \t]*DELIMITER[ \t]+\S+[ \t]*$", re.I | re.M, "MySQL", False),
    ("backtick", r"`[^`\n]+`", 0, "MySQL", False),
    ("engine", r"\)\s*ENGINE\s*=\s*\w+", re.I, "MySQL", False),
    ("auto_increment", r"(?:\b(?:INT|INTEGER|BIGINT|SMALLINT|TINYINT|MEDIUMINT|UNSIGNED|NULL|KEY)|\))\s+AUTO_INCREMENT\b"
                       r"|\bAUTO_INCREMENT\s*=\s*\d", re.I, "MySQL", False),
    ("variable", r"\bDECLARE\s+@\w|\bSET\s+@\w+\s*=|\[dbo\]\s*\.", re.I, "T-SQL", False),
    # `SELECT top 5 …` is not PostgreSQL in any spelling; `SELECT top (5)` calls a function.
    ("top", r"\bSELECT\s+(?:DISTINCT\s+)?TOP\s+\d+\b", re.I, "T-SQL", False),
    ("top_call", r"\bSELECT\s+(?:DISTINCT\s+)?TOP\s*\(\s*\d+\s*\)", re.I, "T-SQL", True),
    ("go_twice", r"^[ \t]*GO[ \t]*$(?:\n.*)*?\n[ \t]*GO[ \t]*$", re.I | re.M, "T-SQL", False),
    ("use", r"^[ \t]*USE[ \t]+\[?[A-Za-z_]\w*\]?[ \t]*;?[ \t]*$", re.I | re.M, "T-SQL", False),
    ("nocount", r"\bSET\s+NOCOUNT\s+(?:ON|OFF)\b", re.I, "T-SQL", False),
    ("exec_sp", r"\bEXEC\s+(?:dbo\.)?sp_\w+", re.I, "T-SQL", False),
    # `[orders]` where a name goes; PostgreSQL brackets only subscript an expression (`a[1]`, `ARRAY[…]`).
    ("brackets", r"(?:\b(?:TABLE|FROM|JOIN|INTO|UPDATE|EXISTS)\s+|[(,]\s*)\[[A-Za-z_][\w$ ]*\]", re.I, "T-SQL", False),
    ("nvarchar", r"\bNVARCHAR\s*\(", re.I, "T-SQL", True),
    ("identity", r"\bIDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)", re.I, "T-SQL", True),
    ("go", r"^[ \t]*GO[ \t]*$", re.I | re.M, "T-SQL", True),
    ("varchar2", r"\bVARCHAR2\s*\(", re.I, "Oracle", False),
    ("pragma", r"^[ \t]*PRAGMA\s+[\w.]+\s*(?:=|\(|;|$)", re.I | re.M, "SQLite", False),
    ("autoincrement", r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.I, "SQLite", False),
)]


def _in_copy(masked: str, start: int) -> bool:
    """Is `start` inside a COPY statement (`COPY t FROM '/x.csv'\\n DELIMITER ',' CSV`)?"""
    return bool(re.match(r"\s*COPY\b", masked[masked.rfind(";", 0, start) + 1:start], re.I))


def non_postgres_dialect(text: str) -> tuple[str, int, str] | None:
    """`(dialect, line, evidence)` for the first construct PostgreSQL does not have, else None."""
    masked = _mask(text)
    found: dict[str, list[tuple[int, str, bool, str]]] = {}
    for name, pattern, dialect, weak in _DIALECT_SIGNALS:
        for match in pattern.finditer(masked):
            if name == "delimiter" and _in_copy(masked, match.start()):
                continue
            found.setdefault(dialect, []).append((match.start(), name, weak, " ".join(match.group(0).split())[:40]))
            break
    hits = [hit for signals in found.values() for hit in signals
            if not hit[2] or len({other[1] for other in signals}) > 1]
    if not hits:
        return None
    start, name, _, evidence = min(hits)
    dialect = next(d for n, _, d, _ in _DIALECT_SIGNALS if n == name)
    return dialect, masked.count("\n", 0, start) + 1, evidence


# Statement openings that exist only in PL/pgSQL. Every one is a syntax error as a
# top-level SQL statement. `DECLARE name CURSOR`, `END`, `BEGIN`, `EXECUTE`, `FETCH`,
# `CLOSE`, `MOVE` and `CALL` are valid SQL and deliberately absent.
_PLPGSQL_ONLY = re.compile(rf"""^(?:
    (?P<simple>RAISE|PERFORM|RETURN|ASSERT|EXIT|CONTINUE|LOOP|FOREACH|ELSIF|ELSEIF)\b
  | (?P<diagnostics>GET\s+(?:CURRENT\s+|STACKED\s+)?DIAGNOSTICS)\b
  | (?P<end>END\s+(?:IF|LOOP|CASE))\b
  | (?P<conditional>IF|WHILE|FOR)\b(?=.*\b(?:THEN|LOOP)\b)
  | (?P<declare>DECLARE)\b(?!\s+@)(?!\s+{_NAME}\s+(?:(?:BINARY|ASENSITIVE|INSENSITIVE|NO\s+SCROLL|SCROLL)\s+)*CURSOR\b)
)""", re.I | re.S | re.X)


def plpgsql_outside_block(statement: str) -> str | None:
    """The PL/pgSQL-only keyword a top-level statement starts with, else None."""
    body = _without_quoted(_strip_leading(statement))
    match = _PLPGSQL_ONLY.match(body)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(0)).upper()
