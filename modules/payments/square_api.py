"""The Square API version every Square call names, in one place.

Square answers by the `Square-Version` header and pins a webhook subscription to `api_version`; a
value that isn't a released date-form version is refused. `configure_webhook` sent the literal
placeholder "SQUARE_API_VERSION", so every Square setup failed and the webhook door stayed
unconfigured. The adapters import this, as the Stripe ones import `stripe_api`.
"""

VERSION = "2025-05-21"
