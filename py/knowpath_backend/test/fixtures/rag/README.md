# CLI test configuration

`build.json` is a fail-closed template: replace both identities with an uploaded
material and learning space in a dedicated migrated test database. Set
`DATABASE_URL` to that database and `QDRANT_URL` to test storage before use.
The CLI never creates/migrates the database or uploads arbitrary files.

For the isolated real public-sample workspace, `rag_eval.bootstrap prepare`
creates the test identities and records them in `setup.json`. Use its exact
`collection_prefix` and material/space identities to reuse the verified index.
`publish` and `rollback` both require `--expected-generation`; use 0 only for an
unpublished material. A stale generation must fail without replacing the pointer.
