# api.openlyoperated.biz

The read api over openly-operated business: the same JSON the pages on openlyoperated.biz render,
served to anyone, and the stream those numbers move on.

Two kinds of data, and the paths carry the line. **Business data** is a firm's own books, read
through from the firm's own account and never copied; the firm gates it with one flag. **Economic
data** is what every firm emits as it trades — the counters — the platform's own store, no opt-in,
never a gerp id. `/v1/gerps/...` is the first, `/v1/economy/...` the second, and the stream on
`events.openlyoperated.biz` carries both: a published gerp's events on its own channels, every
counter delta on one.

Why an api at all, when each gerp already serves its own `/oob` reads: a stranger and a stranger's
agent need one door, one contract, one key, and a way to see a book move as it moves. And why this
shape: a Claude model extends it by adding a folder and a path, so every source a module publishes
becomes a resource without anyone hand-writing gateway resources per source — and the day an issue
asks for something service-sized, it starts as a folder on this api with no migration first.

The page on openlyoperated.biz is a demo of this api, not a product beside it: every number it
shows came from one call, and the call is printed beside the number.
