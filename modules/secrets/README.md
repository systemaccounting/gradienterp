# secrets — the firm's vault

A firm hands its agent keys and tokens: a Stripe key, a vendor app's client secret, a token a
script needs. The agent must be able to cause those to be used without ever seeing one, and the
owner must be able to see what is stored and take it back.

The value goes in through a form in the chat and lands in the firm's own SSM, under a name the
agent chose. From then on the name is the handle: a tool reads the value by name at the moment
it needs it, and the agent's own `manage_secret` tool lists the names and deletes them. The
same tool refuses to store a value that arrives as a tool argument, so a secret typed into the
chat by mistake has nowhere to go.
