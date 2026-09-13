# printing

Onsite manufacturing. A cafe's steam wand tip cracks; the machine is out of service until a
replacement ships, if the part is still made at all. The cafe has a printer. The part is a shape, a
material, and a set of settings — all of which are data.

The interesting part isn't that one firm can print a spare. It's that **every firm that prints the
same part generates evidence about it**, and the network keeps that evidence. A design starts as
someone's file, gets printed by a dozen firms, breaks in three of them the same way, and the next
revision is stronger where it actually failed. No vendor collects that. No certification body
collects it either — they test coupons in a lab, once, under loads someone imagined in advance.

That is what "certified by live feedback" means here, and it isn't a weaker claim than a lab
certificate — it's a stronger one. A lab tells you a part survived the load case an engineer thought
of. The network tells you what actually breaks it: a barista levering the wand against the bottom of
a steaming pitcher, which is off-label, recurring, and in no spec sheet ever written.

The parts themselves come from wherever parts come from — a public git repo, a model site, a
vendor's own file, or an agent that drew it. What the network adds is the part's **service record**:
who printed it, on what, in what material, and what happened to it afterward.

See `AGENTS.md` for the intended module shape and `TODO.md` for the build plan.
