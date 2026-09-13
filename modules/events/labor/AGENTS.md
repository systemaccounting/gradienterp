# labor events

A person's time is capacity inventory — labor events are the capacity events of the human meter.
Emitter module: `modules/labor`. Entry ids carry the location ordinal (`<ts>#<n>#<id>`); shifts
attribute to where they HAPPEN, workers have a home location.

## shift.clocked_in.v1 / shift.clocked_out.v1 — PENDING
- **emit sites**: `manage_labor (op: put)` (time_entry create) / the close path. The labor half of the
  physical i/o series; clocked_out carries `hours` so utilization aggregates need no join.

## shift.unfilled.v1 — SPEC
- floating labor: a shift needing cover matches available workers across firms (cross-firm
  staffing). `rate: 0` = the role's standard rate. The agent-poke availability loop
  (`modules/inventory/TODO.md`) is the supply-side data source.

## job_opening.created.v1 — SPEC
- permanent placement, distinct from floating shifts. What open books uniquely add: cross-firm
  wage + utilization data as hiring signal — a candidate can see a firm's real economics before
  joining, and the match can rank openings by demonstrated utilization.
