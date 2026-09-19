"""Startup reconciliation preserves only model Runs backed by durable work."""


def recover_legacy_runs(runs, repository):
    protected = {event['payload']['run_id']
                 for kind in ('assessment.generate', 'message.generate')
                 for event in repository.records('outbox', event_type=kind, lock=False)
                 if event['status'] in {'pending', 'processing'} and event['payload'].get('run_id')}
    return runs.recover_interrupted(
        exclude_kinds={'graph_reconcile', 'knowledge_publish', 'material_ingest', 'material_delete'},
        exclude_ids=protected,
    )
