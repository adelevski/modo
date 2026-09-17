# Hosted service

This policy applies to any official deployment of modo. Operators of
independent deployments set their own service policies.

## Behavior

- modo has no accounts, advertising, or analytics.
- Confirmed coordinates are used for one calculation and are not stored.
- Results use an identified static road snapshot and cost profile. Traffic,
  depart-at, and arrive-by are not supported.
- Results are stored road vertices, not venues or assurances of a safe stopping
  place. Routes may omit curves between adjacent vertices.

## Browser services

- Address text goes directly from the browser to the public Photon service.
  Photon receives the query, IP address, and ordinary request metadata.
  Manually entered coordinates do not go to Photon.
- Map tiles load directly from OpenStreetMap, which receives IP and request
  metadata; tile requests reveal the viewed map area. Leaflet is served by
  modo itself.

## Current limits

The interface accepts between two and 32 origins and at most 32 KiB of an
`application/json` request. Coordinates must be inside the active snapshot's
supported core and within 1 km of one of its road vertices. The initial
snapshot covers the Chicago area specified in `src/modo/snapshots.json`.

The one-minute region is limited to 5,000 vertices and returned routes to
100,000 vertices in total. modo rejects larger results rather than truncating
them.

Exact search also has a work budget of 10,000 to 50,000 discovered
origin-vertex labels, depending on the snapshot size and distinct snapped
origins. Groups can exceed this budget even when their origins are inside
coverage; modo then rejects the calculation without returning a partial region.

Evaluation requests share a small process-wide rate budget. It stores no IP
address, coordinate, or other requester identifier and returns `429` with a
retry delay when the free service is busy.

## Acceptable use

Use the service for ordinary interactive meeting-region calculations. Do not
intentionally disrupt it, evade its limits, send automated bulk traffic, or
use it in violation of law or another person's rights. Report good-faith
security research privately and avoid harm.

The operator may reject, limit, or block abusive traffic. The service is provided
without a guarantee of availability.

Report security issues through GitHub's private vulnerability reporting form
for the modo repository.
