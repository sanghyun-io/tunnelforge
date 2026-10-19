# TF-STATUS-203 release compatibility notes

Full MySQL dumps now preserve proven generated/invisible columns, triggers,
routines and events in formats 6/7. Formats 1-7 remain readable by this release;
older applications refuse unsupported formats before changing the target.

Export now refuses a table definition that the same-engine importer cannot
recreate. A backup that succeeded in v2.19 may therefore fail, including a
scheduled full backup. Long ENUM/SET definitions (for example more than 512 bytes)
or a type definition the DDL validator cannot safely reproduce can trigger this.
The refusal names the table with `table_definition_unrestorable`; the incomplete
export toggle cannot bypass it. Review/simplify the source definition or explicitly
export only the other tables. Such a subset is not a complete database backup.
This guard prevents creating a strict dump containing an unrestorable table.

Table-subset imports explicitly report omitted views and stored programs. This
can make the existing confirmation mandatory even for old or PostgreSQL dumps;
the data/DDL behavior is unchanged. Read the named omissions before continuing.

Stored programs are supported by normal replace Import. Verify-in-copy and
scheduled restore rehearsals still refuse them before creating a copy; use
normal Import for these dumps. Safe support is planned separately.
