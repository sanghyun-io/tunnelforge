"""Turn a TunnelForge dump manifest into a schema-only fixture with every name and string removed.

Usage: python scripts/anonymize_dump_schema.py <dump_dir> <fixture_dir>

Keeps what decides import behaviour: column types, keys, character sets, collations,
foreign keys, CHECK constraints and view shapes. Replaces table, column, index, FK and CHECK
names, ENUM/SET labels and string literals, drops comments and data, and writes no mapping.
"""
import json
import re
import sys
from pathlib import Path

FIXTURE_SCHEMA = "fixture"


def mask(text):
    """Keep length, case and character class, drop the content."""
    out = []
    for char in text:
        if char.isascii() and char.isalpha():
            out.append("X" if char.isupper() else "x")
        elif char.isascii() and char.isdigit():
            out.append("0")
        elif not char.isascii() and char.isalnum():
            out.append("가")
        else:
            out.append(char)
    return "".join(out)


def literal_spans(sql):
    """Yield (start, end) of each '...' literal, honouring backslash and doubled quotes."""
    index = 0
    while index < len(sql):
        if sql[index] == "`":
            index = sql.index("`", index + 1) + 1
            while index < len(sql) and sql[index] == "`":
                index = sql.index("`", index + 1) + 1
            continue
        if sql[index] == "'":
            start, index = index, index + 1
            while index < len(sql):
                if sql[index] == "\\":
                    index += 2
                elif sql[index] == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                    index += 2
                elif sql[index] == "'":
                    break
                else:
                    index += 1
            yield start, index + 1
        index += 1


def mask_literals(sql, labels=None):
    out, last = [], 0
    for start, end in literal_spans(sql):
        body = sql[start + 1:end - 1]
        out.append(sql[last:start])
        out.append("'" + (labels.get(body) if labels and body in labels else mask(body)) + "'")
        last = end
    out.append(sql[last:])
    return "".join(out)


class Names:
    def __init__(self, tables):
        self.tables = {name: f"t{index:03d}" for index, name in enumerate(tables, 1)}
        self.columns, self.aliases = {}, {}

    def table(self, name):
        if name not in self.tables:
            self.tables[name] = f"t{len(self.tables) + 1:03d}"
        return self.tables[name]

    def column(self, name):
        return self.columns.setdefault(name, f"c{len(self.columns) + 1}")

    def alias(self, name):
        if name in self.columns:
            return self.columns[name]
        return self.aliases.setdefault(name, f"a{len(self.aliases) + 1}")


IDENT = re.compile(r"`((?:[^`]|``)*)`")


def rewrite_sql(sql, names, source_schema, single_table=False):
    """Rename backticked identifiers by position, then mask string literals."""
    sql = re.sub(r"DEFINER=`[^`]*`@`[^`]*`\s*", "", sql)
    out, last, previous = [], 0, None
    for match in IDENT.finditer(sql):
        name = match.group(1).replace("``", "`")
        after_dot = sql[:match.start()].endswith(".")
        before_dot = sql[match.end():].startswith(".")
        if single_table:
            new = names.column(name)
        elif after_dot and previous == source_schema:
            new = names.table(name)
        elif after_dot:
            # A column, or a column alias of another view: same mapping as where the alias is defined.
            new = names.alias(name)
        elif before_dot and name == source_schema:
            new = FIXTURE_SCHEMA
        elif before_dot or name in names.tables:
            new = names.tables.get(name) or names.alias(name)
        else:
            new = names.alias(name)
        out.append(sql[last:match.start()] + f"`{new}`")
        last, previous = match.end(), name
    out.append(sql[last:])
    return mask_literals("".join(out))


def enum_labels(type_name):
    if not re.match(r"\s*(enum|set)\s*\(", type_name, re.I):
        return None
    labels, used = {}, set()
    for start, end in literal_spans(type_name):
        body = type_name[start + 1:end - 1]
        new, suffix = mask(body), 1
        while new in used:
            new, suffix = f"{mask(body)}{suffix}", suffix + 1
        used.add(new)
        labels[body] = new
    return labels


def anonymize(manifest):
    source_schema = manifest.get("source_schema") or manifest.get("database")
    tables = manifest["schema"]["tables"]
    names = Names([table["name"] for table in tables])
    index_count = fk_count = check_count = 0
    for table in tables:
        for column in table["columns"]:
            names.column(column["name"])
    for table in tables:
        table["name"] = names.table(table["name"])
        table["comment"] = None
        table["auto_increment"] = None
        for column in table["columns"]:
            column["name"] = names.column(column["name"])
            column["comment"] = None
            labels = enum_labels(column["type"])
            column["type"] = mask_literals(column["type"], labels)
            if column.get("default") is not None:
                column["default"] = (rewrite_sql(column["default"], names, source_schema, single_table=True)
                                     if column.get("default_is_expression") else mask_literals(column["default"], labels))
        for index in table.get("indexes", []):
            index_count += 1
            index["name"] = f"i{index_count}"
            index["columns"] = [names.column(name) for name in index["columns"]]
        for fk in table.get("foreign_keys", []):
            fk_count += 1
            fk["name"] = f"f{fk_count}"
            fk["columns"] = [names.column(name) for name in fk["columns"]]
            fk["referenced_table"] = names.table(fk["referenced_table"])
            fk["referenced_columns"] = [names.column(name) for name in fk["referenced_columns"]]
        for check in table.get("checks", []):
            check_count += 1
            check["name"] = f"k{check_count}"
            check["expression"] = rewrite_sql(check["expression"], names, source_schema, single_table=True)
    for table in manifest["tables"]:
        table["name"] = names.table(table["name"])
        table["path"] = f"tables/{table['name']}"
        table.update(rows=0, chunks=0, chunk_sha256={})
    views = manifest.get("views", [])
    # Views are relations too: other views select from them by `schema`.`name`.
    for view in views:
        names.table(view["name"])
    for view in views:
        view["name"] = names.table(view["name"])
        view["definition"] = rewrite_sql(view["definition"], names, source_schema)
    manifest.update(database=FIXTURE_SCHEMA, source_schema=FIXTURE_SCHEMA, manifest_warnings=[])
    return manifest


def main():
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    manifest = json.loads((source / "_tunnelforge_dump.json").read_text(encoding="utf-8"))
    target.mkdir(parents=True, exist_ok=True)
    (target / "_tunnelforge_dump.json").write_text(
        json.dumps(anonymize(manifest), ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
