# Initial historical probe

This probe ran real native Test providers and verified the join while its
TemporaryDirectory-owned Store existed. That temporary Store is gone; its Store
references are historical, not currently available objects. The raw result,
events and captured projection are retained without backfilling history.

Use the sibling `native-verification-durable/` proof for a completed session and
strict restart/readback through an enduring ordinary Store artifact.
