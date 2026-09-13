# site

The gerp's public web presence, rendered by its agent.

A visitor lands on kens-cafe.example, taps "order now", and the page they get was
composed by the business's own agent from the live books: the menu never lists an item
that's out (the 86 is an inventory read), the wait estimate comes off the real queue, the
special is a judgment call made at render time. Nobody maintains the site — not a CMS, not
a developer, not a page builder subscription. The site is the agent answering in html.

Orders close the loop: the order button is a form whose submission fires straight into the
books — invoicing and inventory move at the moment of purchase, because the order IS a books
event, not a POS record waiting to sync.

The same machinery serves a consultant's booking page, a wholesaler's catalog, a hotel's
availability — any business the platform runs. Pages that are answers, from books that are
already open.
