# MAF Managed Coordination Files

`maf/control` is written only by the central scheduler. Nodes append events to `maf/node/<node-id>` and write deliverables to the assigned `maf/task/<task-id>/e<epoch>-<node-id>` branch.

Every event must pass `.maf/schemas/event-v1.schema.json`. Never edit generated `.maf/status.md` manually.

