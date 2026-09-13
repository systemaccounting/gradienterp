# hub

A hub is an AWS account whose whole job is delivering events to the gerps of its region. It holds
the bus any gerp in the organization puts to, a rule per spoke whose target is that gerp's own
bus, and the door those rules are added and removed through. It holds no data.

The platform's graph is buses and one directory: every gerp is a spoke of one hub, the operator is
a spoke behind one forward edge, and a gerp addressing another reads the recipient's hub off the
platform directory and puts on that hub's bus itself — so hubs never know each other, and a second
hub is another account of this shape, in another region or beside the first when the first is
full, touching nothing that exists. The quotas that bound a hub (rules per bus, invocations per
second) are the hub account's own, adjustable by request, and shared with nothing else, so a hub's
capacity is a number and adding capacity is a vend.
